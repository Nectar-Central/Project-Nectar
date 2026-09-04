"""
processor.py — DB writes for kills, deaths, wins, playtime.

Called AFTER Engine parses packets and mutates SessionView.
Engine does NO DB writes — it gives us packets + view.

We query Engine classes directly (view.me, view.players, view.zone, view.game_mode)
instead of re-parsing packets. Packets only used for transient events (killfeed).

Called via FastAPI BackgroundTasks in POST /api/capture, so the
userscript gets its new_req_token fast (<20ms), and DB writes
happen async (~50ms). If DB is slow, POST still succeeds.
"""

# Use Python 3.10+ type hints like `str | None` without runtime errors on older versions
from __future__ import annotations

# Supabase client for DB queries (table inserts/updates/queries)
from server.lib.supabase_client import supabase, db

# Logging helpers: log_sys for info, log_err for errors, log_engine for pretty event lines
from server.lib.logger import log_sys, log_err, log_engine

# Strip color tags from player names (e.g. "<color=red>Player</color>" → "Player")
from server.lib.gpbinary import _strip_color_tags

# Retransmit window shared with the engine (killfeed can arrive twice with
# different Photon framing: client RaiseEvent echo + server broadcast).
from server.lib.photon_engine.engine import _KILL_DUP_WINDOW_MS

# Precision tracking: each counted write is re-checked ~2s later (see precision.py)
from server.lib import precision as _precision

# TYPE_CHECKING is False at runtime — only imports below when type-checking (mypy/pyright)
from typing import TYPE_CHECKING

# Import SessionView/AbstractPacket only for type hints to avoid circular imports
if TYPE_CHECKING:
    from server.lib.photon_engine.models import SessionView, AbstractPacket, RoomView


# ---------------------------------------------------------------------------
# Helpers — small pure functions used throughout the processor
# ---------------------------------------------------------------------------

def _norm_name(n: str | None) -> str:
    """Strip color tags, whitespace, and optional dc: prefix from a player name.
    Used to compare names from different sources (killfeed vs view.me).
    Killfeed may show "cis6led" while view.me shows "dc: cis6led" — same player.
    Case is preserved: "Name" != "name", but "dc: Alice" == "Alice".
    """
    if not n:                        # Guard: None or empty string → return ""
        return ""                    # Early return for missing names
    s = _strip_color_tags(str(n)).strip()  # Remove color tags + surrounding whitespace
    if s.lower().startswith("dc:"):  # Strip optional dc: prefix (case-insensitive prefix)
        s = s[3:].strip()            # Keep remainder case-sensitive
    return s                        # Return cleaned name


def _names_equal(a, b) -> bool:
    """Case-sensitive comparison of two player names after stripping color tags.
    Returns True if both names match exactly after normalization.
    Example: "dc: Alice" and "dc: Alice" → True
    Example: "Name" and "name" → False (case matters!)
    """
    na = _norm_name(a)              # Normalize first name
    nb = _norm_name(b)              # Normalize second name
    return na and nb and na == nb   # Both non-empty and exact match (case-sensitive)


def _extract_fields(pkt):
    """Extract common fields from packet (dict or Engine object).
    Returns a tuple of 12 fields used throughout the processor.
    Handles both dict packets (from JSON) and object packets (from Engine).
    """
    if isinstance(pkt, dict):       # Dict packet (JSON-serialized from Engine)
        pkt_type = pkt.get("packet") or pkt.get("packet_type")  # Packet type (kill, alive, leave, etc.)
        kill = pkt.get("kill")      # Kill sub-object if present
        if kill:                    # Kill sub-object exists → extract from it
            instigator = kill.get("instigator")   # Killer name
            instigated = kill.get("instigated")   # Victim name
            kill_type = kill.get("event_type")    # "killed" or "knocked"
        else:                       # No kill sub-object → try top-level fields
            instigator = pkt.get("killer") or pkt.get("instigator")  # Killer (try both names)
            instigated = pkt.get("victim") or pkt.get("instigated")  # Victim (try both names)
            kill_type = None        # No kill type without sub-object
        player_name = pkt.get("player_name") or (pkt.get("player") or {}).get("username")  # Player name (nested or top-level)
        zone_time = pkt.get("zone_time")         # Zone time value (0-600)
        zone_phase = pkt.get("zone_phase")       # Zone phase number (0-10)
        cur_scn = pkt.get("cur_scn")            # Current scene (MainLobby, GameScreen, etc.)
        is_leave = pkt.get("is_leave", False)    # True if player left the room
        is_kill = pkt.get("is_kill", False)      # True if this is a kill event
        is_knock = pkt.get("is_knock", False)    # True if this is a knock event
        is_join = pkt.get("is_join", False)      # True if player joined the room
    else:                           # Object packet (Engine AbstractPacket instance)
        pkt_type = getattr(pkt, "packet", None) or getattr(pkt, "packet_type", None)  # Packet type
        if hasattr(pkt_type, "value"):  # If type is an enum, get its string value
            pkt_type = pkt_type.value   # Unwrap enum to string
        kill = getattr(pkt, "kill", None)  # Kill sub-object
        if kill:                    # Kill sub-object exists
            instigator = getattr(kill, "instigator", None)  # Killer name
            instigated = getattr(kill, "instigated", None)  # Victim name
            kill_type = getattr(kill, "event_type", None)   # Kill type
        else:                       # No kill sub-object
            instigator = getattr(pkt, "killer", None) or getattr(pkt, "instigator", None)  # Killer
            instigated = getattr(pkt, "victim", None) or getattr(pkt, "instigated", None)  # Victim
            kill_type = None        # No kill type
        player_name = getattr(pkt, "player_name", None)  # Player name
        zone_time = getattr(pkt, "zone_time", None)      # Zone time
        zone_phase = getattr(pkt, "zone_phase", None)    # Zone phase
        cur_scn = getattr(pkt, "cur_scn", None)         # Current scene
        is_leave = getattr(pkt, "is_leave", False)       # Left room?
        is_kill = getattr(pkt, "is_kill", False)         # Kill event?
        is_knock = getattr(pkt, "is_knock", False)       # Knock event?
        is_join = getattr(pkt, "is_join", False)         # Join event?

    # Return all 12 fields as a tuple for unpacking at call site
    return (pkt_type, instigator, instigated, kill_type, player_name,
            zone_time, zone_phase, cur_scn, is_leave, is_kill, is_knock, is_join)


def _get_room(pkt_room_id, view) -> RoomView:
    """Resolve room from packet room_id, falling back to current room.
    Used when killfeed packets have room_id="lobby" but the actual match
    room is view.current_room (the FSM-tracked room).
    """
    room = view.rooms.get(pkt_room_id) if pkt_room_id else None  # Try packet's room_id first
    # Fall back to current room if: room not found, or room is lobby, or room not in MATCH state
    if not room or room.room_id == "lobby" or room.state.value != "match":
        room = view.current_room     # Use the FSM-tracked current room
    return room                      # Return resolved room


def _valid_zone_time(v, gm_hint):
    """Validate zone_time is 0-600, cap 300→299 for zw_lg like engine does.
    Game sends zone_time=300 for zw_lg which means "zone not started yet",
    so we cap it to 299 to distinguish from actual zone time.
    Returns None if invalid.
    """
    try:
        iv = int(float(v))          # Convert to int (handle float strings like "150.0")
        if 0 <= iv <= 600:          # Valid range
            # Cap 300→299 for zw_lg/wwzw (game sends 300 for "not started")
            if iv == 300 and str(gm_hint or "").lower() in ("zw_lg", "wwzw"):
                return 299           # Return capped value
            return iv                # Return valid value
    except Exception:
        pass                         # Invalid value → fall through to return None
    return None                      # Invalid or missing


def _member_status(room, view, member):
    """Tri-state liveness for one team member: True = alive, False = dead/gone evidence,
    None = no info. Deaths are loud (killfeed + alv=false always broadcast) while life is
    quiet — so no info means presumed alive by the caller (critical for late joiners whose
    teammates were never tracked in room.players)."""
    try:
        for p in room.players.values():  # tracked players (authoritative)
            if _names_equal(p.username, member):
                return True if p.is_alive else False
        for cand in (getattr(room, "me", None), getattr(view, "me", None)):  # self refs (players=0 rooms)
            if cand is not None and _names_equal(cand.username, member):
                return True if cand.is_alive else False
        for k in room.recent_kills:  # killfeed killed / leave evidence
            if k.event_type == "killed" and _names_equal(k.instigated, member):
                return False
            if k.event_type == "left" and _names_equal(k.instigator, member):
                return False
    except Exception:
        return None
    return None


def _team_alive_for_me(room, view, me_name):
    """Duo team status for self: True = team alive (a member alive or with no death
    evidence), False = whole team dead/gone, None = no team data (solo rules).
    Teams come from _pt packets."""
    try:
        if not room or not me_name:
            return None
        my_team = room.get_team(me_name)  # exact match first (raw usernames)
        if my_team is None and getattr(room, "teams", None):
            for t in room.teams.values():  # normalized fallback (dc: form mismatch)
                if any(_names_equal(m, me_name) for m in t.members):
                    my_team = t
                    break
        if my_team is None or len(my_team.members) < 2:
            return None  # solo or no team data
        for m in my_team.members:
            if _member_status(room, view, m) is not False:  # alive OR no info → counts as alive
                return True
        return False  # every member has death/gone evidence
    except Exception:
        return None


def _is_kd_only(gm) -> bool:
    """Respawn modes: K/D tracked, no personal defeat (DF free mode, RvB TDM).
    Wins/defeats in RvB come only from the team-15 race or stG=3 team result.
    """
    return str(gm or "").lower() in ("df", "tdm_bvr")


def _kill_count_dup(room, killer, victim, kind, pkt_t) -> bool:
    """Retransmit guard for kill counting: same killer->victim kind already
    counted within the window. State lives on the room (survives batches via
    Upstash) and is stamped ONLY here — engine appends use recent_kills, so the
    two paths can never suppress each other. Genuine same-pair re-kills need a
    respawn cycle, far outside the window."""
    try:
        if not killer or not victim or room is None:
            return False
        try:
            now_ms = float(pkt_t) if pkt_t is not None else None
        except Exception:
            now_ms = None
        if now_ms is None:
            try:
                import time as _pt
                now_ms = _pt.time() * 1000.0
            except Exception:
                return False
        key = f"{_norm_name(killer)}\x00{_norm_name(victim)}\x00{kind}"
        seen = getattr(room, "proc_kill_seen", None)
        if not isinstance(seen, dict):
            return False
        # prune stale + overgrown entries opportunistically
        try:
            if len(seen) > 500:
                for k in [k for k, ts in seen.items() if now_ms - float(ts) > _KILL_DUP_WINDOW_MS]:
                    seen.pop(k, None)
                while len(seen) > 500:
                    seen.pop(next(iter(seen)))
        except Exception:
            pass
        try:
            last = seen.get(key)
            if last is not None and abs(now_ms - float(last)) <= _KILL_DUP_WINDOW_MS:
                return True
        except Exception:
            pass
    except Exception:
        pass
    return False


def _kill_count_stamp(room, killer, victim, kind, pkt_t) -> None:
    """Record a counted kill for the retransmit guard above."""
    try:
        if not killer or not victim or room is None:
            return
        try:
            now_ms = float(pkt_t) if pkt_t is not None else None
        except Exception:
            now_ms = None
        if now_ms is None:
            try:
                import time as _pt
                now_ms = _pt.time() * 1000.0
            except Exception:
                return
        key = f"{_norm_name(killer)}\x00{_norm_name(victim)}\x00{kind}"
        seen = getattr(room, "proc_kill_seen", None)
        if not isinstance(seen, dict):
            try:
                room.proc_kill_seen = {}
                seen = room.proc_kill_seen
            except Exception:
                return
        seen[key] = now_ms
    except Exception:
        pass


def _extract_actors(pkt):
    """Return (actor_nr, target_actor_nr) as ints or Nones from packet (dict or object).
    Threaded from Engine (params.ActorNr / params.TargetActorNr) for correct attribution.
    """
    if isinstance(pkt, dict):       # Dict packet from capture.py
        a = pkt.get("actor_nr")     # Sender actor
        t = pkt.get("target_actor_nr")  # Subject actor
    else:                           # Engine object
        a = getattr(pkt, "actor_nr", None)
        t = getattr(pkt, "target_actor_nr", None)
    try:
        a = int(a) if a is not None else None
    except Exception:
        a = None                    # Non-numeric → treat as absent
    try:
        t = int(t) if t is not None else None
    except Exception:
        t = None
    return a, t


def _eff_gm(pkt, pkt_room_gm, gm_val):
    """Effective game mode: packet → room → global fallback.
    Packet-level game_mode overrides room-level, which overrides session-level.
    Handles both dict and enum values.
    """
    # Get game_mode from packet (dict or object)
    pkt_gm = pkt.get("game_mode") if isinstance(pkt, dict) else getattr(pkt, "game_mode", None)
    if isinstance(pkt_gm, dict):    # If dict, extract "value" key or stringify
        pkt_gm = pkt_gm.get("value") if "value" in pkt_gm else str(pkt_gm)
    return pkt_gm or pkt_room_gm or gm_val  # Fallback chain: packet → room → session


def _write_win(discord_id, session_id, user, gm, room_id, short_code, match_ver, winner, room):
    """Write one win to DB (bngc_users + bngc_game_events) + log lines.
    Marks room.last_win_version (NOT drained — version bump re-enables next match).
    """
    new_wins = (user.get("wins") or 0) + 1  # Increment win count
    supabase.table("bngc_users").update({"wins": new_wins}).eq("discord_id", discord_id).execute()
    supabase.table("bngc_game_events").insert({
        "discord_id": discord_id,       # Which Discord user owns this session
        "session_id": session_id,       # Which session
        "event_type": "win",            # Win event
        "event_data": {"gm": gm, "room_id": room_id, "short_code": short_code,
                       "match_version": match_ver, "winner": winner},
    }).execute()
    try:
        _duo = bool(room.is_duo) if room is not None else False
    except Exception:
        _duo = False
    log_engine("win", session_id[:8], winner=winner, gm=gm, duo=_duo, room=str(room_id or "")[:8])
    log_sys(f"PROCESSOR WIN gm={gm} winner={winner} wins {user.get('wins',0)}->{new_wins}")
    user["wins"] = new_wins             # Update local cache
    if room is not None:
        room.last_win_version = match_ver  # Mark recorded (version bump re-enables)
    _precision.schedule("win", session_id=session_id, discord_id=discord_id,
                        expected=new_wins, match_version=match_ver, room_id=room_id)
    return new_wins


def _write_defeat(discord_id, session_id, user, gm, room_id, short_code, match_ver, room):
    """Write one defeat to DB (bngc_users + bngc_game_events) + log lines.
    Sets room.had_self_defeat so later stG=3 / leave defeats are blocked. DF excluded by caller.
    Suppressed when a win was already recorded for this version (win stands).
    """
    if room is not None and room.last_win_version == match_ver:
        log_sys(f"PROCESSOR DEFEAT skip win already recorded gm={gm}")  # Win stands
        return user.get("defeats") or 0
    new_def = (user.get("defeats") or 0) + 1  # Increment defeat count
    supabase.table("bngc_users").update({"defeats": new_def}).eq("discord_id", discord_id).execute()
    supabase.table("bngc_game_events").insert({
        "discord_id": discord_id,       # Which Discord user owns this session
        "session_id": session_id,       # Which session
        "event_type": "defeat",         # Defeat event
        "event_data": {"gm": gm, "room_id": room_id, "short_code": short_code,
                       "match_version": match_ver},
    }).execute()
    try:
        _duo = bool(room.is_duo) if room is not None else False
    except Exception:
        _duo = False
    log_engine("defeat", session_id[:8], gm=gm, duo=_duo, room=str(room_id or "")[:8])
    log_sys(f"PROCESSOR DEFEAT gm={gm} defeats {user.get('defeats',0)}->{new_def}")
    user["defeats"] = new_def           # Update local cache
    if room is not None:
        room.had_self_defeat = True     # Block later stG=3 / leave defeats this life
    _precision.schedule("defeat", session_id=session_id, discord_id=discord_id,
                        expected=new_def, match_version=match_ver, room_id=room_id)
    return new_def


def _upsert_stats(discord_id, user):
    """Upsert bngc_player_stats from bngc_users fields.
    Tries upsert first, then insert, then update as fallback chain.
    This ensures the stats table always has the latest values.
    """
    row = {                         # Build the stats row
        "discord_id": discord_id,   # User's Discord ID (primary key)
        "kills": int(user.get("kills") or 0),    # Total kills
        "deaths": int(user.get("deaths") or 0),  # Total deaths
        "wins": int(user.get("wins") or 0),      # Total wins
        "defeats": int(user.get("defeats") or 0), # Total defeats
    }
    try:
        # Try upsert first (insert or update if exists)
        supabase.table("bngc_player_stats").upsert(row, on_conflict="discord_id").execute()
    except Exception:
        try:
            # If upsert fails, try insert (new row)
            supabase.table("bngc_player_stats").insert(row).execute()
        except Exception:
            try:
                # If insert fails, try update (existing row)
                supabase.table("bngc_player_stats").update(
                    {k: v for k, v in row.items() if k != "discord_id"}  # Exclude primary key
                ).eq("discord_id", discord_id).execute()  # Where discord_id matches
            except Exception as exc:
                log_err(f"processor player_stats sync failed {exc}")  # All attempts failed


def _sync_playtime(view, user, discord_id):
    """Aggregate per-room playtime into bngc_users and reset counters.
    Combines playtime from all rooms (excluding lobby) into per-game-mode totals.
    Also adds lobby time. Writes to DB and resets room counters to zero.
    """
    per_gm_add = {}                 # Accumulate seconds per game mode
    lobby_add = 0                   # Accumulate lobby seconds
    for r in view.rooms.values():   # Iterate all rooms
        if r.room_id == "lobby":    # Skip lobby room (tracked separately)
            continue
        for gm_key, secs in r.match_playtime.per_gm.items():  # Add per-GM time
            per_gm_add[gm_key] = per_gm_add.get(gm_key, 0) + secs
        lobby_add += r.match_playtime.lobby_seconds  # Add lobby time from this room
    # Fallback: use session-level playtime if rooms didn't capture
    if not per_gm_add and view.playtime.per_gm:
        per_gm_add = view.playtime.per_gm  # Use session-level per-GM
        lobby_add = view.playtime.lobby_seconds  # Use session-level lobby
    if not per_gm_add and not lobby_add:  # Nothing to sync
        return                       # Early return
    import json                      # Import json for serialization
    cur_per = {}                     # Current playtime_per_gm from DB
    try:
        cur_per = json.loads(user.get("playtime_per_gm") or "{}")  # Parse existing JSON
    except Exception:
        pass                         # If parse fails, start with empty dict
    for gm_k, secs in per_gm_add.items():  # Merge new time into existing
        cur_per[gm_k] = cur_per.get(gm_k, 0) + secs
    cur_per["lobby"] = cur_per.get("lobby", 0) + lobby_add  # Add lobby time
    total_add = sum(per_gm_add.values()) + lobby_add  # Total seconds to add
    new_total = (user.get("playtime_global") or 0) + total_add  # New global total
    # Write to DB
    supabase.table("bngc_users").update({
        "playtime_global": new_total,           # Updated global playtime
        "playtime_per_gm": json.dumps(cur_per), # Updated per-GM JSON
    }).eq("discord_id", discord_id).execute()   # Where discord_id matches
    log_sys(f"PROCESSOR playtime per_gm={per_gm_add} lobby={lobby_add} total+{total_add}")
    # Reset room counters to zero (already flushed to DB)
    for r in view.rooms.values():
        r.match_playtime.total_seconds = 0      # Reset total
        r.match_playtime.per_gm = {}            # Reset per-GM dict
        r.match_playtime.lobby_seconds = 0      # Reset lobby seconds
    # Reset session-level counters too
    view.playtime.per_gm = {}
    view.playtime.lobby_seconds = 0
    view.playtime.total_seconds = 0


# ---------------------------------------------------------------------------
# Main processor — called by POST /api/capture via BackgroundTasks
# ---------------------------------------------------------------------------

async def process_batch(
    session_id: str,                # Session ID from bngc_sessions
    packets: list,                  # List of parsed Photon packets (dicts)
    view: SessionView,            # Engine's live session state
) -> None:
    """
    Process a batch of packets for one session_id.

    Writes to bngc_game_events:
      - killfeed: all kill/knock events (entire lobby, not just self)
      - kill/death: self-specific for bngc_users stat tracking
      - zone_change: zone phase transitions
      - win/defeat: end-of-match results

    Also syncs bngc_users fields from SessionView (username, game_id, skin, pickaxe).
    """
    # Early exit if missing required data
    if not session_id:              # No session ID
        return                       # Can't do anything
    if not packets:                  # No packets to process
        return                       # Nothing to do
    if not view:                     # No session view
        return                       # Can't access player/room state

    # Log processor start with session metadata
    log_sys(f"PROCESSOR START session={session_id[:8]} packets={len(packets)} "
            f"state={view.state.value} gm={view.game_mode.value if view.game_mode else '?'} "
            f"me={view.me.username if view.me else '?'} players={view.players_count}")

    # Fetch discord_id from bngc_sessions table
    try:
        sess_res = db.get_session_sync(session_id)  # Query session by ID
        sess_data = sess_res.get("data")            # Extract data dict
        discord_id = sess_data.get("discord_id") if sess_data else None  # Get Discord ID
        if not discord_id:            # No Discord ID found
            log_err(f"processor: no discord_id for session {session_id[:8]}")
            return                    # Can't proceed without Discord ID
    except Exception as exc:
        log_err(f"processor: get session failed {session_id[:8]} {exc}")
        return                        # DB error → abort

    # Fetch user row from bngc_users (kills, deaths, wins, etc.)
    try:
        user_res = supabase.table("bngc_users").select("*").eq("discord_id", discord_id).single().execute()
        user = user_res.data          # Extract user dict
        if not user:                  # No user found
            log_err(f"processor: no user for discord_id {discord_id[:6]}")
            return                    # Can't proceed without user
    except Exception as exc:
        log_err(f"processor: get user failed {discord_id[:6]} {exc}")
        return                        # DB error → abort

    # Log current user stats before processing
    log_sys(f"PROCESSOR user={discord_id[:6]} kills={user.get('kills',0)} "
            f"deaths={user.get('deaths',0)} wins={user.get('wins',0)}")

    # Settle precision checks due from earlier batches (writes are verified ~2s later)
    try:
        await _precision.verify_due()
    except Exception:
        pass

    # Track which match_versions already had a death counted (dedup alv + killfeed)
    # Prevents double-counting when both killfeed and alv report the same death
    death_counted_for_match = set()  # Set of (discord_id, match_version) tuples
    me_name = (view.me.username if view.me else None) or user.get("game_username")  # My game username
    gm_val = view.game_mode.value if view.game_mode else None  # Current game mode string
    last_zone_phase = getattr(view, "_lastZonePhase", None)  # Last zone phase (persisted on view)

    # Sync user fields from SessionView (so /info, /stats are live)
    # Only updates if values changed to avoid unnecessary DB writes
    try:
        updates = {}                 # Build update dict
        if view.me:                  # Only if we have player data
            if view.me.username and view.me.username != user.get("game_username"):
                updates["game_username"] = view.me.username  # Username changed
            if view.me.game_id and view.me.game_id != user.get("game_id"):
                updates["game_id"] = view.me.game_id        # Game ID changed
            if view.me.skin and view.me.skin != user.get("skin"):
                updates["skin"] = view.me.skin              # Skin changed
            if view.me.pickaxe and view.me.pickaxe != user.get("pickaxe"):
                updates["pickaxe"] = view.me.pickaxe        # Pickaxe changed
        if updates:                  # Only write if something changed
            supabase.table("bngc_users").update(updates).eq("discord_id", discord_id).execute()
            log_sys(f"PROCESSOR sync user {discord_id[:6]} {updates}")
            user.update(updates)     # Keep local copy in sync
    except Exception as exc:
        log_err(f"processor sync user failed {exc}")

    # -------------------------------------------------------------------
    # Per-packet processing — iterate each packet in the batch
    # -------------------------------------------------------------------
    for pkt in packets:              # Each packet is a dict from Engine
        # Unpack 12 fields from packet
        (pkt_type, instigator, instigated, kill_type, player_name,
         zone_time, zone_phase, cur_scn, is_leave, is_kill, is_knock, is_join) = _extract_fields(pkt)

        # Per-packet room context stamped by Engine (avoids batch-final gm merging)
        pkt_room_id = pkt.get("room_id") or pkt.get("room_code") if isinstance(pkt, dict) else getattr(pkt, "room_id", None)  # Room ID from packet
        pkt_short = pkt.get("short_code") if isinstance(pkt, dict) else getattr(pkt, "short_code", None)  # Short code (5-char server room ID)
        pkt_match_ver = pkt.get("match_version") if isinstance(pkt, dict) else getattr(pkt, "match_version", None)  # Match version (0 during match)
        pkt_room_gm = pkt.get("room_gm") if isinstance(pkt, dict) else getattr(pkt, "room_gm", None)  # Room's game mode
        eff_gm = _eff_gm(pkt, pkt_room_gm, gm_val)  # Effective game mode (packet → room → session)

        # Log packet details for debugging
        log_sys(f"PROCESSOR pkt type={pkt_type} ig={instigator} ed={instigated} "
                f"kill={is_kill} knock={is_knock} leave={is_leave} me={me_name} "
                f"gm={eff_gm} room={str(pkt_room_id or '')[:8]}")

        try:
            # --- Killfeed: kill or knock (entire lobby) ---
            # Check if this is a kill/knock event (by flag or type string)
            is_kf = is_kill or is_knock or pkt_type in ("kill", "KILL", "Kill", "knock", "KNOCK", "Knock")
            if is_kf and instigator and instigated:  # Valid killfeed needs both killer and victim
                # Determine if it's a kill or knock
                kf_type = "killed" if (is_kill or pkt_type in ("kill", "KILL", "Kill")) else "knocked"
                # Outbound RaiseEvent echo: our client sends the killfeed text and the
                # server rebroadcasts it ~0.5s later. Only the broadcast counts — the
                # echo would double every kill (and every knock).
                pkt_is_echo = pkt.get("is_echo", False) if isinstance(pkt, dict) else bool(getattr(pkt, "is_echo", False))
                # Retransmit guard: same content within the window (Photon can deliver
                # the same event twice with different framing).
                _kf_room = _get_room(pkt_room_id, view)
                _kf_t = pkt.get("t") if isinstance(pkt, dict) else getattr(pkt, "t", None)
                if pkt_is_echo:
                    skip_kf = True
                    log_sys(f"PROCESSOR killfeed echo ignored {instigator}->{instigated} ({kf_type})")
                elif _kill_count_dup(_kf_room, instigator, instigated, kf_type, _kf_t):
                    skip_kf = True
                    log_sys(f"PROCESSOR killfeed dup ignored {instigator}->{instigated} ({kf_type}, retransmit window)")
                else:
                    skip_kf = False
                # Insert killfeed event (records ALL kills/knocks in the lobby)
                if not skip_kf:
                    supabase.table("bngc_game_events").insert({
                        "discord_id": discord_id,       # Which Discord user owns this session
                        "session_id": session_id,       # Which session
                        "event_type": "killfeed",       # Event type for querying
                        "event_data": {                 # Kill details
                            "gm": eff_gm,              # Game mode
                            "instigator": instigator,   # Killer name
                            "instigated": instigated,   # Victim name
                            "kill_type": kf_type,       # "killed" or "knocked"
                            "room_id": pkt_room_id,     # Room where it happened
                            "code": pkt_short,          # Room short code
                            "short_code": pkt_short,    # Duplicate for querying
                            "match_version": pkt_match_ver,  # Match version
                        },
                    }).execute()
                    _kill_count_stamp(_kf_room, instigator, instigated, kf_type, _kf_t)

                # Self-specific kill: increment kills + write kill event.
                # Kills only (kf_type == killed): a knock is not an elimination —
                # counting it doubled every knock-then-finish.
                if not skip_kf and kf_type == "killed" and me_name and _names_equal(instigator, me_name):  # I am the killer
                    new_kills = (user.get("kills") or 0) + 1  # Increment kill count
                    supabase.table("bngc_users").update({"kills": new_kills}).eq("discord_id", discord_id).execute()
                    # Write kill event (separate from killfeed for stat tracking)
                    supabase.table("bngc_game_events").insert({
                        "discord_id": discord_id,
                        "session_id": session_id,
                        "event_type": "kill",       # Self-kill event
                        "event_data": {"gm": eff_gm, "victim": instigated, "room_id": pkt_room_id,
                                       "code": pkt_short, "match_version": pkt_match_ver},
                    }).execute()
                    log_sys(f"PROCESSOR KILL victim={instigated} kills {user.get('kills',0)}->{new_kills}")
                    user["kills"] = new_kills       # Update local cache
                    _precision.schedule("kill", session_id=session_id, discord_id=discord_id,
                                        expected=new_kills, match_version=pkt_match_ver,
                                        room_id=pkt_room_id, extra={"victim": instigated})

                # Self-specific death: increment deaths + write death event (one per life).
                # Kills only: being knocked is not death (revive possible) — the
                # finishing "killed" feed or alv=false counts it.
                elif not skip_kf and kf_type == "killed" and me_name and _names_equal(instigated, me_name):  # I am the victim
                    # Dedup key: prevent double-counting from killfeed + alv
                    dedup_key = (discord_id, pkt_match_ver if pkt_match_ver is not None else pkt_room_id)
                    if dedup_key not in death_counted_for_match:  # Not yet counted
                        death_room = _get_room(pkt_room_id, view)  # Resolve room for per-life flag
                        if death_room is not None and death_room.had_self_death:
                            log_sys(f"PROCESSOR DEATH via killfeed skip already counted this life killer={instigator}")
                            death_counted_for_match.add(dedup_key)  # Mark as counted
                        else:
                            new_deaths = (user.get("deaths") or 0) + 1  # Increment death count
                            supabase.table("bngc_users").update({"deaths": new_deaths}).eq("discord_id", discord_id).execute()
                            # Write death event (with "via": "killfeed" to track source)
                            supabase.table("bngc_game_events").insert({
                                "discord_id": discord_id,
                                "session_id": session_id,
                                "event_type": "death",
                                "event_data": {"gm": eff_gm, "killer": instigator, "via": "killfeed",
                                               "room_id": pkt_room_id, "code": pkt_short, "match_version": pkt_match_ver},
                            }).execute()
                            log_sys(f"PROCESSOR DEATH via killfeed killer={instigator} deaths {user.get('deaths',0)}->{new_deaths}")
                            user["deaths"] = new_deaths  # Update local cache
                            death_counted_for_match.add(dedup_key)  # Mark as counted
                            _precision.schedule("death", session_id=session_id, discord_id=discord_id,
                                                expected=new_deaths, match_version=pkt_match_ver,
                                                room_id=pkt_room_id, extra={"via": "killfeed"})
                            if death_room is not None:
                                death_room.had_self_death = True  # One death per life
                            # Defeat NOW: death before stG=3 counts immediately (K/D-only
                            # modes excluded), EXCEPT in duos with a living teammate
                            # (defeat only when whole team dead). Later stG=3 / leave
                            # defeats are blocked by had_self_defeat.
                            try:
                                if not _is_kd_only(eff_gm) and not (death_room is not None and death_room.had_self_defeat):
                                    _tstat = _team_alive_for_me(death_room, view, me_name)
                                    if _tstat is True:  # duo + teammate alive → hold marker for drain
                                        if death_room is not None and death_room.last_defeat_version != death_room.match_version:
                                            death_room.last_defeat_version = death_room.match_version
                                        log_sys(f"PROCESSOR defeat deferred duo teammate alive gm={eff_gm} (held for drain)")
                                    else:  # solo, no team data, or whole team dead
                                        _write_defeat(discord_id, session_id, user, eff_gm, pkt_room_id, pkt_short, pkt_match_ver, death_room)
                            except Exception as e:
                                log_err(f"processor defeat-via-kill failed {e}")

            # --- Death via alv (self elim not in killfeed) ---
            # Some eliminations don't appear in killfeed (e.g. zone damage, disconnect)
            # The "alive" packet with is_alive=false or is_eliminated=true signals death
            if pkt_type in ("alive", "eliminated", "ALIVE", "ELIMINATED"):
                try:
                    # Extract alive/eliminated flags from packet
                    alv_is_alive = pkt.get("is_alive") if isinstance(pkt, dict) else getattr(pkt, "is_alive", None)
                    alv_is_elim = pkt.get("is_eliminated") if isinstance(pkt, dict) else getattr(pkt, "is_eliminated", None)
                    alv_player = pkt.get("player_name") or player_name or instigator  # Which player

                    # Resolve nameless alv via actor map FIRST (correct attribution:
                    # actor 2's death must not count as mine just because I'm also dead)
                    if not alv_player and isinstance(pkt, dict):
                        try:
                            _pa, _pt = _extract_actors(pkt)  # Threaded from Engine params
                            _rm0 = _get_room(pkt_room_id, view) if pkt_room_id else view.current_room
                            if _rm0:  # Try sender then subject actor
                                for _nr in (_pa, _pt):
                                    if _nr is None:
                                        continue
                                    try:
                                        _nm = _rm0.actor_map.get(int(_nr))
                                    except Exception:
                                        _nm = None
                                    if _nm:  # Resolved: trust the map (may or may not be me)
                                        alv_player = _nm
                                        break
                        except Exception:
                            pass
                    # Last resort: no name AND no actor info — if I'm dead in room state,
                    # assume this nameless alv=false is mine (my own broadcast without 255)
                    if not alv_player and pkt_room_id and isinstance(pkt, dict) and pkt.get("is_alive") is False:
                        _pa2, _pt2 = _extract_actors(pkt)
                        if _pa2 is None and _pt2 is None:  # Truly anonymous — heuristic allowed
                            try:
                                room_alv = _get_room(pkt_room_id, view)  # Resolve room
                                if room_alv and me_name:  # Have room and know my name
                                    for pp in room_alv.players.values():  # Check each player
                                        if _names_equal(pp.username, me_name) and not pp.is_alive:  # My player is dead
                                            alv_player = me_name  # Found myself
                                            alv_is_alive = False  # Confirm dead
                                            break                # Stop searching
                            except Exception:
                                pass   # Silently ignore lookup errors
                        else:
                            log_sys(f"PROCESSOR alv actors={(_pa2, _pt2)} unmapped — not mine, skip")

                    # Check if this alv packet indicates death
                    is_dead_alv = (alv_is_alive is False) or bool(alv_is_elim)
                    if is_dead_alv and me_name and alv_player and _names_equal(alv_player, me_name):  # I died
                        dedup_key_alv = (discord_id, pkt_match_ver if pkt_match_ver is not None else pkt_room_id)
                        if dedup_key_alv not in death_counted_for_match:  # Not yet counted
                            room_alv = _get_room(pkt_room_id, view)  # Resolve room
                            # One death per life: stale alv=false repeats (still dead from
                            # an earlier counted death, never revived) must not recount
                            if room_alv and not room_alv.had_self_death:
                                new_deaths_alv = (user.get("deaths") or 0) + 1  # Increment deaths
                                supabase.table("bngc_users").update({"deaths": new_deaths_alv}).eq("discord_id", discord_id).execute()
                                # Write death event (with "via": "alv" to track source)
                                supabase.table("bngc_game_events").insert({
                                    "discord_id": discord_id,
                                    "session_id": session_id,
                                    "event_type": "death",
                                    "event_data": {"gm": eff_gm, "killer": None, "via": "alv",
                                                   "room_id": pkt_room_id, "code": pkt_short, "match_version": pkt_match_ver},
                                }).execute()
                                log_sys(f"PROCESSOR DEATH via alv deaths {user.get('deaths',0)}->{new_deaths_alv}")
                                user["deaths"] = new_deaths_alv  # Update local cache
                                death_counted_for_match.add(dedup_key_alv)  # Mark as counted
                                _precision.schedule("death", session_id=session_id, discord_id=discord_id,
                                                    expected=new_deaths_alv, match_version=pkt_match_ver,
                                                    room_id=pkt_room_id, extra={"via": "alv"})
                                room_alv.had_self_death = True  # One death per life
                                # Defeat NOW (same rule as killfeed path, K/D-only excluded)
                                try:
                                    if not _is_kd_only(eff_gm) and not room_alv.had_self_defeat:
                                        _tstat_alv = _team_alive_for_me(room_alv, view, me_name)
                                        if _tstat_alv is True:  # duo + teammate alive → hold marker for drain
                                            if room_alv.last_defeat_version != room_alv.match_version:
                                                room_alv.last_defeat_version = room_alv.match_version
                                            log_sys(f"PROCESSOR defeat deferred duo teammate alive gm={eff_gm} via=alv (held for drain)")
                                        else:
                                            _write_defeat(discord_id, session_id, user, eff_gm, pkt_room_id, pkt_short, pkt_match_ver, room_alv)
                                except Exception as e_def:
                                    log_err(f"processor defeat-via-alv failed {e_def}")
                            else:
                                log_sys(f"PROCESSOR DEATH via alv skip already counted this life")
                except Exception as e:
                    log_err(f"processor alv death failed {e}")

            # --- Join events (new player joined room) ---
            # Persist so lobby history is visible in the UI
            if is_join or pkt_type in ("join", "JOIN", "Join", "create_room", "join_room", "join_random_room"):
                joiner = player_name or instigator  # Who joined
                if joiner:             # Have a valid name
                    try:
                        supabase.table("bngc_game_events").insert({
                            "discord_id": discord_id,
                            "session_id": session_id,
                            "event_type": "join",    # Join event type
                            "event_data": {"gm": eff_gm, "player": joiner, "player_name": joiner,
                                           "room_id": pkt_room_id, "code": pkt_short,
                                           "short_code": pkt_short, "match_version": pkt_match_ver},
                        }).execute()
                        log_sys(f"PROCESSOR JOIN {joiner} gm={eff_gm}")
                    except Exception as exc_j:
                        log_err(f"processor join write failed {joiner}: {exc_j}")

            # --- Leave events (player left or was kicked) ---
            if is_leave or pkt_type in ("leave", "LEAVE", "Leave"):
                leaver = player_name or instigator  # Who left
                if leaver:              # Have a valid name
                    # Write as killfeed event with kill_type="left"
                    supabase.table("bngc_game_events").insert({
                        "discord_id": discord_id,
                        "session_id": session_id,
                        "event_type": "killfeed",   # Killfeed type for querying
                        "event_data": {"gm": eff_gm, "instigator": leaver, "instigated": None,
                                       "kill_type": "left", "room_id": pkt_room_id, "code": pkt_short,
                                       "match_version": pkt_match_ver},
                    }).execute()
                    # Write as discrete leave event (separate from killfeed)
                    try:
                        supabase.table("bngc_game_events").insert({
                            "discord_id": discord_id,
                            "session_id": session_id,
                            "event_type": "leave",   # Leave event type
                            "event_data": {"gm": eff_gm, "player": leaver, "player_name": leaver,
                                           "room_id": pkt_room_id, "code": pkt_short, "match_version": pkt_match_ver},
                        }).execute()
                    except Exception:
                        pass            # Silently ignore leave event write errors
                    log_sys(f"PROCESSOR LEAVE {leaver} gm={eff_gm}")

                    # Defeat via leave: quitting mid-match is a personal defeat immediately
                    # (abandon = loss, teammate status irrelevant). Blocked when already
                    # recorded this life, when a win stands, or in K/D-only modes.
                    try:
                        if _names_equal(leaver, me_name):  # I am the leaver
                            gm_low_lv = str(eff_gm or "").lower()  # Lowercase game mode
                            if gm_low_lv and not _is_kd_only(eff_gm):  # K/D-only has no defeat
                                room2 = _get_room(pkt_room_id, view)  # Resolve room
                                # Room must be in MATCH state (pcount unused: I just left it)
                                if room2 and room2.state.value == "match":
                                    if (not room2.had_self_defeat
                                            and room2.last_win_version != room2.match_version):
                                        _write_defeat(discord_id, session_id, user, eff_gm, pkt_room_id, pkt_short, pkt_match_ver, room2)
                                        log_sys(f"PROCESSOR DEFEAT via leave gm={eff_gm}")
                                    else:
                                        log_sys(f"PROCESSOR defeat-via-leave skip already recorded this life gm={eff_gm}")
                    except Exception as e:
                        log_err(f"processor defeat-via-leave failed {e}")

                    # Walkover win: whole enemy team left (duo/small-team modes + 1v1s).
                    # My own leave takes the defeat path above, never a walkover.
                    try:
                        _wo_gm = str(eff_gm or "").lower()
                        _wo_room = _get_room(pkt_room_id, view)  # Resolve independently (room2 is self-leave scoped)
                        if (_wo_gm and not _is_kd_only(eff_gm) and _wo_room is not None
                                and _wo_room.state.value == "match" and me_name
                                and not _names_equal(leaver, me_name)
                                and not _wo_room.had_self_defeat
                                and _wo_room.last_win_version != _wo_room.match_version):
                            _wo_winner = None
                            if _wo_room.teams and all(len(t.members) <= 2 for t in _wo_room.teams.values()):
                                # Duo walkover: my team + every enemy team fully left
                                _my_t = _wo_room.get_team(me_name)
                                if _my_t is None:
                                    for t in _wo_room.teams.values():
                                        if any(_names_equal(m, me_name) for m in t.members):
                                            _my_t = t
                                            break
                                if _my_t is not None:
                                    _left = {k.instigator for k in _wo_room.recent_kills[-50:] if k.event_type == "left" and k.instigator}
                                    _gone = lambda m: any(_names_equal(m, l) for l in _left)
                                    _foes = [t for tid, t in _wo_room.teams.items() if tid != _my_t.team_id]
                                    if (_foes and all(_gone(m) for t in _foes for m in t.members)
                                            and any(not _gone(m) for m in _my_t.members)):
                                        _wo_winner = _my_t.team_id
                            elif _wo_gm in ("bf", "va", "r_1v1"):
                                _wo_winner = me_name  # 1v1: opponent left = win
                            if _wo_winner is not None:
                                _write_win(discord_id, session_id, user, eff_gm, pkt_room_id, pkt_short, _wo_room.match_version, _wo_winner, _wo_room)
                                log_sys(f"PROCESSOR WIN via walkover winner={_wo_winner} gm={eff_gm}")
                    except Exception as e_wo:
                        log_err(f"processor walkover failed {e_wo}")

            # --- Zone phase changes (compare to last seen) ---
            # Only record when phase actually changes (dedup)
            if zone_phase is not None:  # Zone phase present in packet
                vt = _valid_zone_time(zone_time, eff_gm) if zone_time is not None else None  # Validate time
                try:
                    phase_int = int(float(str(zone_phase).strip()))  # Convert to int
                    sane_phase = 0 <= phase_int <= 10  # Valid range check
                except Exception:
                    sane_phase = True   # If can't parse, assume valid (don't block)
                # Only insert if phase changed and is sane
                if sane_phase and str(zone_phase) != str(last_zone_phase):
                    supabase.table("bngc_game_events").insert({
                        "discord_id": discord_id,
                        "session_id": session_id,
                        "event_type": "zone_change",  # Zone phase change event
                        "event_data": {"gm": eff_gm, "phase": str(zone_phase), "time": vt,
                                       "room_id": pkt_room_id, "code": pkt_short, "match_version": pkt_match_ver},
                    }).execute()
                    log_sys(f"PROCESSOR ZONE {last_zone_phase}→{zone_phase} time={vt}")
                    last_zone_phase = zone_phase  # Update tracker for next comparison

        except Exception as exc:
            log_err(f"processor packet write failed {exc}")  # Log and continue to next packet

    # Save zone phase tracker on view (persists across batches)
    try:
        view._lastZonePhase = last_zone_phase  # Store for next batch
    except Exception:
        pass                         # Silently ignore if view is read-only

    # -------------------------------------------------------------------
    # Per-room: win / defeat — strict FSM drain
    # Philosophy:
    #   individualist (default): only self kills/deaths/wins/defeats
    #   host collab (server room + host_mode): also record others for replay
    #   Players list is collaborative in both modes
    #   Duo teammate death cared for via team elimination (BF2V2/WWZW)
    # Handles: zw_lg/wwzw stG 3→1, BF2V2 team elimination, DF K/D only
    # -------------------------------------------------------------------

    is_host = bool(getattr(view, "host_mode", False))  # Check if host mode is enabled

    def _should_skip_room(r) -> bool:
        """Skip lobby rooms with no game mode and no pending win/defeat.
        This prevents processing lobby-only batches that have no match data.
        """
        return r.room_id == "lobby" and not r.game_mode and r.last_win_version != r.match_version and r.last_defeat_version != r.match_version

    def _is_server_room(r) -> bool:
        """Server-created rooms carry a 5-char short_code.
        Used to distinguish server-hosted rooms from player-created rooms.
        """
        return bool(r.short_code)

    # --- Wins: check stG=3 → who's alive → set winner ---
    # Processor uses stG and alv fields directly (Engine just updates state)
    try:
        for room_id, room in list(view.rooms.items()):  # Iterate all rooms
            if _should_skip_room(room):  # Skip lobby with no pending win
                continue
            gm = room.game_mode.value if room.game_mode else gm_val  # Room's game mode
            if str(gm).lower() == "df":  # DF has no win/defeat (K/D only)
                continue

            # stG=3 detection: match_version incremented means stG was 3
            # Check if win was already recorded for this match_version
            if room.last_win_version == room.match_version:
                continue  # Already recorded

            # Check if stG=3 just happened as a real phase end (not a zone-timer tick
            # through 3): engine bumps match_version and arms stg_is_phase_end only then.
            # Edge-triggered: the arm survives stG moving to 2/1 afterwards, so a batch
            # containing [3,2] still drains. Consumed on write; cleared on MATCH enter.
            if room.stg_is_phase_end and room.match_version > 0:
                # Match just ended — check who's alive to determine winner
                me_name_local = (view.me.username if view.me else None) or user.get("game_username")
                if me_name_local and room.state.value == "match":
                    # Check if I'm still alive
                    is_alive = False
                    for p in room.players.values():
                        if _names_equal(p.username, me_name_local) and p.is_alive:
                            is_alive = True
                            break

                    # Duo: whole team alive (incl. presumed-alive quiet members) counts as alive,
                    # so a dead member of the surviving team still gets the win
                    _ta_win = _team_alive_for_me(room, view, me_name_local)
                    if _ta_win is not None:
                        is_alive = _ta_win

                    if is_alive:
                        # I'm alive → WIN (helper marks version so it records once)
                        winner = me_name_local
                        if room.teams:
                            my_team = room.get_team(me_name_local)
                            if my_team:
                                winner = my_team.team_id
                        _write_win(discord_id, session_id, user, gm, room_id, room.short_code, room.match_version, winner, room)
                        room.stg_is_phase_end = False  # consume arm (version bump re-arms)

                    # Note: defeat is handled by alv/kill/leave logic below

    except Exception as exc:
        log_err(f"processor win failed {exc}")

    # --- RvB race to 15 team kills (tdm_bvr only) ---
    # First team to 15 kills wins the match. Counted from validated killfeed kills
    # attributed via _pt teams (knocks don't count). Personal deaths in RvB never
    # defeat (respawn mode, like DF) — only the team result writes win/defeat.
    try:
        for room_id4, room4 in list(view.rooms.items()):  # Iterate all rooms
            if _should_skip_room(room4):  # Skip lobby with no pending result
                continue
            gm4 = room4.game_mode.value if room4.game_mode else gm_val  # Room's game mode
            if not gm4 or str(gm4).lower() != "tdm_bvr":  # RvB only
                continue
            if room4.state.value != "match" or not room4.teams:  # Need match + teams
                continue
            me4 = (view.me.username if view.me else None) or user.get("game_username")
            if not me4:
                continue
            my_tid4 = None  # Resolve my team (exact first, normalized fallback)
            _exact = room4.get_team(me4)
            if _exact is not None:
                my_tid4 = _exact.team_id
            else:
                for tid, t in room4.teams.items():
                    if any(_names_equal(m, me4) for m in t.members):
                        my_tid4 = tid
                        break
            if my_tid4 is None:
                continue  # My team unknown — stG=3 fallback covers it
            counts: dict[str, int] = {}  # team_id -> kills this match
            for k in room4.recent_kills:
                if k.event_type != "killed":  # Only kills count toward 15
                    continue
                for tid, t in room4.teams.items():
                    if any(_names_equal(m, k.instigator) for m in t.members):
                        counts[tid] = counts.get(tid, 0) + 1
                        break  # Attribute to first matching team only
            my_k = counts.get(my_tid4, 0)  # My team's kills
            foe_best = max([c for tid, c in counts.items() if tid != my_tid4] + [0])
            if my_k >= 15 and room4.last_win_version != room4.match_version:
                _write_win(discord_id, session_id, user, gm4, room_id4, room4.short_code, room4.match_version, my_tid4, room4)
                log_sys(f"PROCESSOR WIN via race-to-15 team={my_tid4} {my_k}-{foe_best} gm={gm4}")
            elif foe_best >= 15 and not room4.had_self_defeat:
                _write_defeat(discord_id, session_id, user, gm4, room_id4, room4.short_code, room4.match_version, room4)
                log_sys(f"PROCESSOR DEFEAT via race-to-15 enemy hit 15 ({my_k}-{foe_best}) gm={gm4}")
    except Exception as exc:
        log_err(f"processor race-to-15 failed {exc}")

    # --- Defeats: check alv=false, kill victim, or leave ---
    # Processor uses alv fields directly (Engine just updates is_alive)
    try:
        for room_id2, room2 in list(view.rooms.items()):  # Iterate all rooms
            if _should_skip_room(room2):  # Skip lobby with no pending defeat
                continue
            gm2 = room2.game_mode.value if room2.game_mode else gm_val  # Room's game mode
            if gm2 and gm2.lower() == "df":  # DF has no defeat
                continue

            # End-of-match sweep: phase just ended and I'm dead in room state (covers
            # zone deaths with no killfeed/alv packets) — mark for drain below. Skipped
            # when a defeat was already recorded this life (no double defeat at match
            # end). Edge-triggered via stg_is_phase_end (a batch ending on stG=2 must
            # still sweep).
            me_name_local2 = (view.me.username if view.me else None) or user.get("game_username")
            if (room2.last_defeat_version != room2.match_version and not room2.had_self_defeat
                    and room2.stg_is_phase_end
                    and me_name_local2 and room2.state.value == "match" and room2.players_count > 1):
                # Check if I'm dead (solo rule)
                is_dead = False
                for p in room2.players.values():
                    if _names_equal(p.username, me_name_local2) and not p.is_alive:
                        is_dead = True
                        break

                # Duo: defeat only if whole team dead (quiet members presumed alive)
                _ta2 = _team_alive_for_me(room2, view, me_name_local2)
                if _ta2 is not None:
                    is_dead = not _ta2

                if is_dead:
                    room2.last_defeat_version = room2.match_version  # Mark for drain
                    log_sys(f"PROCESSOR marked defeat via stG room={room_id2[:8]} gm={gm2}")
                    # Reconcile a death the live paths missed (keeps deaths == defeats)
                    if not room2.had_self_death:
                        new_def_death = (user.get("deaths") or 0) + 1
                        supabase.table("bngc_users").update({"deaths": new_def_death}).eq("discord_id", discord_id).execute()
                        supabase.table("bngc_game_events").insert({
                            "discord_id": discord_id,
                            "session_id": session_id,
                            "event_type": "death",
                            "event_data": {"gm": gm2, "killer": None, "via": "stG",
                                           "room_id": room_id2, "code": room2.short_code, "match_version": room2.match_version},
                        }).execute()
                        log_sys(f"PROCESSOR DEATH via stG reconcile deaths {user.get('deaths',0)}->{new_def_death}")
                        user["deaths"] = new_def_death
                        room2.had_self_death = True
                        _precision.schedule("death", session_id=session_id, discord_id=discord_id,
                                            expected=new_def_death, match_version=room2.match_version,
                                            room_id=room_id2, extra={"via": "stG"})

            # Drain: single DB write point for defeats (kill/alv/stG markers set above;
            # leave writes immediately). One defeat per life — a stale marker for an
            # already-recorded defeat is discarded. A recorded win suppresses any defeat
            # for the same version (win stands). A marker held while a duo teammate
            # lives is kept (silent) until the team dies, then written.
            if room2.last_defeat_version == room2.match_version and room2.last_defeat_version != -1:
                if room2.had_self_defeat:
                    log_sys(f"PROCESSOR DEFEAT skip already recorded this life gm={gm2}")
                    room2.last_defeat_version = -1  # Discard stale marker
                elif room2.last_win_version == room2.match_version:
                    log_sys(f"PROCESSOR DEFEAT skip win already recorded gm={gm2}")
                    room2.last_defeat_version = -1  # Discard stale marker
                elif _team_alive_for_me(room2, view, me_name_local2) is True:
                    pass  # hold marker silently — teammate still alive, re-evaluated next batch
                else:
                    new_def = (user.get("defeats") or 0) + 1
                    supabase.table("bngc_users").update({"defeats": new_def}).eq("discord_id", discord_id).execute()
                    supabase.table("bngc_game_events").insert({
                        "discord_id": discord_id,
                        "session_id": session_id,
                        "event_type": "defeat",
                        "event_data": {"gm": gm2, "room_id": room_id2, "short_code": room2.short_code,
                                       "match_version": room2.match_version},
                    }).execute()
                    log_engine("defeat", session_id[:8], gm=gm2, duo=room2.is_duo, room=room_id2[:8])
                    log_sys(f"PROCESSOR DEFEAT gm={gm2} defeats {user.get('defeats',0)}->{new_def}")
                    user["defeats"] = new_def
                    room2.had_self_defeat = True  # One defeat per life
                    room2.last_defeat_version = -1  # Drain (prevent re-processing)
                    room2.stg_is_phase_end = False  # consume phase-end arm for this version
                    _precision.schedule("defeat", session_id=session_id, discord_id=discord_id,
                                        expected=new_def, match_version=room2.match_version,
                                        room_id=room_id2)

        # --- Competitive sealing (host collab only) ---
        # Only when host_mode is enabled and room is server-created (short_code)
        # Creates competitive_matches and matches records for replay
        try:
            for room_id3, room3 in view.rooms.items():  # Iterate all rooms
                if room3.room_id == "lobby":    # Skip lobby
                    continue
                gm3 = room3.game_mode.value if room3.game_mode else ""  # Room's game mode
                # Competitive conditions: host mode + zw_lg/wwzw/zw + server room + party + I'm host
                if is_host and gm3.lower() in ("zw_lg", "wwzw", "zw") and room3.short_code and room3.party.is_party and room3.me and room3.me.username == (view.me.username if view.me else None):
                    # Check if match just ended (win or defeat just drained)
                    is_ended = (room3.last_win_version == -1 and room3.last_defeat_version == -1)
                    try:
                        # Check if competitive record already exists
                        existing = supabase.table("competitive_matches").select("match_id").eq("match_id", f"{room_id3}_{room3.match_version}").limit(1).execute()
                        if not existing.data:    # No existing record → create
                            total_players = len(room3.players)  # Count players
                            total_teams = len(room3.teams) if room3.teams else 1  # Count teams
                            # Insert competitive match record
                            supabase.table("competitive_matches").insert({
                                "match_id": f"{room_id3}_{room3.match_version}",  # room_id_version
                                "host_pfid": view.me.game_id if view.me else None,  # Host's player ID
                                "verified_by_server": False,  # Not yet verified
                                "total_players": total_players,
                                "total_teams": total_teams,
                            }).execute()
                            # Upsert sealed match record
                            supabase.table("matches").upsert({
                                "match_id": f"{room_id3}_{room3.match_version}",
                                "match_type": "private_casual" if not room3.teams else "competitive",
                                "game_mode": room3.game_mode.value if room3.game_mode else "unknown",
                                "scene": room3.cur_scn,
                                "host_pfid": view.me.game_id if view.me else None,
                                "is_sealed": False,   # Not sealed yet (sealed on host upload)
                            }, on_conflict="match_id").execute()
                            log_sys(f"PROCESSOR competitive {room_id3[:8]} version={room3.match_version}")
                    except Exception as e:
                        log_err(f"processor competitive failed {room_id3[:8]} {e}")
        except Exception as e:
            log_err(f"processor competitive outer failed {e}")

        # --- Playtime sync ---
        # Aggregate per-room playtime into bngc_users and reset counters
        try:
            _sync_playtime(view, user, discord_id)  # Call helper
        except Exception as exc:
            log_err(f"processor playtime failed {exc}")

        # --- Sync to bngc_player_stats ---
        # Upsert stats table so SELECT * FROM bngc_player_stats is populated
        try:
            _upsert_stats(discord_id, user)  # Call helper
            log_sys(f"PROCESSOR sync player_stats kills={user.get('kills',0)} defeats={user.get('defeats',0)} wins={user.get('wins',0)}")
        except Exception as exc:
            log_err(f"processor player_stats sync failed {exc}")

    except Exception as exc:
        log_err(f"processor win/defeat failed {exc}")

    # Log final stats after processing all packets
    log_sys(f"PROCESSOR DONE session={session_id[:8]} kills={user.get('kills',0)} "
            f"deaths={user.get('deaths',0)} wins={user.get('wins',0)} defeats={user.get('defeats',0)}")
