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

import os
import time

# Supabase client for DB queries (table inserts/updates/queries)
from server.lib.supabase_client import supabase, db, db_io, is_transient_error

# Logging helpers: log_sys for info, log_err for errors, log_engine for pretty event lines
from server.lib.logger import log_sys, log_err, log_engine, log_ranked, log_dbg, log_comp, log_match

# Strip color tags from player names (e.g. "<color=red>Player</color>" → "Player")
from server.lib.gpbinary import _strip_color_tags

# Retransmit window shared with the engine (killfeed can arrive twice with
# different Photon framing: client RaiseEvent echo + server broadcast).
from server.lib.photon_engine.engine import _KILL_DUP_WINDOW_MS

# Precision tracking: each counted write is re-checked ~2s later (see precision.py)
from server.lib import precision as _precision

# A host whose own feed is this stale is not evidence of anything: nobody is
# talking, so phase-end results must not be rewritten (crash guard).
_STALE_HOST_SEC = 300.0

# Play-points economy values + hourly helper (all-modes activity ledger)
from server.lib.ranked import PLAY_KILL, PLAY_KNOCK, PLAY_WIN, hourly_play_points

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


def _last_seen_due(last_seen_raw, now_ts: float | None = None, max_age_s: int = 300) -> bool:
    """True when the last_seen heartbeat should refresh. Pure, unit-tested.

    Missing/unparseable timestamps always refresh (fail-open: a player with
    no heartbeat gets one). Otherwise refresh at most every max_age_s so an
    active session costs one tiny write per 5 minutes, not one per batch.
    """
    try:
        import time as _t
        now = float(now_ts) if now_ts is not None else _t.time()
    except Exception:
        return True
    try:
        if not last_seen_raw:
            return True
        from datetime import datetime
        ts = datetime.fromisoformat(str(last_seen_raw).replace("Z", "+00:00")).timestamp()
        return (now - ts) > int(max_age_s)
    except Exception:
        return True


def _names_equal(a, b) -> bool:
    """Strict (case-sensitive) comparison after stripping tags + dc: prefix.

    Solid primary: "Name" != "name" — twins and lookalikes never cross-credit
    here. Casing drift is handled one layer up by _names_equal_fold, which
    only credits when no twin conflict exists. Never raises.
    """
    try:
        na = _norm_name(a)              # Normalize first name
        nb = _norm_name(b)              # Normalize second name
        return bool(na and nb and na == nb)
    except Exception:
        return False


def _names_equal_fold(a, b) -> bool:
    """Open fallback: case-insensitive comparison after normalization.

    Catches killfeed casing drift ("ALICE" vs "alice") that strict misses.
    Callers must still twin-guard (see _twin_ambiguous_fold) so a genuine
    same-name pair can't miscredit. Never raises.
    """
    try:
        na = _norm_name(a).lower()
        nb = _norm_name(b).lower()
        return bool(na and nb and na == nb)
    except Exception:
        return False


def _leet_fold(s: str) -> str:
    """Leet-insensitive fold: map common gamer-tag digit substitutions so
    "Vo1d Eight" matches a registered "Void Eight" (1->i, 0->o).

    Narrow on purpose (only 1/0): wider maps (3->e, 4->a, 5->s, 7->t)
    conflate genuinely different names. Never raises.
    """
    try:
        return (str(s or "").lower().replace("1", "i").replace("0", "o"))
    except Exception:
        return ""


def _names_equal_leet(a, b) -> bool:
    """Leet fallback: leet-folded comparison after normalization.

    Catches digit-substituted tags ("VO1D" vs "Void") that strict+fold
    miss. Callers must twin-guard (see _twin_ambiguous_leet). Never raises.
    """
    try:
        na = _leet_fold(_norm_name(a))
        nb = _leet_fold(_norm_name(b))
        return bool(na and nb and na == nb)
    except Exception:
        return False


def _self_match_tier(room, view, user, name) -> int:
    """Attribution tier for one killfeed name: 0 none, 1 exact, 2 strict,
    3 fold (casing drift), 4 game-ID, 5 leet (digit-substituted tags).
    Pure, unit-tested. Never raises.

    Exposed so the kill path can log WHEN the open fallback saves a player
    (tier 3) instead of silently crediting — that log line is the early
    warning before anyone sits at 0 kills for hours.
    """
    try:
        if not name:
            return 0
        me_name = None
        try:
            me_name = (view.me.username if view and view.me else None) or (
                user.get("game_username") if isinstance(user, dict) else None)
        except Exception:
            me_name = None
        if me_name:
            try:
                if str(name or "").strip() == str(me_name or "").strip():
                    return 1
            except Exception:
                pass
            if _names_equal(name, me_name) and not _twin_ambiguous(room, user, name):
                return 2
            if _names_equal_fold(name, me_name) and not _twin_ambiguous_fold(room, user, name):
                return 3
            if _names_equal_leet(name, me_name) and not _twin_ambiguous_leet(room, user, name):
                return 5
        try:
            ugid = str((user.get("game_id") if isinstance(user, dict) else None) or "").strip()
        except Exception:
            ugid = ""
        if not ugid or room is None:
            return 0
        try:
            players = getattr(room, "players", None) or {}
        except Exception:
            return 0
        for p in players.values():
            try:
                pun = getattr(p, "username", None)
                if not pun or not (_names_equal(pun, name) or _names_equal_fold(pun, name)
                                   or _names_equal_leet(pun, name)):
                    continue
                if str(getattr(p, "game_id", "") or "").strip() == ugid:
                    return 4
            except Exception:
                continue
        return 0
    except Exception:
        return 0


def _is_self(room, view, user, name) -> bool:
    """Is this killfeed name the session owner? Tiered: solid first, open fallback.

    Tier 1 (exact raw): byte-identical names credit — unambiguous, even with twins.
    Tier 2 (strict norm + twin guard): tags/dc: stripped, case-sensitive; blocked
      only when 2+ roster entries share the name and gid can't disambiguate.
    Tier 3 (fold + twin guard): case-insensitive catch for killfeed casing drift
      ("ALICE" vs "alice") — this is what kept players at 0 kills for hours while
      playtime counted. Blocked only on genuine fold-level twin conflict.
    Tier 4 (game-ID): stable pfid through the room roster (strict, fold or leet).
    Tier 5 (leet + twin guard): digit-substituted tags ("VO1D" vs "Void",
      1->i / 0->o only) — blocked on genuine leet-level twin conflict.
    Never raises.
    """
    try:
        return _self_match_tier(room, view, user, name) > 0
    except Exception:
        return False


def _twin_ambiguous(room, user, name) -> bool:
    """True when 2+ roster entries share this strict-normalized name and the
    user's game_id doesn't single one out. Pure helper. Never raises."""
    try:
        players = getattr(room, "players", None) or {} if room is not None else {}
        mates = [p for p in players.values()
                 if getattr(p, "username", None) and _names_equal(getattr(p, "username"), name)]
        if len(mates) < 2:
            return False
        try:
            ugid = str((user.get("game_id") if isinstance(user, dict) else None) or "").strip()
        except Exception:
            ugid = ""
        if ugid and any(str(getattr(p, "game_id", "") or "").strip() == ugid for p in mates):
            return False
        return True
    except Exception:
        return False


def _twin_ambiguous_fold(room, user, name) -> bool:
    """Twin check at fold (case-insensitive) level for the Tier-3 fallback.
    Same rule as _twin_ambiguous, just wider net. Never raises."""
    try:
        players = getattr(room, "players", None) or {} if room is not None else {}
        mates = [p for p in players.values()
                 if getattr(p, "username", None) and _names_equal_fold(getattr(p, "username"), name)]
        if len(mates) < 2:
            return False
        try:
            ugid = str((user.get("game_id") if isinstance(user, dict) else None) or "").strip()
        except Exception:
            ugid = ""
        if ugid and any(str(getattr(p, "game_id", "") or "").strip() == ugid for p in mates):
            return False
        return True
    except Exception:
        return False


def _twin_ambiguous_leet(room, user, name) -> bool:
    """Twin check at leet level for the Tier-5 fallback (1->i / 0->o).
    Same rule as _twin_ambiguous, widest net — a genuine leet-level twin
    conflict (two roster rows folding to the same tag) blocks the credit
    so lookalikes never cross-credit. Never raises."""
    try:
        players = getattr(room, "players", None) or {} if room is not None else {}
        mates = [p for p in players.values()
                 if getattr(p, "username", None) and _names_equal_leet(getattr(p, "username"), name)]
        if len(mates) < 2:
            return False
        try:
            ugid = str((user.get("game_id") if isinstance(user, dict) else None) or "").strip()
        except Exception:
            ugid = ""
        if ugid and any(str(getattr(p, "game_id", "") or "").strip() == ugid for p in mates):
            return False
        return True
    except Exception:
        return False


def _heal_view_me(view, user):
    """Adopt session identity from the roster when view.me never resolved.

    Null-identity class of bug: join props arrived without a 255 username
    (or under a drifting name), so view.me stays None while playtime still
    counts — kills/deaths/wins silently miss for the whole session ("player
    ignored" despite registering). The DB game_username (seeded at
    /register) is the source of truth: when exactly one roster row matches
    it through the attribution tiers (name OR stable game_id), adopt that
    row as view.me so this batch credits correctly.

    Only ever fills a missing view.me (never overwrites), prefers an exact
    roster hit, and stays silent when ambiguous (twins) or absent — callers
    keep the old me=? behavior there. Returns True when adopted. Never raises.
    """
    try:
        if view is None or getattr(view, "me", None) is not None:
            return False
        try:
            want = (user.get("game_username") if isinstance(user, dict) else None) or ""
            ugid = str((user.get("game_id") if isinstance(user, dict) else None) or "").strip()
        except Exception:
            want, ugid = "", ""
        if not (str(want or "").strip() or ugid):
            return False
        try:
            rooms = list((getattr(view, "rooms", None) or {}).values())
        except Exception:
            rooms = []
        for room in rooms:
            try:
                players = getattr(room, "players", None) or {}
            except Exception:
                continue
            for p in players.values():
                try:
                    pun = getattr(p, "username", None)
                    if not pun:
                        continue
                    hit = False
                    try:
                        hit = _is_self(room, view, user, pun)
                    except Exception:
                        hit = False
                    if not hit:
                        continue
                    try:
                        p.is_self = True
                    except Exception:
                        pass
                    try:
                        view.me = p
                    except Exception:
                        return False
                    try:
                        if getattr(room, "me", None) is None:
                            room.me = p
                    except Exception:
                        pass
                    try:
                        log_sys(f"PROCESSOR identity healed me={pun} via roster "
                                f"(tier={_self_match_tier(room, view, user, pun)})")
                    except Exception:
                        pass
                    return True
                except Exception:
                    continue
        return False
    except Exception:
        return False


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


def _self_death_evidence(room, view, me_name) -> bool:
    """Monotonic self-death evidence for one match: per-life flag, killed-me in
    the killfeed, or self tracked dead. Deaths are loud (killfeed + alv always
    broadcast), so NO evidence here means survived — this is what lets a phase
    end resolve when the room has already emptied (no live players to judge)."""
    try:
        if room is not None and getattr(room, "had_self_death", False):
            return True
        if me_name:
            for k in (getattr(room, "recent_kills", None) or []):
                if k.event_type == "killed" and _names_equal(k.instigated, me_name):
                    return True
            for cand in (getattr(room, "me", None), getattr(view, "me", None)):
                if cand is not None and _names_equal(cand.username, me_name) and not cand.is_alive:
                    return True
            for p in (getattr(room, "players", None) or {}).values():
                if _names_equal(p.username, me_name) and not p.is_alive:
                    return True
    except Exception:
        pass
    return False


def _alive_now(room, p) -> bool:
    """Is this roster player genuinely alive RIGHT NOW?

    `is_alive` alone lies after a crash: the row keeps the last value forever
    because the player stopped sending packets, so a phantom survivor would
    win the phase end. A player is only trusted while their last_seen_at is
    fresh. Unknown stamp (old cache) is trusted — never take a real win away —
    and the rule switches off entirely when the host's own feed is dead.
    Never raises.
    """
    try:
        if p is None or not getattr(p, "is_alive", False):
            return False
        seen = getattr(p, "last_seen_at", None)
        if seen is None:
            return True
        host = getattr(room, "last_packet_at", None)
        if host is not None and (time.time() - float(host)) > _STALE_HOST_SEC:
            return True  # nobody is talking: leave history alone
        return (time.time() - float(seen)) <= _stale_alive_sec()
    except Exception:
        return bool(getattr(p, "is_alive", False))


def _stale_alive_sec() -> float:
    """Seconds a player may be silent before a phase end stops trusting them."""
    try:
        return float(os.getenv("PHASE_STALE_ALIVE_SEC", "180") or 180)
    except Exception:
        return 180.0


def _stale_alive_evidence(room, me_name) -> bool:
    """The roster says this player is alive, but their feed stopped (crash).

    A crasher must not win the phase end, and they must not be treated as
    "survived with no information" either — that fallback is what handed a
    free round to somebody who was no longer in the game. Never raises.
    """
    try:
        if room is None or not me_name:
            return False
        for p in (getattr(room, "players", None) or {}).values():
            if not _names_equal(getattr(p, "username", None), me_name):
                continue
            return bool(getattr(p, "is_alive", False)) and not _alive_now(room, p)
        return False
    except Exception:
        return False


def _phase_end_result(room, view, me_name) -> str | None:
    """Decide a real phase end (stG=3): 'win' or None (no win).

    Wins-only: defeats are deleted, so non-win outcomes resolve to None and
    the seal-only drain still seals ranked. Same generosity as before (quiet
    teammates presumed alive; zero-info fallback is 'win': the arm existing
    means our client saw this room's match end while connected, and deaths
    are loud — no death evidence anywhere means survived).
    """
    try:
        if not room or not me_name:
            return None
        alive = False
        try:
            for p in room.players.values():
                if _names_equal(p.username, me_name) and _alive_now(room, p):
                    alive = True
                    break
        except Exception:
            pass
        team = _team_alive_for_me(room, view, me_name)
        if alive or team is True:
            return "win"
        if _self_death_evidence(room, view, me_name):
            return None
        if _stale_alive_evidence(room, me_name):
            # Roster says alive, feed says gone: a crash, not a survivor.
            return None
        if team is False:
            return None
        # Leavers never win via phase end: quitting exits to LOBBY, and the
        # zero-info fallback below must not credit it. Absent-from-roster +
        # a leave event = left (rejoining re-adds the row, so later matches
        # are unaffected); every other absent shape keeps old behavior.
        try:
            _present = False
            for p in room.players.values():
                if _names_equal(p.username, me_name):
                    _present = True
                    break
            if not _present:
                for k in (getattr(room, "recent_kills", None) or []):
                    if getattr(k, "event_type", None) == "left" and _names_equal(getattr(k, "instigator", None), me_name):
                        return None
        except Exception:
            pass
        return "win"
    except Exception:
        pass
    return None


def _phase_end_debug(room, view, me_name) -> str:
    """Why did _phase_end_result deny the win? Single-line reason. Pure (logs nothing)."""
    try:
        if not room:
            return "no-room"
        if not me_name:
            return "no-identity(view.me null + game_username null)"
        parts = []
        try:
            parts.append(f"ver={getattr(room, 'match_version', '?')}")
            parts.append(f"lastwin={getattr(room, 'last_win_version', '?')}")
            parts.append(f"state={getattr(getattr(room, 'state', None), 'value', getattr(room, 'state', '?'))}")
        except Exception:
            pass
        try:
            me_p = None
            for p in (getattr(room, "players", None) or {}).values():
                if _names_equal(p.username, me_name):
                    me_p = p
                    break
            alive = _alive_now(room, me_p)
            parts.append(f"alive_in_roster={alive}")
            # A survivor whose feed stopped is the crash case: say so loudly.
            try:
                _stale = bool(me_p is not None and getattr(me_p, "is_alive", False)
                              and not alive)
                if _stale:
                    parts.append(f"stale_alive=True seen_ago="
                                 f"{int(time.time() - float(me_p.last_seen_at))}s")
            except Exception:
                pass
        except Exception:
            parts.append("alive_in_roster=?")
        try:
            parts.append(f"team={_team_alive_for_me(room, view, me_name)}")
        except Exception:
            parts.append("team=?")
        try:
            parts.append(f"death_ev={_self_death_evidence(room, view, me_name)}")
        except Exception:
            parts.append("death_ev=?")
        try:
            present = any(_names_equal(p.username, me_name) for p in room.players.values())
            left = False
            if not present:
                for k in (getattr(room, "recent_kills", None) or []):
                    if getattr(k, "event_type", None) == "left" and _names_equal(getattr(k, "instigator", None), me_name):
                        left = True
                        break
            parts.append(f"in_roster={present} left_row={left}")
        except Exception:
            parts.append("roster=?")
        try:
            parts.append(f"had_self_death={bool(getattr(room, 'had_self_death', False))}")
        except Exception:
            pass
        return " ".join(parts) if parts else "unknown"
    except Exception:
        return "debug-failed"


def _defeat_fields(room, view, me_name, gm=None) -> dict | None:
    """Field set for a DEFEAT line when the tracked player LOST a real match.

    Wins-only by design: no defeat row is ever written, so without this the
    console never said who lost a round — the denial just read "WIN denied".
    Only a positive elimination counts (dead at the phase end with death
    evidence): a leave, a walkover or an already-emptied room is unknown, not
    a defeat. `killer` is the last player who killed us in this match, which
    in a 1v1 is the opponent. Returns None when there is nothing to log.
    Never raises.
    """
    try:
        if not room or not me_name:
            return None
        alive = any(_names_equal(p.username, me_name) and p.is_alive
                    for p in (getattr(room, "players", None) or {}).values())
        if alive or not _self_death_evidence(room, view, me_name):
            return None
        killer = None
        for k in (getattr(room, "recent_kills", None) or []):
            if getattr(k, "event_type", None) != "killed":
                continue
            if _names_equal(getattr(k, "instigated", None), me_name):
                killer = getattr(k, "instigator", None) or None
        return {
            "me": me_name,
            "killer": killer,
            "gm": gm or getattr(getattr(room, "game_mode", None), "value", None),
            "room": str(getattr(room, "room_id", "") or "")[:8] or None,
            "code": getattr(room, "short_code", None) or None,
            "version": getattr(room, "match_version", None),
        }
    except Exception:
        return None


def _is_kd_only(gm) -> bool:
    """Respawn modes: K/D tracked, no personal defeat (DF free mode, RvB TDM).
    Wins/defeats in RvB come only from the team-15 race or stG=3 team result.
    """
    return str(gm or "").lower() in ("df", "tdm_bvr")


def _eff_is_duo(room) -> bool:
    """Effective duo: gv flag OR party-of-2 fallback.

    Private zw_lg duos often arrive without gv (default solo) and without
    _pt teams, so room.is_duo stays False and defeat-now degrades to solo
    rules (alive winner saved as defeat). Treat as duo when an explicit
    2-member team exists or the party room holds 2+ tracked players.
    Never raises.
    """
    try:
        if room is None:
            return False
        try:
            if bool(getattr(room, "is_duo", False)):
                return True
        except Exception:
            pass
        try:
            for t in (getattr(room, "teams", None) or {}).values():
                if len(getattr(t, "members", []) or []) == 2:
                    return True
        except Exception:
            pass
        try:
            party = getattr(room, "party", None)
            if party is not None and bool(getattr(party, "is_party", False)):
                try:
                    n = len(getattr(room, "players", None) or {})
                except Exception:
                    n = 0
                if n >= 2:
                    return True
        except Exception:
            pass
    except Exception:
        pass
    return False


def _kill_count_dup(room, killer, victim, kind, pkt_t) -> bool:
    """Retransmit guard: same killer->victim kind already counted within the
    window, scoped per room+match_version so suppression can never leak across
    matches (the "0 kills for hours" class). Photon retransmits arrive with
    the same server timestamp, so pkt_t-keyed comparison is exact; packets
    without a timestamp fall back to wall-clock — still scoped, so the worst
    case is one same-pair re-kill inside 5s in the same match being skipped.
    State lives on the room (survives batches via Upstash)."""
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
        try:
            _scope = f"{getattr(room, 'room_id', '')}\x00{getattr(room, 'match_version', 0)}"
        except Exception:
            _scope = ""
        key = f"{_scope}\x00{_norm_name(killer)}\x00{_norm_name(victim)}\x00{kind}"
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
                # The victim respawning after the counted kill means this is a
                # NEW life, not a retransmit. Without this, Free Mode (version
                # never moves, ~3s respawns) silently dropped every second
                # kill of the same victim.
                try:
                    _alive = getattr(room, "proc_alive_at", None)
                    if isinstance(_alive, dict):
                        _an = _norm_name(victim)
                        if _an and float(_alive.get(_an) or 0) > float(last):
                            return False
                except Exception:
                    pass
                return True
        except Exception:
            pass
    except Exception:
        pass
    return False


def _kill_count_stamp(room, killer, victim, kind, pkt_t) -> None:
    """Record a counted kill for the retransmit guard above, scoped per
    room+match_version (mirrors _kill_count_dup). Timestamp-less packets use
    wall-clock so the guard still catches echo pairs."""
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
        try:
            _scope = f"{getattr(room, 'room_id', '')}\x00{getattr(room, 'match_version', 0)}"
        except Exception:
            _scope = ""
        key = f"{_scope}\x00{_norm_name(killer)}\x00{_norm_name(victim)}\x00{kind}"
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


def _buf_event(buf, discord_id, session_id, event_type, event_data):
    """Queue one bngc_game_events row for the batch-end bulk insert.

    When buf is None the row is inserted directly (old behavior, kept for
    callers outside process_batch). Never raises.

    zone_change rows are retired: no writer produces them anymore (legacy
    volume with zero consumers) — drop them here so they can never return.
    """
    if event_type == "zone_change":
        return
    row = {"discord_id": discord_id,       # Which Discord user owns this session
           "session_id": session_id,       # Which session
           "event_type": event_type,       # Event type for querying
           "event_data": event_data}       # Event details
    try:
        if buf is None:
            supabase.table("bngc_game_events").insert(row).execute()
        else:
            buf.append(row)
    except Exception as exc:
        log_err(f"processor event write failed {event_type}: {exc}")


def _flush_events(buf, chunk: int = 50):
    """Bulk-insert queued game events in ONE query. Never raises.

    Returns (flushed, dropped): on bulk failure retries in chunks, then
    row-by-row inside the failing chunk, so ONE poison row can never nuke
    the whole batch silently. Dropped rows are logged with their event_type.
    The buffer is always drained (never requeued across batches).
    """
    try:
        rows = list(buf or [])
    except Exception:
        rows = []
    try:
        buf.clear()
    except Exception:
        pass
    if not rows:
        return 0, 0
    try:
        supabase.table("bngc_game_events").insert(rows).execute()
        return len(rows), 0
    except Exception as bulk_exc:
        pass
    flushed, dropped = 0, 0
    try:
        step = max(1, int(chunk or 50))
        for i in range(0, len(rows), step):
            piece = rows[i:i + step]
            try:
                supabase.table("bngc_game_events").insert(piece).execute()
                flushed += len(piece)
                continue
            except Exception:
                pass
            for r in piece:
                try:
                    supabase.table("bngc_game_events").insert(r).execute()
                    flushed += 1
                except Exception as row_exc:
                    dropped += 1
                    try:
                        log_err(f"processor event row dropped type={(r or {}).get('event_type')}: {row_exc}")
                    except Exception:
                        pass
        try:
            log_err(f"processor bulk event flush split {flushed}/{len(rows)} ok, {dropped} dropped")
        except Exception:
            pass
        return flushed, dropped
    except Exception as exc:
        try:
            log_err(f"processor event flush failed ({len(rows)} rows): {exc}")
        except Exception:
            pass
        return 0, len(rows)


def _flush_user_counters(discord_id, pending_user_updates, dirty, user, attempts=3) -> bool:
    """Persist counter + field updates in ONE bngc_users UPDATE, retried.

    A win row and its counter increment flush in back-to-back queries; a
    blip between them used to save the row while losing the counter
    ("wins not saved"). Retrying here closes that window. Returns True
    when persisted. Never raises (runs in a db_io worker thread; blocking
    backoff sleep is fine there).
    """
    try:
        final_user_update = dict(pending_user_updates or {})
        for _k in (dirty or set()):
            try:
                final_user_update[_k] = user.get(_k)
            except Exception:
                continue
        if not final_user_update:
            return True
        last_exc = None
        for _a in range(max(1, int(attempts or 1))):
            try:
                supabase.table("bngc_users").update(
                    final_user_update).eq("discord_id", discord_id).execute()
                if _a:
                    try:
                        log_sys(f"PROCESSOR flush user {discord_id[:6]} ok on retry {_a}")
                    except Exception:
                        pass
                else:
                    try:
                        log_sys(f"PROCESSOR flush user {discord_id[:6]} {final_user_update}")
                    except Exception:
                        pass
                return True
            except Exception as exc:
                last_exc = exc
                try:
                    import time as _t
                    _t.sleep(0.25 * (_a + 1))
                except Exception:
                    pass
        log_err(f"processor user flush failed {last_exc}")
        return False
    except Exception as exc:
        log_err(f"processor user flush failed {exc}")
        return False


def _bump_user(user, dirty, key, delta=1):
    """Increment an in-memory user counter, marking it for the batch-end
    single UPDATE. Returns the new value. Never raises."""
    try:
        new_v = (user.get(key) or 0) + delta
    except Exception:
        new_v = delta
    try:
        user[key] = new_v
        if dirty is not None:
            dirty.add(key)
    except Exception:
        pass
    return new_v


def _result_event_exists(session_id, event_type, room_id, match_ver, buf=None) -> bool:
    """Idempotency guard: was this match result already written?

    Retried/requeued batches re-run the drain with restored (marker-less)
    state and would otherwise double-record wins/defeats. Kills are excluded
    on purpose: respawn modes legitimately repeat same-victim kills, and the
    time-window retransmit guard already covers those. Fail-open (False) on
    read errors so a blip never blocks a legitimate first write.

    buf: batch event buffer — a row queued but not yet flushed counts as
    written, so two triggers inside one batch can't double-record.
    """
    try:
        if buf:
            for r in buf:
                try:
                    if r.get("session_id") != session_id or r.get("event_type") != event_type:
                        continue
                    ed = r.get("event_data") or {}
                    if str(ed.get("room_id") or "") == str(room_id or "") and ed.get("match_version") == match_ver:
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    try:
        res = supabase.table("bngc_game_events").select("event_data").eq(
            "session_id", session_id).eq(
            "event_type", event_type).order("created_at", desc=True).limit(20).execute()
        for r in (res.data or []):
            ed = (r or {}).get("event_data") or {}
            if str(ed.get("room_id") or "") == str(room_id or "") and ed.get("match_version") == match_ver:
                return True
    except Exception as exc:
        log_err(f"processor result-exists check failed {exc}")
    return False


def _aligned_match_version(room_ver, sealed_count):
    """Session-continuous version for a fresh Photon room. Pure helper.

    match_version restarts at 0 per Photon room, but seals are keyed
    (room_id, match_version) per RANKED session — without this, every new
    Photon room re-seals v1, v2... and collides with already-sealed rows
    (skipped as duplicates, so no points are ever granted again).
    Returns sealed_count when the room is still at v0 with seals behind
    it, else the room version untouched. Never raises.
    """
    try:
        rv = int(room_ver or 0)
        sc = int(sealed_count or 0)
    except Exception:
        return room_ver
    if rv == 0 and sc > 0:
        return sc
    return rv


def _sealed_count(rid) -> int:
    """COUNT ranked_results rows for a room. 0 on any error. Never raises."""
    try:
        if not rid:
            return 0
        res = supabase.table("ranked_results").select("match_version").eq("room_id", str(rid)).execute()
        return len(res.data or [])
    except Exception:
        return 0


def _write_win(discord_id, session_id, user, gm, room_id, short_code, match_ver, winner, room,
               buf=None, dirty=None):
    """Queue one win (bngc_users counter + bngc_game_events row) + log lines.
    The counter UPDATE and event INSERT are flushed once at batch end —
    pass the process_batch buf/dirty pair. Marks room.last_win_version
    (NOT drained — version bump re-enables next match). Wins-only: no defeats.
    """
    if _result_event_exists(session_id, "win", room_id, match_ver, buf):
        log_sys(f"PROCESSOR WIN skip already recorded gm={gm} room={str(room_id or '')[:8]} ver={match_ver}")
        if room is not None:
            room.last_win_version = match_ver  # heal marker so it isn't retried
        return user.get("wins") or 0
    new_wins = _bump_user(user, dirty, "wins")  # Increment (flushed at batch end)
    _bump_user(user, dirty, "play_points", PLAY_WIN)  # Play points: +per win (defeats pay nothing)
    if buf is None:
        supabase.table("bngc_users").update({"wins": new_wins}).eq("discord_id", discord_id).execute()
    _buf_event(buf, discord_id, session_id, "win",
               {"gm": gm, "room_id": room_id, "short_code": short_code,
                "match_version": match_ver, "winner": winner})
    try:
        _duo = bool(room.is_duo) if room is not None else False
    except Exception:
        _duo = False
    log_match("WIN", tag="WIN", event="win", session=session_id,
              did=str(discord_id or "")[:9], winner=winner, gm=gm,
              room=str(room_id or "")[:8], code=short_code or None,
              version=match_ver, duo=_duo)
    log_sys(f"PROCESSOR WIN gm={gm} winner={winner} wins {user.get('wins',0)}->{new_wins}")
    user["wins"] = new_wins             # Update local cache
    if room is not None:
        room.last_win_version = match_ver  # Mark recorded (version bump re-enables)
    _precision.schedule("win", session_id=session_id, discord_id=discord_id,
                        expected=new_wins, match_version=match_ver, room_id=room_id)
    return new_wins


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
    # Hourly play points: owed = (full hours now - hours already paid) * rate.
    # Checkpoint only advances, so replays/splits can never double-pay.
    hourly_owed, hourly_paid = hourly_play_points(new_total, user.get("play_points_hours_paid"))
    new_play = (user.get("play_points") or 0) + hourly_owed
    # Write to DB
    supabase.table("bngc_users").update({
        "playtime_global": new_total,           # Updated global playtime
        "playtime_per_gm": json.dumps(cur_per), # Updated per-GM JSON
        "play_points": new_play,                # Hourly accrual included
        "play_points_hours_paid": hourly_paid,  # Checkpoint advanced
    }).eq("discord_id", discord_id).execute()   # Where discord_id matches
    if hourly_owed:
        log_sys(f"PROCESSOR play_points hourly +{hourly_owed} (hours paid -> {hourly_paid})")
        try:
            user["play_points"] = new_play
            user["play_points_hours_paid"] = hourly_paid
        except Exception:
            pass
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
# v2.19.0 streak rewards — daily / win / kill (batch-end, never raises)
#
# v1 SHIP SWITCH: streak counters are tracked (drawer display) but NEVER
# paid into the ladder — seals are the only ladder movers. Set
# PAY_STREAK_BONUS = True to re-enable post-launch (daily + win + kill
# threshold bonuses from server.lib.streaks).
# ---------------------------------------------------------------------------
PAY_STREAK_BONUS = False
# ---------------------------------------------------------------------------

def _apply_streaks(user, dirty, ev_rows, view=None) -> int:
    """Update streak counters in-memory + queue for flush. Returns bonus earned.

    Daily: first batch per UTC day advances (continue on 1-day gap, reset
    otherwise; same-day re-batch pays nothing). Win: any win event bumps
    win_streak (defeat resets, best kept). Kill: per-MATCH self-kill total vs
    best_match_kills (room.my_kills_by_version accumulates across batches;
    batch count is only a fallback for untracked rooms), threshold bonus once
    per batch on the batch count. Missing columns (pre-migration rows) default
    to 0. Never raises.
    """
    bonus = 0
    try:
        from datetime import date as _date
        from server.lib import streaks as _st
    except Exception:
        return 0
    try:
        kinds = set()
        kills_batch = 0
        for e in (ev_rows or []):
            try:
                t = e.get("event_type") if isinstance(e, dict) else None
                if t:
                    kinds.add(t)
                if t == "kill":
                    kills_batch += 1
            except Exception:
                continue
        # daily
        try:
            today = _date.today()
            lc = user.get("last_daily_claim")
            if str(lc) != str(today):
                if lc in (None, ""):
                    ns, b = 1, _st.DAILY_BONUS.get(1, 0)
                else:
                    try:
                        from datetime import date as _d2
                        lcd = _d2.fromisoformat(str(lc)[:10])
                        gap = (today - lcd).days
                    except Exception:
                        gap = 99
                    if gap <= 0:
                        ns, b = int(user.get("daily_streak") or 0), 0
                    elif gap == 1:
                        ns, b = _st.daily_continue(int(user.get("daily_streak") or 0))
                    else:
                        ns, b = 1, _st.DAILY_BONUS.get(1, 0)
                user["daily_streak"] = ns
                user["last_daily_claim"] = str(today)
                dirty.add("daily_streak")
                dirty.add("last_daily_claim")
                bonus += int(b or 0)
        except Exception:
            pass
        # win / defeat
        try:
            if "win" in kinds:
                ns = int(user.get("win_streak") or 0) + 1
                user["win_streak"] = ns
                dirty.add("win_streak")
                if ns > int(user.get("best_win_streak") or 0):
                    user["best_win_streak"] = ns
                    dirty.add("best_win_streak")
                bonus += int(_st.win_bonus(ns) or 0)
            elif "defeat" in kinds and int(user.get("win_streak") or 0) != 0:
                user["win_streak"] = 0
                dirty.add("win_streak")
        except Exception:
            pass
        # kill: per-match total (not per-batch — batches split a match across POSTs)
        try:
            _match_best = int(kills_batch or 0)
            try:
                for _r in (getattr(view, "rooms", None) or {}).values():
                    for _v in (getattr(_r, "my_kills_by_version", None) or {}).values():
                        if int(_v or 0) > _match_best:
                            _match_best = int(_v or 0)
            except Exception:
                pass
            if _match_best > int(user.get("best_match_kills") or 0):
                user["best_match_kills"] = _match_best
                dirty.add("best_match_kills")
            bonus += int(_st.kill_bonus(kills_batch) or 0)
        except Exception:
            pass
    except Exception:
        pass
    return bonus


# ---------------------------------------------------------------------------
# Ranked sealing (host authority) — kills x8 + alive +50 / dead -5.
#
# Split authority, matching the match flow:
#   - kills come from the HOST feed (host sees every killfeed entry);
#   - win/loss comes from each reporter's own alv (host self-damages when
#     hosting_only, so he reads dead and is skipped from awards entirely).
# One writer (host) per (room, version); seal_match is idempotent so
# retries and multi-trigger drains collapse to a single award.
# ---------------------------------------------------------------------------

def _resolve_ranked_dids(usernames) -> dict:
    """Map game usernames -> discord_ids via bngc_users (single batched query).

    Unmapped names are absent from the result (fail-open: they become
    non-roster warnings, never fake points). Never raises.
    """
    try:
        names = sorted({str(n).strip() for n in (usernames or []) if str(n or "").strip()})
    except Exception:
        return {}
    if not names:
        return {}
    try:
        res = supabase.table("bngc_users").select("discord_id,game_username").in_("game_username", names[:64]).execute()
        out = {}
        for row in (res.data or []):
            try:
                if row.get("game_username") and row.get("discord_id"):
                    out[str(row["game_username"])] = str(row["discord_id"])
            except Exception:
                continue
        return out
    except Exception as exc:
        log_err(f"processor ranked did-resolve failed {exc}")
        return {}


def _roster_name2did(room_data, room) -> dict:
    """Map roster game usernames -> discord_ids. Never raises.

    Roster entries written by the bot carry game_username; anything missing
    is resolved via one bngc_users query for the names the host actually saw
    (room.players + killfeed). Unmapped names stay absent (no fake points).
    """
    out: dict = {}
    try:
        for entry in (room_data.get("roster") or []):
            try:
                uname = entry.get("game_username") or entry.get("username")
                did = entry.get("discord_id")
                if uname and did:
                    out[str(uname)] = str(did)
            except Exception:
                continue
    except Exception:
        pass
    try:
        missing = []
        try:
            for p in (getattr(room, "players", None) or {}).values():
                uname = getattr(p, "username", None)
                if uname and str(uname) not in out and str(uname) not in missing:
                    missing.append(str(uname))
        except Exception:
            pass
        try:
            for k in (getattr(room, "recent_kills", None) or []):
                for uname in (getattr(k, "instigator", None), getattr(k, "instigated", None)):
                    if uname and str(uname) not in out and str(uname) not in missing:
                        missing.append(str(uname))
        except Exception:
            pass
        if missing:
            out.update(_resolve_ranked_dids(missing))
    except Exception:
        pass
    return out


_comp_logged_codes: set = set()
_comp_seen: dict = {}  # mid -> set[(etype, killer, victim)] banked, normalized keys
_comp_namemap: dict = {}  # game_username -> discord_id (positives only; twins stay None)

_rt_logged_codes: set = set()
_rt_seen: dict = {}  # mid -> set[(etype, killer, victim)] banked, normalized keys
_rt_host_warned: set = set()  # (session, code) already told they are not host


def _comp_norm_key(name) -> str:
    """Normalized tourney dedup/match key: tags + dc: stripped, case-folded."""
    try:
        return _norm_name(name).lower()
    except Exception:
        return ""


def _comp_map_names(names: list) -> dict:
    """Batch game_username → discord_id. Exact-first, fold-fallback, twins None.

    Killfeed names drift (case, color tags, dc: prefix) while bngc_users stores
    clean names — same tier rule as lifetime _is_self: a byte-exact single
    owner wins; otherwise a normalized (tags/dc stripped, case-folded) single
    owner wins; genuine conflicts stay None for the dashboard /link flow.
    Only positive hits are cached, so a mid-tourney /link heals on the next
    batch instead of sticking until process restart. Never raises.
    """
    try:
        want = sorted({str(n) for n in (names or []) if str(n or "").strip()})
    except Exception:
        return {}
    out: dict = {}
    pending: list = []
    for n in want:
        try:
            hit = _comp_namemap.get(n)
        except Exception:
            hit = None
        if hit:
            out[n] = hit
        else:
            pending.append(n)
    if not pending or supabase is None:
        return {n: out.get(n) for n in want}
    try:
        ures = supabase.table("bngc_users").select(
            "discord_id,game_username").in_("game_username", pending[:50]).execute()
        by_game: dict = {}
        for r in (ures.data or []):
            try:
                g, d = r.get("game_username"), r.get("discord_id")
                if g and d:
                    by_game.setdefault(g, set()).add(d)
            except Exception:
                continue
        still: list = []
        for n in pending:
            try:
                dids = by_game.get(n) or set()
                if len(dids) == 1:
                    did = next(iter(dids))
                    _comp_namemap[n] = did
                    out[n] = did
                else:
                    still.append(n)
            except Exception:
                still.append(n)
    except Exception:
        still = list(pending)
    # Fold fallback (capped per batch; uncached misses retry next batch).
    try:
        for n in (still or [])[:20]:
            try:
                nk = _comp_norm_key(n)
                if not nk:
                    continue
                fres = supabase.table("bngc_users").select(
                    "discord_id,game_username").ilike("game_username", nk).execute()
                dids = set()
                for r in (fres.data or []):
                    try:
                        g, d = r.get("game_username"), r.get("discord_id")
                        if g and d and _comp_norm_key(g) == nk:
                            dids.add(d)
                    except Exception:
                        continue
                if len(dids) == 1:
                    did = next(iter(dids))
                    _comp_namemap[n] = did
                    out[n] = did
            except Exception:
                continue
    except Exception:
        pass
    return {n: out.get(n) for n in want}


def _comp_roster_dids(room) -> set:
    """discord_ids mapped from one room roster (fold-tolerant). Never raises."""
    try:
        names = [str(getattr(p, "username", "") or "")
                 for p in (getattr(room, "players", None) or {}).values()]
        names = [n for n in names if n.strip()]
        if not names:
            return set()
        return {d for d in _comp_map_names(names).values() if d}
    except Exception:
        return set()


def _comp_banked_keys(table: str, mid) -> set:
    """Normalized (etype, killer, victim) keys already stored for one match.

    Cross-process idempotency for collaborative banking: the in-memory
    _comp_seen/_rt_seen guards only cover this worker, so contributors (and
    the host, after a contributor) subtract what's already in the DB before
    inserting. Canonical etypes: kill / leave. Never raises.
    """
    keys: set = set()
    try:
        if supabase is None or mid is None:
            return keys
        res = supabase.table(table).select(
            "etype,actor_name,target_name").eq("match_id", mid).limit(500).execute()
        for r in (res.data or []):
            try:
                et = str((r or {}).get("etype") or "").lower()
                cet = "leave" if et == "leave" else "kill"
                keys.add((cet, _comp_norm_key((r or {}).get("actor_name")),
                          _comp_norm_key((r or {}).get("target_name"))))
            except Exception:
                continue
    except Exception:
        pass
    return keys


def _bank_open_events(*, table: str, seen: dict, mid, room, tag: str) -> int:
    """Append fresh kill/leave rows into an already-open tourney match.

    Shared by the host writer and roster contributors: collects this view's
    recent killfeed, drops rows already banked (in-memory seen + DB check,
    both normalized so "cis6led" and "CIS6LED" are the same kill), maps names
    in one query, inserts the remainder. Unknown/twins stay null for /link.
    Returns rows inserted. Never raises.
    """
    try:
        kills = [k for k in (getattr(room, "recent_kills", None) or [])
                 if getattr(k, "event_type", None) in ("killed", "left")]
        if not kills:
            return 0
        seen_set = seen.setdefault(mid, set())
        banked = _comp_banked_keys(table, mid)
        fresh_kills = []
        for k in kills[-200:]:
            try:
                et = str(getattr(k, "event_type", "") or "")
                killer = str(getattr(k, "instigator", "") or "")
                victim = str(getattr(k, "instigated", "") or "")
                if not killer and not victim:
                    continue
                cet = "leave" if et == "left" else "kill"
                key = (cet, _comp_norm_key(killer), _comp_norm_key(victim))
                if key in seen_set or key in banked:
                    continue
                seen_set.add(key)
                fresh_kills.append((et, killer, victim))
            except Exception:
                continue
        if not fresh_kills:
            return 0
        mapping = _comp_map_names([n for (_et, killer, victim) in fresh_kills
                                   for n in (killer, victim)])
        batch = [{
            "match_id": mid,
            "etype": "leave" if et == "left" else "kill",
            "actor_nr": None,
            "actor_name": killer or victim,
            "target_name": victim or None,
            "discord_id": mapping.get(killer),
            "alive": None,
        } for et, killer, victim in fresh_kills]
        try:
            supabase.table(table).insert(batch).execute()
            n_mapped = sum(1 for r in batch if r.get("discord_id"))
            log_comp(f"{tag}match={mid} +{len(batch)} events ({n_mapped} mapped)")
            return batch
        except Exception as exc:
            if not is_transient_error(exc):
                log_comp(f"{tag}match={mid} event write failed ({str(exc)[:80]})")
    except Exception:
        pass
    return []


def _comp_contribute(tid, code, session_id, discord_id, room, room_id) -> bool:
    """Roster-member event contribution into the open comp match.

    All-players fix: the host owns the lifecycle (open / seal / alv snapshot /
    leaver removal / auto-calc) so matches can never duplicate, but any
    authenticated session whose discord_id maps to the room roster may append
    fresh kill/leave rows the host missed. Returns True when membership proved
    (handled — caller returns silently), False for strangers and anonymous
    sessions (caller keeps the existing skip log). Never opens or seals.
    Never raises.
    """
    try:
        if not discord_id or supabase is None:
            return False
        try:
            last_stg = getattr(room, "last_stg", None)
            last_stg = int(float(last_stg)) if last_stg is not None else None
        except Exception:
            last_stg = None
        if last_stg != 1:
            return False
        if str(discord_id) not in _comp_roster_dids(room):
            return False
        mid = None
        try:
            mres = supabase.table("bngc_comp_matches").select("id").eq(
                "tourney_id", tid).eq("room_id", room_id).eq(
                "status", "open").order("id", desc=True).limit(1).execute()
            mrows = mres.data or []
            if mrows:
                mid = mrows[0].get("id")
        except Exception:
            mid = None
        if mid is None:
            return True  # member, but opens are host-owned
        _bank_open_events(table="bngc_comp_events", seen=_comp_seen,
                          mid=mid, room=room, tag="contrib ")
        return True
    except Exception:
        return False


def _rt_contribute(tid, code, session_id, discord_id, room, room_id) -> bool:
    """Ranked-tourney mirror of _comp_contribute (same contract, ranked tables)."""
    try:
        if not discord_id or supabase is None:
            return False
        try:
            last_stg = getattr(room, "last_stg", None)
            last_stg = int(float(last_stg)) if last_stg is not None else None
        except Exception:
            last_stg = None
        if last_stg != 1:
            return False
        if str(discord_id) not in _comp_roster_dids(room):
            return False
        mid = None
        try:
            mres = supabase.table("bngc_ranked_tourney_matches").select("id").eq(
                "tourney_id", tid).eq("room_id", room_id).eq(
                "status", "open").order("id", desc=True).limit(1).execute()
            mrows = mres.data or []
            if mrows:
                mid = mrows[0].get("id")
        except Exception:
            mid = None
        if mid is None:
            return True  # member, but opens are host-owned
        _bank_open_events(table="bngc_ranked_tourney_events", seen=_rt_seen,
                          mid=mid, room=room, tag="rt contrib ")
        return True
    except Exception:
        return False


def _comp_bank_alv(tourney, mid, room) -> None:
    """Seal-time alv snapshot for one comp match. Never raises.

    Row convention (same as comp-intake.js, enshrined in
    test_kills_plus_win_plus_placement): one alv row per roster player,
    dead in chronological kill order first (alive=False), survivors last
    (alive=False) — the last row reads as the winner, giving correct
    placements without any manual override. Spectating host
    (host_playing False) gets no alv row. Skips entirely if alv rows were
    already banked (seal-retry idempotency).
    """
    try:
        if supabase is None or mid is None:
            return
        try:
            # tracked alv rows only: manual dashboard elim corrections must
            # not suppress the seal-time snapshot
            chk = supabase.table("bngc_comp_events").select("id").eq(
                "match_id", mid).eq("etype", "alv").neq("manual", True).limit(1).execute()
            if chk.data:
                return
        except Exception:
            pass
        try:
            players = list((getattr(room, "players", None) or {}).items())
        except Exception:
            players = []
        if not players:
            return
        try:
            me = getattr(room, "me", None)
            self_name = str(getattr(me, "username", "") or "")
        except Exception:
            self_name = ""
        try:
            host_playing = (tourney or {}).get("host_playing", True)
            host_playing = False if host_playing is False else True
        except Exception:
            host_playing = True
        info: dict = {}
        for key, p in players:
            try:
                nm = str(getattr(p, "username", "") or "")
                if not nm:
                    continue
                try:
                    nr = getattr(p, "actor_nr", None)
                    nr = int(nr) if nr is not None else None
                except Exception:
                    nr = None
                if nr is None:
                    try:
                        nr = int(key)
                    except Exception:
                        nr = None
                info[nm] = {"nr": nr, "alive": bool(getattr(p, "is_alive", True))}
            except Exception:
                continue
        if not host_playing and self_name and self_name in info:
            try:
                del info[self_name]
            except Exception:
                pass
        kill_order: list = []
        try:
            for k in (getattr(room, "recent_kills", None) or []):
                try:
                    if getattr(k, "event_type", None) != "killed":
                        continue
                    v = str(getattr(k, "instigated", "") or "")
                    if v and v not in kill_order:
                        kill_order.append(v)
                except Exception:
                    continue
        except Exception:
            pass
        ordered = [n for n in kill_order if n in info and not info[n]["alive"]]
        ordered += [n for n in kill_order if n in info and info[n]["alive"] and n not in ordered]
        ordered += [n for n, d in info.items() if n not in ordered and not d["alive"]]
        ordered += [n for n, d in info.items() if n not in ordered and d["alive"]]
        if not ordered:
            return
        mapping = _comp_map_names(ordered)
        rows = [{
            "match_id": mid,
            "etype": "alv",
            "actor_nr": info[n]["nr"],
            "actor_name": n,
            "target_name": None,
            "discord_id": mapping.get(n),
            "alive": False,
        } for n in ordered]
        try:
            supabase.table("bngc_comp_events").insert(rows).execute()
            log_comp(f"match={mid} +{len(rows)} alv")
        except Exception as exc:
            if not is_transient_error(exc):
                log_comp(f"match={mid} alv write failed ({str(exc)[:80]})")
    except Exception:
        pass


_comp_host_warned: set = set()  # (session, code) already told they're not host


def _comp_series_complete(tid, series_len) -> bool:
    """True when sealed+manual count reached series_len (0 = open-ended).
    Auto-opens stop here; dashboard manual blanks still allowed. Never raises."""
    try:
        sl = int(series_len or 0)
    except Exception:
        return False
    if sl <= 0 or tid is None:
        return False
    try:
        res = supabase.table("bngc_comp_matches").select("id").eq(
            "tourney_id", int(tid)).in_("status", ["sealed", "manual"]).limit(500).execute()
        n = len(res.data or [])
        return n >= sl
    except Exception:
        return False


def seal_comp_match(session_id, discord_id, room, room_id, gm) -> None:
    """Auto open/seal comp matches from the HOST capture. Never raises.

    Match lifecycle is stG-delimited, mirroring comp-intake.js:
      stG == 1  -> open a blank match, enable writes
      stG == 3  -> seal the open match, close writes
      stG == 2  -> transition noise: ignored (no open / seal / write action)

    The delimiter is room.last_stg + the open row itself (no version math,
    no phase-end flag — the ranked sweep consumes that flag first, which is
    why kills used to pile into one never-sealed match):
      open row + last_stg == 1  -> current life: write kills here
      open row + last_stg in (2, 3) -> life ended: seal it, ignore writes
      open row + last_stg None  -> unknown: touch nothing
      no row + last_stg == 1    -> create blank match (version = per-tourney
                                   max+1, same as comp-intake.js), then write
      no row + otherwise        -> ignore (writes closed until next stG 1)

    The host owns the lifecycle (open / seal / alv snapshot / auto-calc):
    with N clients capturing the same room, one writer means no duplicate
    matches. Kill/leave banking is collaborative (roster contributors fill
    host misses). Dashboard
    manual rows are never auto-sealed. At seal, one alv row per roster
    player is banked (dead in kill order, survivors last) so placements
    and winner resolve with no manual override. Twin names stay null —
    never guess (dashboard /link resolves). Lifecycle host-owned, event
    banking collaborative (roster contributors fill host misses).
    """
    try:
        code = str(getattr(room, "short_code", None) or "").strip().upper()
        if len(code) != 5 or not code.isalnum():
            return
        if supabase is None:
            return
        try:
            last_stg = getattr(room, "last_stg", None)
            last_stg = int(float(last_stg)) if last_stg is not None else None
        except Exception:
            last_stg = None
        try:
            tres = supabase.table("bngc_comp_tourneys").select(
                "id,code,status,hosts,created_by,host_playing,series_len").eq("code", code).neq(
                "status", "ended").limit(2).execute()
            rows = tres.data or []
        except Exception as exc:
            if is_transient_error(exc):
                return  # pool blip — next batch retries
            if code not in _comp_logged_codes:
                _comp_logged_codes.add(code)
                log_comp(f"by-code={code} lookup failed ({str(exc)[:60]})")
            return
        if len(rows) != 1:
            if code not in _comp_logged_codes:
                _comp_logged_codes.add(code)
                log_comp(f"by-code={code} rows={len(rows)} → null (ambiguous or none)")
            return
        t = rows[0]
        tid = t.get("id")
        # Host-only lifecycle (created_by always counts as host, per v233).
        # Roster members may still append missed kill/leave rows into the open
        # match via _comp_contribute (lifecycle itself never leaves the host).
        try:
            hosts = {str(h) for h in (t.get("hosts") or []) if h}
            if t.get("created_by"):
                hosts.add(str(t.get("created_by")))
        except Exception:
            hosts = set()
        if not discord_id or str(discord_id) not in hosts:
            try:
                if _comp_contribute(tid, code, session_id, discord_id, room, room_id):
                    return
            except Exception:
                pass
            try:
                key = (str(session_id or "")[:13], code)
                if key not in _comp_host_warned:
                    _comp_host_warned.add(key)
                    log_comp(f"tid={tid} code={code} non-host session — comp writes skipped")
            except Exception:
                pass
            return
        # Current auto-managed open row for this room (dashboard manual rows
        # are owned by the dashboard: never auto-sealed, never written here).
        mid = None
        try:
            mres = supabase.table("bngc_comp_matches").select("id").eq(
                "tourney_id", tid).eq("room_id", room_id).eq(
                "status", "open").order("id", desc=True).limit(1).execute()
            mrows = mres.data or []
            if mrows:
                mid = mrows[0].get("id")
        except Exception:
            mid = None
        if mid is not None and last_stg in (2, 3):
            # stG == 3 (or the 2 that follows it): bank the alv snapshot,
            # then seal and close writes.
            try:
                _comp_bank_alv(t, mid, room)
            except Exception:
                pass
            try:
                from datetime import datetime, timezone as _tz
                sres = supabase.table("bngc_comp_matches").update(
                    {"status": "sealed",
                     "sealed_at": datetime.now(_tz.utc).isoformat()}
                ).eq("id", mid).eq("status", "open").execute()
                if sres.data:
                    log_comp(f"sealed match={mid} tid={tid} code={code} (stG {last_stg})")
                    # Placements are always (re)calculated when alv lands:
                    # snapshot results right away so boards never go stale.
                    try:
                        from server.routes.comp import calculate_tourney_sync as _calc
                        _board = _calc(int(tid), "bridge")
                        log_comp(f"match={mid} auto-calculated {len(_board or [])} rows")
                    except Exception:
                        pass
            except Exception as exc:
                if not is_transient_error(exc):
                    log_comp(f"seal failed match={mid} ({str(exc)[:60]})")
            try:
                _comp_seen.pop(mid, None)
            except Exception:
                pass
            return  # writes closed until the next stG 1
        if mid is None and last_stg != 1:
            # stG 2/3 window (or pre-join): no row to write to, and only
            # stG 1 may create one. Ignore.
            return
        if mid is None:
            # Series complete: no new auto-opens (manual dashboard blanks
            # still allowed — they bypass the bridge entirely).
            try:
                if _comp_series_complete(tid, (t or {}).get("series_len")):
                    log_comp(f"tid={tid} code={code} series complete — auto-open ignored")
                    return
            except Exception:
                pass
            # stG == 1: open a blank match, enable writes. Version is the
            # per-tourney max+1 (rooms share the counter, so engine versions
            # can't be reused across rooms). A lost insert race re-reads the
            # winner's row instead of duplicating.
            try:
                vres = supabase.table("bngc_comp_matches").select("version").eq(
                    "tourney_id", int(tid)).order("version", desc=True).limit(1).execute()
                vrows = vres.data or []
                ver = int(vrows[0].get("version", -1)) + 1 if vrows else 0
            except Exception:
                ver = 0
            try:
                ires = supabase.table("bngc_comp_matches").insert({
                    "tourney_id": tid, "version": ver,
                    "room_id": room_id, "status": "open",
                }).execute()
                irows = ires.data or []
                mid = (irows[0].get("id") if irows else None)
            except Exception as exc:
                if not is_transient_error(exc):
                    try:
                        rres = supabase.table("bngc_comp_matches").select("id").eq(
                            "tourney_id", tid).eq("room_id", room_id).eq(
                            "status", "open").order("id", desc=True).limit(1).execute()
                        rrows = rres.data or []
                        mid = rrows[0].get("id") if rrows else None
                    except Exception:
                        mid = None
                    if mid is None:
                        log_comp(f"open failed tid={tid} ({str(exc)[:60]})")
                        return
                else:
                    return
            if mid is None:
                return
            log_comp(f"open tid={tid} code={code} match={mid} room={str(room_id)[:8]} (stG 1)")
            # Write kill + leave events. Canonical bngc_comp_events shape
            # {match_id, etype, actor_nr, actor_name, target_name, discord_id, alive}
            # (no tourney_id / victim_name columns — matches comp-intake.js).
            # Deduped per match across batches AND against stored rows (both
            # normalized), so host + roster contributors never double-bank;
            # unknown/twins stay null for the dashboard /link flow.
        try:
            batch = _bank_open_events(table="bngc_comp_events", seen=_comp_seen,
                                      mid=mid, room=room, tag="")
            if batch:
                # auto-remove mapped leavers from the match roster (mirrors
                # comp-intake leaves[]; host adds back via dashboard/bot)
                try:
                    leave_dids = sorted({r.get("discord_id") for r in batch
                                         if r.get("etype") == "leave" and r.get("discord_id")})
                    if leave_dids:
                        try:
                            mres = supabase.table("bngc_comp_matches").select(
                                "removed_discord_ids").eq("id", mid).limit(1).execute()
                            cur = list(((mres.data or [{}])[0] or {}).get("removed_discord_ids") or [])
                        except Exception:
                            cur = None  # pre-migration: skip silently
                        if cur is not None:
                            new = [d for d in leave_dids if d not in cur]
                            if new:
                                supabase.table("bngc_comp_matches").update(
                                    {"removed_discord_ids": cur + new}).eq("id", mid).execute()
                                log_comp(f"match={mid} auto-removed {len(new)} leaver(s)")
                except Exception:
                    pass
        except Exception:
            pass
        # No phase-end handling here: sealing is decided above from last_stg
        # (the ranked sweep consumes stg_is_phase_end first, so it can never
        # be trusted this late in the batch).
    except Exception as exc:
        try:
            log_comp(f"bridge failed ({str(exc)[:60]})")
        except Exception:
            pass


def _rt_calc(tid, who="bridge"):
    """Ranked-tourney calculate for non-request callers (bridge auto-calc after
    every seal, so stored standings never go stale). Sync. Never raises."""
    try:
        from server.routes.ranked_tourney import calculate_tourney_sync as _calc
        return _calc(int(tid), who)
    except Exception:
        return []


def _rt_series_complete(tid, series_len) -> bool:
    """True when sealed+manual count reached series_len (0 = open-ended).
    Auto-opens stop here; dashboard manual blanks still allowed. Never raises."""
    try:
        sl = int(series_len or 0)
    except Exception:
        return False
    if sl <= 0 or tid is None:
        return False
    try:
        res = supabase.table("bngc_ranked_tourney_matches").select("id").eq(
            "tourney_id", int(tid)).in_("status", ["sealed", "manual"]).limit(500).execute()
        n = len(res.data or [])
        return n >= sl
    except Exception:
        return False


def _rt_bank_alv(tourney, mid, room) -> None:
    """Seal-time alv snapshot for one ranked-tourney match. Never raises.

    Same convention as _comp_bank_alv: one alv row per roster player, dead
    in chronological kill order first (alive=False), survivors last — the
    last row reads as the winner. Spectating host (host_playing False) gets
    no alv row. Skips entirely if alv rows were already banked
    (seal-retry idempotency).
    """
    try:
        if supabase is None or mid is None:
            return
        try:
            chk = supabase.table("bngc_ranked_tourney_events").select("id").eq(
                "match_id", mid).eq("etype", "alv").limit(1).execute()
            if chk.data:
                return
        except Exception:
            pass
        try:
            players = list((getattr(room, "players", None) or {}).items())
        except Exception:
            players = []
        if not players:
            return
        try:
            me = getattr(room, "me", None)
            self_name = str(getattr(me, "username", "") or "")
        except Exception:
            self_name = ""
        try:
            host_playing = (tourney or {}).get("host_playing", True)
            host_playing = False if host_playing is False else True
        except Exception:
            host_playing = True
        info: dict = {}
        for key, p in players:
            try:
                nm = str(getattr(p, "username", "") or "")
                if not nm:
                    continue
                try:
                    nr = getattr(p, "actor_nr", None)
                    nr = int(nr) if nr is not None else None
                except Exception:
                    nr = None
                if nr is None:
                    try:
                        nr = int(key)
                    except Exception:
                        nr = None
                info[nm] = {"nr": nr, "alive": bool(getattr(p, "is_alive", True))}
            except Exception:
                continue
        if not host_playing and self_name and self_name in info:
            try:
                del info[self_name]
            except Exception:
                pass
        kill_order: list = []
        try:
            for k in (getattr(room, "recent_kills", None) or []):
                try:
                    if getattr(k, "event_type", None) != "killed":
                        continue
                    v = str(getattr(k, "instigated", "") or "")
                    if v and v not in kill_order:
                        kill_order.append(v)
                except Exception:
                    continue
        except Exception:
            pass
        ordered = [n for n in kill_order if n in info and not info[n]["alive"]]
        ordered += [n for n in kill_order if n in info and info[n]["alive"] and n not in ordered]
        ordered += [n for n, d in info.items() if n not in ordered and not d["alive"]]
        ordered += [n for n, d in info.items() if n not in ordered and d["alive"]]
        if not ordered:
            return
        mapping = _comp_map_names(ordered)
        rows = [{
            "match_id": mid,
            "etype": "alv",
            "actor_nr": info[n]["nr"],
            "actor_name": n,
            "target_name": None,
            "discord_id": mapping.get(n),
            "alive": False,
        } for n in ordered]
        try:
            supabase.table("bngc_ranked_tourney_events").insert(rows).execute()
            log_comp(f"rt match={mid} +{len(rows)} alv")
        except Exception as exc:
            if not is_transient_error(exc):
                log_comp(f"rt match={mid} alv write failed ({str(exc)[:80]})")
    except Exception:
        pass


def _rt_autotag(session_id, discord_id, room, code) -> None:
    """Auto-tag the host's codeless session with a room THEY created.

    Removes the manual change-code step in the common case:
    host creates a game room → pID observed here → their one codeless
    session gets tagged → next batch binds (negative caches cleared here).

    State-gated codes (announce rules live downstream):
      - created + codeless → silent start setup (no broadcast anywhere)
      - live (started) + codeless, e.g. after a backout → retag; the bot
        watch loop announces it (embed + roster DMs + @here)
      - ended → never touched (excluded from the lookup outright)

    Strict no-harm rules (manual always wins, never raises):
      - valid 5-char alnum code + room.created_by_me (never a joined room)
      - room is the session's CURRENT room (callers gate this: the sweep
        also visits stale rooms, which must never tag) + live traffic
        (<=90s, belt-and-braces inside)
      - discord_id known
      - host owns EXACTLY ONE non-ended session with no code set
        (zero → nothing to tag; two+ → ambiguous, stay manual)
      - no OTHER non-ended session already claims this code (no steals)
      - never clobbers a code set manually (same or different → hands off)
    """
    try:
        code = str(code or "").strip().upper()
        if len(code) != 5 or not code.isalnum():
            return
        try:
            if not bool(getattr(room, "created_by_me", False)):
                return
        except Exception:
            return
        try:
            import time as _t
            _lp = float(getattr(room, "last_packet_at", None) or 0)
            _fresh = bool(_lp) and ((_t.time() - _lp) <= 90)
        except Exception:
            _fresh = False
        if not _fresh:
            # Stale room snapshot: the sweep iterates every room in the
            # view, including the previous room (still state=match) after
            # a fresh create. Tagging from it would bind the session to
            # the OLD code. Only a room with live traffic may tag.
            return
        if supabase is None or not discord_id:
            return
        did = str(discord_id)
        try:
            q1 = supabase.table("bngc_ranked_tourneys").select(
                "id,code,status,hosts,created_by").eq(
                "created_by", did).neq("status", "ended").limit(20).execute()
            q2 = supabase.table("bngc_ranked_tourneys").select(
                "id,code,status,hosts,created_by").contains(
                "hosts", [did]).neq("status", "ended").limit(20).execute()
        except Exception as exc:
            if is_transient_error(exc):
                return  # pool blip — next batch retries
            return
        try:
            seen: dict = {}
            for _r in ((q1.data or []) + (q2.data or [])):
                try:
                    seen[str((_r or {}).get("id"))] = _r
                except Exception:
                    continue
            mine = list(seen.values())
        except Exception:
            return
        # Only CODELESS tourneys count: a tourney that already has a code
        # isn't waiting for one, so it must not make the choice ambiguous.
        # (One coded + one codeless + fresh room = tag the codeless one.)
        try:
            codeless = [t for t in mine
                        if not str((t or {}).get("code") or "").strip()]
        except Exception:
            return
        if len(codeless) != 1:
            if len(codeless) > 1:
                try:
                    _k = f"autotag-ambiguous:{did[:6]}"
                    if _k not in _rt_logged_codes:
                        _rt_logged_codes.add(_k)
                        log_comp(f"rt autotag skipped — {len(codeless)} codeless tourneys (stay manual)")
                except Exception:
                    pass
            return
        t = codeless[0]
        try:
            tid = t.get("id")
            tstatus = str((t or {}).get("status") or "")
        except Exception:
            return
        if tstatus == "ended":
            return  # belt-and-braces: the lookup already excludes ended
        try:
            cres = supabase.table("bngc_ranked_tourneys").select("id").eq(
                "code", code).neq("status", "ended").limit(2).execute()
            if cres.data:
                try:
                    _k = f"autotag-claimed:{code}"
                    if _k not in _rt_logged_codes:
                        _rt_logged_codes.add(_k)
                        log_comp(f"rt autotag tid={tid} code={code} skipped — claimed by another tourney")
                except Exception:
                    pass
                return
        except Exception as exc:
            if is_transient_error(exc):
                return
            return
        try:
            ures = supabase.table("bngc_ranked_tourneys").update(
                {"code": code}).eq("id", tid).execute()
            if not ures.data:
                return
        except Exception as exc:
            if is_transient_error(exc):
                return  # next batch retries
            try:
                log_comp(f"rt autotag tid={tid} write failed ({str(exc)[:60]})")
            except Exception:
                pass
            return
        try:
            from server.lib import ranked as _rm
            _rm._ROOM_CACHE.pop(code, None)
        except Exception:
            pass
        try:
            from server.routes import capture as _cap
            _cap._ranked_room_cache.pop(code, None)
        except Exception:
            pass
        try:
            log_comp(f"rt autotag tid={tid} code={code} status={tstatus} from host room — binds next batch (change-code still wins)")
        except Exception:
            pass
    except Exception:
        pass


def _rt_attach_room_seal(mid, code, room) -> None:
    """Store the room seal's ladder deltas on a freshly sealed tourney match.

    The room seal (same sweep, just above) already paid the ladder — it IS
    the arena pay. The bridge stores its deltas here so the tourney board
    replays one economy instead of recomputing a second: {mode
    room-seal-v1, winner, points, kills, placements, breakdown}. When the
    room seal hasn't landed yet, pending join keys
    (room-seal-pending-v1) are stored instead and the calculate backfill
    resolves them. Separate best-effort update: pre-v249 DBs (no
    points_table column) fail silently into legacy replay. Never raises.
    """
    try:
        if supabase is None or mid is None:
            return
        try:
            mver = int(getattr(room, "match_version", 0) or 0)
        except Exception:
            mver = 0
        try:
            from server.lib import ranked as ranked_mod
        except Exception:
            return
        try:
            room_data = ranked_mod.get_ranked_room(code)
            rid = str((room_data or {}).get("id") or "") or None
        except Exception:
            rid = None
        if not rid or mver <= 0:
            return  # no join keys: legacy replay, nothing to store
        try:
            bundle = ranked_mod.seal_bundle_for(rid, mver)
        except Exception:
            bundle = None
        if isinstance(bundle, dict):
            patch = {"points_table": bundle}
            try:
                n = len(bundle.get("points") or {})
            except Exception:
                n = "?"
            tag = f"seal bundle ({n} players)"
        else:
            patch = {"points_table": {"mode": "room-seal-pending-v1", "rid": rid,
                                      "code": code, "room_match_version": mver}}
            tag = "pending (room seal not landed yet)"
        try:
            supabase.table("bngc_ranked_tourney_matches").update(
                patch).eq("id", mid).execute()
            log_comp(f"rt match={mid} {tag}")
        except Exception:
            pass  # pre-v249: legacy replay
    except Exception:
        pass


def seal_ranked_tourney_match(session_id, discord_id, room, room_id, gm,
                              is_current: bool = True) -> None:
    """Auto open/seal ranked-tourney matches from the HOST capture. Never raises.

    Ranked tourneys are comp tourneys with dynamic points. Match lifecycle is
    stG-delimited, mirroring seal_comp_match exactly:
      stG == 1  -> open a blank match, enable writes
      stG == 3  -> seal the open match, close writes, auto-calculate
      stG == 2  -> transition noise: ignored (no open / seal / write action)

    The delimiter is room.last_stg + the open row itself (no version math,
    no phase-end flag):
      open row + last_stg == 1  -> current life: write kills here
      open row + last_stg in (2, 3) -> life ended: seal it, ignore writes
      open row + last_stg None  -> unknown: touch nothing
      no row + last_stg == 1    -> create blank match (version = per-tourney
                                   max+1), then write
      no row + otherwise        -> ignore (writes closed until next stG 1)

    The host owns the lifecycle (open / seal / alv snapshot / auto-calc):
    with N clients capturing the same room, one writer means no duplicate
    matches. Kill/leave banking is collaborative (roster contributors fill
    host misses). Dashboard
    manual rows are never auto-sealed. At seal, one alv row per roster
    player is banked (dead in kill order, survivors last) so placements
    and winner resolve with no manual override. Twin names stay null —
    never guess (dashboard /link resolves). Lifecycle host-owned, event
    banking collaborative (roster contributors fill host misses).
    """
    try:
        code = str(getattr(room, "short_code", None) or "").strip().upper()
        if len(code) != 5 or not code.isalnum():
            return
        if supabase is None:
            return
        try:
            last_stg = getattr(room, "last_stg", None)
            last_stg = int(float(last_stg)) if last_stg is not None else None
        except Exception:
            last_stg = None
        try:
            tres = supabase.table("bngc_ranked_tourneys").select(
                "id,code,status,hosts,created_by,host_playing,series_len").eq("code", code).neq(
                "status", "ended").limit(2).execute()
            rows = tres.data or []
        except Exception as exc:
            if is_transient_error(exc):
                return  # pool blip — next batch retries
            if code not in _rt_logged_codes:
                _rt_logged_codes.add(code)
                log_comp(f"rt by-code={code} lookup failed ({str(exc)[:60]})")
            return
        if len(rows) != 1:
            if not rows:
                # Untagged room: try the host auto-tag (created rooms only).
                # On success the next batch binds (caches cleared in autotag).
                # Current-room only: the sweep also visits stale rooms, and
                # tagging from one rebinds the OLD code after a backout.
                if is_current:
                    try:
                        _rt_autotag(session_id, discord_id, room, code)
                    except Exception:
                        pass
            if code not in _rt_logged_codes:
                _rt_logged_codes.add(code)
                log_comp(f"rt by-code={code} rows={len(rows)} -> null (ambiguous or none)")
            return
        t = rows[0]
        tid = t.get("id")
        # Host-only lifecycle (created_by always counts as host).
        # Roster members may still append missed kill/leave rows into the open
        # match via _rt_contribute (lifecycle itself never leaves the host).
        try:
            hosts = {str(h) for h in (t.get("hosts") or []) if h}
            if t.get("created_by"):
                hosts.add(str(t.get("created_by")))
        except Exception:
            hosts = set()
        if not discord_id or str(discord_id) not in hosts:
            try:
                if _rt_contribute(tid, code, session_id, discord_id, room, room_id):
                    return
            except Exception:
                pass
            try:
                key = (str(session_id or "")[:13], code)
                if key not in _rt_host_warned:
                    _rt_host_warned.add(key)
                    log_comp(f"rt tid={tid} code={code} non-host session — ranked writes skipped")
            except Exception:
                pass
            return
        # Current auto-managed open row for this room (dashboard manual rows
        # are owned by the dashboard: never auto-sealed, never written here).
        mid = None
        try:
            mres = supabase.table("bngc_ranked_tourney_matches").select("id").eq(
                "tourney_id", tid).eq("room_id", room_id).eq(
                "status", "open").order("id", desc=True).limit(1).execute()
            mrows = mres.data or []
            if mrows:
                mid = mrows[0].get("id")
        except Exception:
            mid = None
        if mid is not None and last_stg in (2, 3):
            # stG == 3 (or the 2 that follows it): bank the alv snapshot,
            # then seal and close writes.
            try:
                _rt_bank_alv(t, mid, room)
            except Exception:
                pass
            try:
                from datetime import datetime, timezone as _tz
                sres = supabase.table("bngc_ranked_tourney_matches").update(
                    {"status": "sealed",
                     "sealed_at": datetime.now(_tz.utc).isoformat()}
                ).eq("id", mid).eq("status", "open").execute()
                if sres.data:
                    log_comp(f"rt sealed match={mid} tid={tid} code={code} (stG {last_stg})")
                    try:
                        _rt_attach_room_seal(mid, code, room)
                    except Exception:
                        pass
                    try:
                        from server.routes import ranked_tourney as _rt_mod
                        _rt_mod.reset_loser_streaks_sync(mid)
                    except Exception:
                        pass
                    try:
                        _board = _rt_calc(int(tid), "bridge")
                        log_comp(f"rt match={mid} auto-calculated {len(_board or [])} rows")
                    except Exception:
                        pass
            except Exception as exc:
                if not is_transient_error(exc):
                    log_comp(f"rt seal failed match={mid} ({str(exc)[:60]})")
            try:
                _rt_seen.pop(mid, None)
            except Exception:
                pass
            return  # writes closed until the next stG 1
        if mid is None and last_stg != 1:
            # stG 2/3 window (or pre-join): no row to write to, and only
            # stG 1 may create one. Ignore.
            return
        if mid is None:
            # Series complete: no new auto-opens (manual dashboard blanks
            # still allowed — they bypass the bridge entirely).
            try:
                if _rt_series_complete(tid, (t or {}).get("series_len")):
                    log_comp(f"rt tid={tid} code={code} series complete — auto-open ignored")
                    return
            except Exception:
                pass
            # stG == 1: open a blank match, enable writes. Version is the
            # per-tourney max+1 (rooms share the counter, so engine versions
            # can't be reused across rooms). A lost insert race re-reads the
            # winner's row instead of duplicating.
            try:
                vres = supabase.table("bngc_ranked_tourney_matches").select("version").eq(
                    "tourney_id", int(tid)).order("version", desc=True).limit(1).execute()
                vrows = vres.data or []
                ver = int(vrows[0].get("version", -1)) + 1 if vrows else 0
            except Exception:
                ver = 0
            try:
                ires = supabase.table("bngc_ranked_tourney_matches").insert({
                    "tourney_id": tid, "version": ver,
                    "room_id": room_id, "status": "open",
                }).execute()
                irows = ires.data or []
                mid = (irows[0].get("id") if irows else None)
            except Exception as exc:
                if not is_transient_error(exc):
                    try:
                        rres = supabase.table("bngc_ranked_tourney_matches").select("id").eq(
                            "tourney_id", tid).eq("room_id", room_id).eq(
                            "status", "open").order("id", desc=True).limit(1).execute()
                        rrows = rres.data or []
                        mid = rrows[0].get("id") if rrows else None
                    except Exception:
                        mid = None
                    if mid is None:
                        log_comp(f"rt open failed tid={tid} ({str(exc)[:60]})")
                        return
                else:
                    return
            if mid is None:
                return
            log_comp(f"rt open tid={tid} code={code} match={mid} room={str(room_id)[:8]} (stG 1)")
            # Write kill + leave events. Canonical bngc_ranked_tourney_events shape
            # {match_id, etype, actor_nr, actor_name, target_name, discord_id, alive}.
            # Deduped per match across batches AND against stored rows (both
            # normalized), so host + roster contributors never double-bank;
            # unknown/twins stay null for the dashboard /link flow.
        _bank_open_events(table="bngc_ranked_tourney_events", seen=_rt_seen,
                          mid=mid, room=room, tag="rt ")
        # No phase-end handling here: sealing is decided above from last_stg.
    except Exception as exc:
        try:
            log_comp(f"rt bridge failed ({str(exc)[:60]})")
        except Exception:
            pass


def seal_ranked_match(session_id, discord_id, room, room_id, gm) -> None:
    """Seal one ranked match from the host session. Never raises.

    Gates: room carries a ranked code, the room is live, this session is the
    host, and the match version is positive. Idempotency lives in
    ranked.seal_match (re-seals are no-ops), so every phase-end trigger can
    call this freely — including hosting_only hosts, whose spectator did is
    skipped from awards but whose feed still seals everyone else's kills.

    Winner = alive roster in the HOST view (alv is broadcast, so this agrees
    with each client's own alive check); nobody alive (zone double-KO) =>
    most-recent roster death survived longest. Kills x8, win +50, loss -5.
    Non-host sessions return silently (personal win/defeat already ran).
    """
    try:
        from server.lib import ranked as ranked_mod
    except Exception as exc:
        log_err(f"processor ranked import failed {exc}")
        return
    try:
        try:
            if getattr(ranked_mod, "RANKED_ENABLED", False) is False:
                return  # v2.19.0 ranked sleeping: code committed, never seals
        except Exception:
            return
        code = getattr(room, "short_code", None)
        if not code:
            return
        room_data = ranked_mod.get_ranked_room(code)
        try:
            mver = int(getattr(room, "match_version", 0) or 0)
        except Exception:
            mver = 0
        if not room_data or room_data.get("status") != "live":
            # Loud skip (once per match version): a match ended in a room
            # that was never started — run /ranked start in Discord.
            try:
                if mver > 0 and getattr(room, "_seal_skip_logged_v", 0) != mver:
                    room._seal_skip_logged_v = mver
                    log_ranked(f"[ranked] seal SKIPPED {(room_data or {}).get('id') or code} v{mver} — "
                               f"room status={((room_data or {}).get('status')) or 'unknown'}, run /ranked start")
            except Exception:
                pass
            return  # not a live ranked room
        if str(discord_id or "") != str(room_data.get("host_discord_id") or ""):
            return  # not the host — personal stats only
        rid = str(room_data.get("id"))
        if not rid or mver <= 0:
            return
        # Comp tourney rooms score manually: ranked must not pay out here.
        # One indexed lookup per phase end only (never on the hot path).
        # Lookup failure fails OPEN (ranked proceeds) — never break ranked
        # on a comp-table blip.
        try:
            if supabase is not None and code:
                _ct = supabase.table("bngc_comp_tourneys").select("id").eq(
                    "code", str(code).upper()).neq("status", "ended").limit(2).execute()
                if len(_ct.data or []) == 1:
                    log_ranked(f"[ranked] seal SKIPPED {rid} v{mver} — comp tourney room (manual scoring)")
                    return
        except Exception:
            pass
        roster = set(ranked_mod.room_roster_ids(room_data))
        if not roster:
            return
        # hosting_only: the host spectates (self-damages, reads dead) — not
        # counted, but his feed still seals the players' kills.
        host_did = str(room_data.get("host_discord_id") or "")
        skip = {host_did} if (room_data.get("host_status") or "").strip() == "hosting_only" and host_did else set()
        name2did = _roster_name2did(room_data, room)
        # alive roster in the host view + death recency (most-recent first)
        # for the double-KO fallback below.
        alive: list = []
        try:
            for p in (getattr(room, "players", None) or {}).values():
                if not getattr(p, "is_alive", False):
                    continue
                try:
                    did = name2did.get(str(getattr(p, "username", "")))
                except Exception:
                    did = None
                if did and did in roster and did not in skip and did not in alive:
                    alive.append(did)
        except Exception:
            pass
        dead_order: list = []  # victim dids, most-recent death first
        try:
            for k in reversed(getattr(room, "recent_kills", None) or []):
                try:
                    if getattr(k, "event_type", None) != "killed":
                        continue
                    vdid = name2did.get(str(getattr(k, "instigated", "")))
                    if vdid and vdid in roster and vdid not in skip and vdid not in dead_order:
                        dead_order.append(vdid)
                except Exception:
                    continue
        except Exception:
            pass
        # exact per-kill events (roster killer + mapped victim; victim may be
        # non-roster — they earn nothing but their did is stored for review).
        # NOTE: the engine clears the feed at each stG=1 (new life), so these
        # are current-match kills only — no cross-match leakage.
        kill_events: list = []
        kills_count: dict = {}
        try:
            for k in (getattr(room, "recent_kills", None) or []):
                try:
                    if getattr(k, "event_type", None) != "killed":
                        continue
                    kdid = name2did.get(str(getattr(k, "instigator", "")))
                    if not kdid or kdid not in roster or kdid in skip:
                        continue
                    vdid = name2did.get(str(getattr(k, "instigated", "")))
                    kill_events.append((kdid, vdid or kdid))
                    kills_count[kdid] = kills_count.get(kdid, 0) + 1
                except Exception:
                    continue
        except Exception:
            pass
        # winner: first alive; total wipe => the last killer survived
        # longest (they were alive at least until the final blow — crowning
        # the last *victim* gets it backwards when the killer then dies to
        # zone); no kills at all => most-recent death. Every other roster
        # did takes the loss — zone, unknown, and killed alike.
        # Killfeed is truth for deaths: the host alv flag can still read
        # alive at seal time for a player the feed already killed (stale
        # snapshot). Anyone in dead_order is dead, never the winner.
        try:
            _dead = set(dead_order or [])
            if _dead:
                alive = [d for d in (alive or []) if d not in _dead]
        except Exception:
            pass
        try:
            _last_killer = None
            for _kdid, _vdid in reversed(kill_events or []):
                try:
                    if str(_kdid or "").strip() and str(_kdid) != str(_vdid):
                        _last_killer = str(_kdid)
                        break
                except Exception:
                    continue
        except Exception:
            _last_killer = None
        winner = alive[0] if alive else (_last_killer or (dead_order[0] if dead_order else None))
        # duo: alive teammates of the winner share the crown (P1 tie, no
        # loss) — otherwise the surviving partner "wins" the game and still
        # pays a defeat. Dead teammates keep their death placement + loss.
        tied_winners: list = []
        try:
            if winner and getattr(room, "is_duo", False):
                try:
                    _by_did = {}
                    for _nm, _dd in (name2did or {}).items():
                        if _dd and str(_dd) not in _by_did:
                            _by_did[str(_dd)] = str(_nm)
                    _wname = _by_did.get(str(winner))
                    _wteam = room.get_team(_wname) if _wname else None
                    if _wteam is None and _wname:
                        for _t in ((getattr(room, "teams", None)) or {}).values():
                            try:
                                if any(_names_equal(_m, _wname) for _m in (getattr(_t, "members", None) or [])):
                                    _wteam = _t
                                    break
                            except Exception:
                                continue
                    if _wteam is not None:
                        _alive_set = set(alive or [])
                        for _m in (getattr(_wteam, "members", None) or []):
                            try:
                                _md = (name2did or {}).get(str(_m))
                                if (not _md):
                                    for _nm2, _dd2 in (name2did or {}).items():
                                        if _names_equal(_nm2, str(_m)):
                                            _md = _dd2
                                            break
                                if (_md and str(_md) in roster and str(_md) not in skip
                                        and str(_md) != str(winner) and str(_md) in _alive_set
                                        and str(_md) not in tied_winners):
                                    tied_winners.append(str(_md))
                            except Exception:
                                continue
                except Exception:
                    tied_winners = []
        except Exception:
            tied_winners = []
        try:
            _wset = {str(winner)} | {str(d) for d in (tied_winners or [])} if winner else set()
        except Exception:
            _wset = {str(winner)} if winner else set()
        # victim -> killer for killer-relative loss (grandmaster feeding a
        # lower tier pays up to -50). Unknown/zone deaths stay None and pay
        # the flat tier stake. Last kill wins per victim.
        deaths = {did: None for did in roster if did not in skip and did not in _wset}
        try:
            for _kdid, _vdid in (kill_events or []):
                try:
                    _vk = str(_vdid or "").strip()
                    _kk = str(_kdid or "").strip()
                    if _vk and _vk in deaths and _kk and _vk != _kk:
                        deaths[_vk] = _kk
                except Exception:
                    continue
        except Exception:
            pass
        # placements from THIS room view (reverse death recency, winner
        # first) so boards always agree with the crown even in a zone
        # double-KO. The winner always places P1; the dead place only when
        # their game name resolves to a verified identity (bngc_users via
        # _comp_map_names - the same stable mapping the dashboard shows).
        # Session-gossip name2did alone is not identity enough to place.
        placements: dict = {}
        try:
            order = []
            if winner:
                order.append(winner)
            for _td in (tied_winners or []):
                if _td not in order:
                    order.append(_td)
            dead_names = [by_did_nm for by_did_nm in
                          (name2did or {}).items()]
            did_to_name = {str(d): nm for nm, d in dead_names if d}
            cand = list(reversed(dead_order)) + [d for d in (deaths or {})
                                                 if d not in dead_order]
            verified = _comp_map_names([did_to_name[str(d)]
                                        for d in cand
                                        if str(d) in did_to_name])
            for d in reversed(dead_order):
                nm = did_to_name.get(str(d))
                if (d not in _wset and d not in order
                        and nm and verified.get(nm) and
                        str(verified.get(nm)) == str(d)):
                    order.append(d)
            # zone-dead roster dids (no killfeed entry) place after the
            # killfeed-dead when verified-mapped; order among them is
            # roster order - deterministic enough for the purse.
            for d in (deaths or {}):
                nm = did_to_name.get(str(d))
                if (d not in _wset and d not in order
                        and nm and verified.get(nm) and
                        str(verified.get(nm)) == str(d)):
                    order.append(d)
            # co-winners share P1 (engine pays every P1 holder the win
            # bonus); everyone else places sequentially from P2.
            try:
                placements = {}
                _next = 2
                for _did in order:
                    if _did in _wset:
                        placements[_did] = 1
                    else:
                        placements[_did] = _next
                        _next += 1
            except Exception:
                placements = {did: i + 1 for i, did in enumerate(order)}
        except Exception:
            placements = {}
        res = ranked_mod.seal_match(rid=rid, match_version=mver, gm=gm or room_data.get("gm") or "zw_lg",
                                    winner_discord_id=winner, kills=kills_count,
                                    placements=placements or None,
                                    kill_events=kill_events or None,
                                    death_killers=deaths or None,
                                    tied_winners=tied_winners or None)
        if not res.get("ok"):
            log_ranked(f"[ranked] seal failed {rid} v{mver}: {res.get('error')}")
            return
        # Bank the comp-style evidence rows for this seal (the host tracker
        # may also bank; the alv pre-check inside keeps the engine side
        # idempotent on retries and dual-writer races). Never breaks the seal.
        _ranked_bank_events(rid, mver, room, name2did, roster, skip, winner,
                            dead_order)
        log_ranked(f"[ranked] sealed {rid} v{mver} winner={winner} points={res.get('points')} kills={kills_count}")
    except Exception as exc:
        log_ranked(f"[ranked] seal_ranked_match failed {exc}")


def _ranked_bank_events(rid: str, mver: int, room, name2did: dict,
                        roster: set, skip: set, winner,
                        dead_order: list) -> None:
    """Bank comp-style kill + alv rows for one ranked seal. Never raises.

    Mirror of _comp_bank_alv for ranked rooms: one kill row per roster
    killfeed event (killer/victim names + killer did) and one alv row per
    roster player, dead in kill order first, winner last (all alive=False
    by the comp row convention - the last row reads as the winner).
    Spectating host (skip) gets no alv row but their killfeed still banks
    everyone else. Idempotent: skips when alv rows already exist for
    (rid, mver) - seal retries and dual-writer races never duplicate.
    Bank failure never breaks the seal (ranked_results stays truth).
    """
    try:
        if supabase is None:
            return
        try:
            chk = supabase.table("bngc_ranked_events").select("id").eq(
                "room_id", str(rid)).eq("match_version", int(mver)).eq(
                "etype", "alv").limit(1).execute()
            if chk.data:
                return  # already banked (retry / host tracker won the race)
        except Exception:
            pass  # pre-v240 DB (missing table): seal proceeds bankless
        rows: list = []
        # kill rows: roster killer + mapped victim names, killer did set
        try:
            by_did: dict = {}
            for nm, did in (name2did or {}).items():
                if did and str(did) not in by_did:
                    by_did[str(did)] = nm
            for k in (getattr(room, "recent_kills", None) or []):
                try:
                    if getattr(k, "event_type", None) != "killed":
                        continue
                    kname = str(getattr(k, "instigator", "") or "")
                    vname = str(getattr(k, "instigated", "") or "")
                    if not kname or not vname:
                        continue
                    kdid = name2did.get(kname)
                    if not kdid or kdid not in roster:
                        continue
                    # spectating hosts still bank their killfeed (only
                    # their alv rows are suppressed)
                    try:
                        k_actor_nr = next(int(key) for key, p in (
                            getattr(room, "players", None) or {}).items()
                            if str(getattr(p, "username", "")) == kname)
                    except Exception:
                        k_actor_nr = None
                    rows.append({
                        "room_id": str(rid), "match_version": int(mver),
                        "etype": "kill",
                        "actor_name": kname, "target_name": vname,
                        "discord_id": str(kdid),
                        "actor_nr": k_actor_nr, "alive": None,
                    })
                except Exception:
                    continue
        except Exception:
            pass
        # alv snapshot: dead in kill order first (reverse dead_order =
        # most-recent -> first-out), then everyone else; winner last.
        try:
            info: dict = {}
            for key, p in ((getattr(room, "players", None) or {}).items()):
                try:
                    nm = str(getattr(p, "username", "") or "")
                    if not nm:
                        continue
                    info[nm] = {
                        "did": None,  # verified mapping fills below
                        "alive": bool(getattr(p, "is_alive", True)),
                        "nr": getattr(p, "actor_nr", None),
                    }
                except Exception:
                    continue
            by_did: dict = {}
            for nm2, did2 in (name2did or {}).items():
                if did2 and str(did2) not in by_did:
                    by_did[str(did2)] = nm2
            # verified identity mapping (bngc_users; twins stay None)
            vmap = _comp_map_names(list(info.keys()))
            for nm, d in info.items():
                vd = vmap.get(nm)
                if vd:
                    d["did"] = str(vd)
            ordered: list = []
            for did in reversed(list(dead_order or [])):
                nm = by_did.get(str(did))
                if nm and nm in info and nm not in ordered:
                    ordered.append(nm)
            for nm in info:
                if nm not in ordered:
                    ordered.append(nm)
            wname = next((nm for nm, d in info.items()
                          if d.get("did") and str(d["did"]) == str(winner or "")), None)
            if wname and wname in ordered:
                ordered.remove(wname)
                ordered.append(wname)
            for nm in ordered:
                d = info.get(nm) or {}
                did = d.get("did")
                if did and str(did) in {str(s) for s in (skip or set())}:
                    continue  # spectating host: no alv row
                rows.append({
                    "room_id": str(rid), "match_version": int(mver),
                    "etype": "alv",
                    "actor_name": nm, "target_name": None,
                    "discord_id": did,
                    "actor_nr": d.get("nr"), "alive": False,
                })
        except Exception:
            pass
        if not rows:
            return
        try:
            supabase.table("bngc_ranked_events").insert(rows).execute()
            log_ranked(f"[ranked] banked {rid} v{mver}: +{len(rows)} rows")
        except Exception as exc:
            if not is_transient_error(exc):
                log_ranked(f"[ranked] bank write failed {rid} v{mver} ({str(exc)[:80]})")
    except Exception:
        pass


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

    # Entry log (always visible): proves the batch reached the processor
    # with real packets. If a kill never credits AND this line is missing,
    # the batch died in capture (view/packets empty), not in here.
    try:
        _kinds: dict = {}
        for _p in packets:
            try:
                _t = (_p.get("packet") or _p.get("packet_type") or "?") if isinstance(_p, dict) else getattr(_p, "packet_type", "?")
                _kinds[str(_t)] = _kinds.get(str(_t), 0) + 1
            except Exception:
                continue
        _kinds_s = ",".join(f"{k}:{v}" for k, v in sorted(_kinds.items()))
        log_engine("proc", session_id[:13], action="batch", pkts=len(packets),
                   kinds=_kinds_s, me=(view.me.username if view and view.me else None) or "?")
    except Exception:
        pass

    # Fetch discord_id from bngc_sessions table
    try:
        sess_res = await db_io(db.get_session_sync, session_id)  # Query session by ID (thread: no loop block)
        sess_data = sess_res.get("data") if isinstance(sess_res, dict) else None
        discord_id = sess_data.get("discord_id") if sess_data else None  # Get Discord ID
        if not discord_id:            # No Discord ID found
            # Personal stats still blocked — but comp bridge runs with
            # nullable discord_ids (unmapped flow) so tourneys keep sealing.
            log_comp(f"no discord_id for session {session_id[:13]} — comp-only mode")
            try:
                for _rid, _room in list((getattr(view, "rooms", None) or {}).items()):
                    if getattr(_room, "room_id", None) == "lobby":
                        continue
                    try:
                        await db_io(seal_comp_match, session_id, None, _room, _rid,
                                    getattr(_room, "game_mode", None))
                    except Exception:
                        pass
            except Exception:
                pass
            return                    # Can't proceed with personal stats without Discord ID
    except Exception as exc:
        log_err(f"processor: get session failed {session_id[:13]} {exc}")
        return                        # DB error → abort

    # Fetch user row from bngc_users (kills, deaths, wins, etc.)
    try:
        user_res = await db_io(lambda: supabase.table("bngc_users").select("*").eq("discord_id", discord_id).single().execute())
        user = user_res.data          # Extract user dict
        if not user:                  # No user found
            log_err(f"processor: no user for discord_id {discord_id[:6]}")
            return                    # Can't proceed without user
    except Exception as exc:
        log_err(f"processor: get user failed {discord_id[:6]} {exc}")
        return                        # DB error → abort

    # Null-identity heal: join props arrived without a 255 username (or under
    # a drifting name), so view.me is None while playtime still counts — every
    # kill/death/win silently misses ("registered but ignored"). Adopt the
    # matching roster row via the DB game_username BEFORE me_name is read, so
    # this batch credits correctly. No-op when identity already resolved.
    try:
        _heal_view_me(view, user)
    except Exception:
        pass

    # Log current user stats before processing (ALWAYS — every batch)
    log_sys(f"PROCESSOR CALL session={session_id[:13]} did={discord_id[:6]} n={len(packets)} "
            f"kills={user.get('kills',0)} deaths={user.get('deaths',0)} wins={user.get('wins',0)}")

    # Last-seen heartbeat (drives "last online" display): refresh at most
    # every 5 minutes per player, never let a DB blip break the batch.
    try:
        if _last_seen_due(user.get("last_seen")):
            from datetime import datetime, timezone as _tz
            _now_iso = datetime.now(_tz.utc).isoformat()
            try:
                await db_io(lambda: supabase.table("bngc_users").update(
                    {"last_seen": _now_iso}).eq("discord_id", discord_id).execute())
            except Exception as exc:
                log_err(f"processor last_seen failed {discord_id[:6]} {exc}")
            try:
                user["last_seen"] = _now_iso
            except Exception:
                pass
    except Exception:
        pass

    # Settle precision checks due from earlier batches (writes are verified ~2s later)
    try:
        await _precision.verify_due()
    except Exception:
        pass

    # Track which match_versions already had a death counted (dedup alv + killfeed)
    # Prevents double-counting when both killfeed and alv report the same death
    death_counted_for_match = set()  # Set of (discord_id, match_version) tuples
    _kf_credited = 0      # kills credited to self this batch (tripwire denominator)
    _kf_unattributed = 0  # valid killfeed kills credited to NOBODY (tripwire numerator)
    me_name = (view.me.username if view.me else None) or user.get("game_username")  # My game username
    gm_val = view.game_mode.value if view.game_mode else None  # Current game mode string
    last_zone_phase = getattr(view, "_lastZonePhase", None)  # Last zone phase (persisted on view)

    # hosting_only: host is a spectator in a ranked room — skip personal stat writes
    _me_hosting_only = False
    if getattr(view, "host_mode", False):
        try:
            from server.lib import ranked as ranked_mod
            _room_code = getattr(view, "room", None)
            if _room_code:
                _rdata = ranked_mod.get_ranked_room(str(getattr(_room_code, "short_code", "") or ""))
                if _rdata and (_rdata.get("host_status") or "").strip() == "hosting_only":
                    _me_hosting_only = True
        except Exception:
            pass

    # Batch write buffers: game-event rows queue here for ONE bulk insert at
    # batch end, and user counters accumulate in-memory for ONE update.
    # This collapses ~30-60 sequential queries per match batch into a few.
    ev_rows: list = []               # bngc_game_events rows (bulk flushed)
    dirty: set = set()               # user counter keys changed this batch
    pending_user_updates: dict = {}  # non-counter user field updates (deferred)

    # Sync user fields from SessionView (so /info, /stats are live)
    # Only updates if values changed; the DB write is deferred to the
    # batch-end flush (merged with counter updates into one UPDATE).
    try:
        updates = {}                 # Build update dict
        if view.me:                  # Only if we have player data
            # Usernames arrive tag-wrapped from some props ("<color=black>Bob").
            # The engine cleans at ingestion, but strip here too so the DB can
            # never store tags (and heals rows dirtied before the engine fix).
            clean_name = _strip_color_tags(view.me.username).strip() if view.me.username else None
            if not clean_name:
                clean_name = view.me.username
            if clean_name and clean_name != user.get("game_username"):
                updates["game_username"] = clean_name  # Username changed
            if view.me.game_id and view.me.game_id != user.get("game_id"):
                updates["game_id"] = view.me.game_id        # Game ID changed
            if view.me.skin and view.me.skin != user.get("skin"):
                updates["skin"] = view.me.skin              # Skin changed
            if view.me.pickaxe and view.me.pickaxe != user.get("pickaxe"):
                updates["pickaxe"] = view.me.pickaxe        # Pickaxe changed
        if updates:                  # Stash for the batch-end flush
            pending_user_updates.update(updates)
            log_sys(f"PROCESSOR sync user {discord_id[:6]} {updates}")
            user.update(updates)     # Keep local copy in sync now
    except Exception as exc:
        log_err(f"processor sync user failed {exc}")

    # Identity fallback: when view.me never resolved (join props arrived
    # without 255/skid, or under a drifting name), the block above syncs
    # nothing — skins/pickaxe/game_id freeze while playtime still counts.
    # Scan room rosters for a self row (name OR stable game_id) carrying
    # fresher fields and adopt them. Never overwrites with blanks.
    try:
        need_skin = not user.get("skin")
        need_pick = not user.get("pickaxe")
        need_gid = not user.get("game_id")
        if need_skin or need_pick or need_gid:
            me_row = None
            try:
                _rooms = list((view.rooms or {}).values())
            except Exception:
                _rooms = []
            for _r in _rooms:
                try:
                    _ps = getattr(_r, "players", None) or {}
                except Exception:
                    continue
                for _p in _ps.values():
                    try:
                        if _is_self(_r, view, user, getattr(_p, "username", None)):
                            me_row = _p
                            break
                    except Exception:
                        continue
                if me_row is not None:
                    break
            if me_row is not None:
                _adopted = {}
                try:
                    if need_skin and getattr(me_row, "skin", None) and me_row.skin != user.get("skin"):
                        _adopted["skin"] = me_row.skin
                    if need_pick and getattr(me_row, "pickaxe", None) and me_row.pickaxe != user.get("pickaxe"):
                        _adopted["pickaxe"] = me_row.pickaxe
                    if need_gid and getattr(me_row, "game_id", None):
                        _adopted["game_id"] = me_row.game_id
                except Exception:
                    pass
                if _adopted:
                    pending_user_updates.update(_adopted)
                    user.update(_adopted)
                    log_sys(f"PROCESSOR sync user {discord_id[:6]} adopted from roster {sorted(_adopted)}")
    except Exception as exc:
        log_err(f"processor roster-adopt failed {exc}")

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

        # Log EVERY packet (ALWAYS — full visibility)
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
                    try:
                        if me_name and (_names_equal(instigator, me_name) or _names_equal(instigated, me_name)):
                            log_engine("proc", session_id[:13], action="self-skip", reason="echo",
                                       killer=instigator, victim=instigated, kind=kf_type)
                    except Exception:
                        pass
                elif _kill_count_dup(_kf_room, instigator, instigated, kf_type, _kf_t):
                    skip_kf = True
                    log_sys(f"PROCESSOR killfeed dup ignored {instigator}->{instigated} ({kf_type}, retransmit window)")
                    try:
                        if me_name and (_names_equal(instigator, me_name) or _names_equal(instigated, me_name)):
                            log_engine("proc", session_id[:13], action="self-skip", reason="dup",
                                       killer=instigator, victim=instigated, kind=kf_type)
                    except Exception:
                        pass
                else:
                    skip_kf = False
                # Queue killfeed event (records ALL kills/knocks in the lobby;
                # bulk-inserted once at batch end)
                if not skip_kf:
                    _buf_event(ev_rows, discord_id, session_id, "killfeed",
                               {"gm": eff_gm,              # Game mode
                                "instigator": instigator,   # Killer name
                                "instigated": instigated,   # Victim name
                                "kill_type": kf_type,       # "killed" or "knocked"
                                "room_id": pkt_room_id,     # Room where it happened
                                "code": pkt_short,          # Room short code
                                "short_code": pkt_short,    # Duplicate for querying
                                "match_version": pkt_match_ver})  # Match version
                    _kill_count_stamp(_kf_room, instigator, instigated, kf_type, _kf_t)

                # Self-specific kill: increment kills + queue kill event.
                # Kills only (kf_type == killed): a knock is not an elimination —
                # counting it doubled every knock-then-finish.
                # Identity via _is_self (name OR stable game_id): some clients'
                # killfeed names drift from the join-time username (casing/tags)
                # or view.me never resolved — playtime still counted while
                # kills silently missed. The gid path heals exactly that.
                _self_kill = (not skip_kf and not _me_hosting_only and kf_type == "killed"
                              and _is_self(_kf_room, view, user, instigator))
                if kf_type == "killed" and not skip_kf and not _me_hosting_only:
                    if _self_kill:
                        _kf_credited += 1
                        # Tier-3 tripwire: casing drift credited via the open
                        # fallback — log once per session so a drifting client
                        # gets fixed (game-name relink) before hours pass.
                        try:
                            if _self_match_tier(_kf_room, view, user, instigator) == 3 and not getattr(view, "_drift_logged", False):
                                view._drift_logged = True
                                log_sys(f"PROCESSOR drift credited killer={instigator} me={me_name} "
                                        f"gm={eff_gm} room={str(pkt_room_id or '')[:8]} (tier-3 fold fallback)")
                        except Exception:
                            pass
                    else:
                        _kf_unattributed += 1
                # Diagnostic for the null-identity class of bug (game_username
                # null, view.me None): valid kill, nobody credited. Once per
                # session so a broken client doesn't spam the log.
                if (kf_type == "killed" and not skip_kf and not _self_kill
                        and not _me_hosting_only and instigator and instigated
                        and view.me is None and not me_name
                        and not getattr(view, "_no_me_kill_logged", False)):
                    try:
                        view._no_me_kill_logged = True
                    except Exception:
                        pass
                    log_sys(f"PROCESSOR no-me kill unattributed killer={instigator} victim={instigated} "
                            f"gm={eff_gm} room={str(pkt_room_id or '')[:8]} — identity unresolved (check /api/stats identity)")
                if _self_kill:  # I am the killer
                    old_kills = user.get("kills", 0)  # Pre-increment value for the log
                    new_kills = _bump_user(user, dirty, "kills")  # Queued for batch-end flush
                    _bump_user(user, dirty, "play_points", PLAY_KILL)  # Play points: +per kill
                    # Per-match kill counter for best_match_kills (survives batches via room state).
                    # Batch-only counting capped the record at ~1 per POST — this accumulates
                    # across the whole (room_id, match_version).
                    try:
                        _mk_room = _kf_room if _kf_room is not None else _get_room(pkt_room_id, view)
                        if _mk_room is not None:
                            _mk_key = f"{str(pkt_room_id or _mk_room.room_id or '')[:8]}:v{int(pkt_match_ver if pkt_match_ver is not None else getattr(_mk_room, 'match_version', 0) or 0)}"
                            _mk_map = getattr(_mk_room, "my_kills_by_version", None)
                            if not isinstance(_mk_map, dict):
                                _mk_room.my_kills_by_version = {}
                                _mk_map = _mk_room.my_kills_by_version
                            _mk_map[_mk_key] = int(_mk_map.get(_mk_key) or 0) + 1
                            while len(_mk_map) > 50:
                                _mk_map.pop(next(iter(_mk_map)))
                    except Exception:
                        pass
                    # Queue kill event (separate from killfeed for stat tracking)
                    _buf_event(ev_rows, discord_id, session_id, "kill",
                               {"gm": eff_gm, "victim": instigated, "room_id": pkt_room_id,
                                "code": pkt_short, "match_version": pkt_match_ver})
                    try:
                        _k_actors = _extract_actors(pkt)
                    except Exception:
                        _k_actors = (None, None)
                    log_engine("proc", session_id[:13], action="kill", victim=instigated,
                               kills=f"{old_kills}->{new_kills}", actors=str(_k_actors))
                    _precision.schedule("kill", session_id=session_id, discord_id=discord_id,
                                        expected=new_kills, match_version=pkt_match_ver,
                                        room_id=pkt_room_id, extra={"victim": instigated})

                # Self-specific knock: play points only (a knock is not an
                # elimination — the finishing kill pays separately on top).
                elif not skip_kf and not _me_hosting_only and kf_type == "knocked" and _is_self(_kf_room, view, user, instigator):  # I am the knocker
                    _bump_user(user, dirty, "play_points", PLAY_KNOCK)  # Queued for batch-end flush
                    _buf_event(ev_rows, discord_id, session_id, "knock",
                               {"gm": eff_gm, "victim": instigated, "room_id": pkt_room_id,
                                "code": pkt_short, "match_version": pkt_match_ver})
                    log_sys(f"PROCESSOR KNOCK victim={instigated} play_points +{PLAY_KNOCK}")

                # Self-specific death: increment deaths + write death event (one per life).
                # Kills only: being knocked is not death (revive possible) — the
                # finishing "killed" feed or alv=false counts it.
                elif not skip_kf and not _me_hosting_only and kf_type == "killed" and _is_self(_kf_room, view, user, instigated):  # I am the victim
                    # Dedup key per room+version: killfeed + alv for the same
                    # death count once, but different rooms never collide.
                    _dr0 = getattr(_kf_room, "room_id", None) or pkt_room_id
                    _dv0 = getattr(_kf_room, "match_version", None)
                    _dv0 = _dv0 if _dv0 is not None else pkt_match_ver
                    dedup_key = (discord_id, _dr0, _dv0)
                    if dedup_key not in death_counted_for_match:  # Not yet counted
                        death_room = _get_room(pkt_room_id, view)  # Resolve room for per-life flag
                        if death_room is not None and death_room.had_self_death:
                            log_sys(f"PROCESSOR DEATH via killfeed skip already counted this life killer={instigator}")
                            death_counted_for_match.add(dedup_key)  # Mark as counted
                        else:
                            old_deaths = user.get("deaths", 0)  # Pre-increment value for the log
                            new_deaths = _bump_user(user, dirty, "deaths")  # Queued for batch-end flush
                            # Queue death event (with "via": "killfeed" to track source)
                            _buf_event(ev_rows, discord_id, session_id, "death",
                                       {"gm": eff_gm, "killer": instigator, "via": "killfeed",
                                        "room_id": pkt_room_id, "code": pkt_short, "match_version": pkt_match_ver})
                            log_engine("proc", session_id[:13], action="death", via="killfeed",
                                       killer=instigator, deaths=f"{old_deaths}->{new_deaths}")
                            death_counted_for_match.add(dedup_key)  # Mark as counted
                            _precision.schedule("death", session_id=session_id, discord_id=discord_id,
                                                expected=new_deaths, match_version=pkt_match_ver,
                                                room_id=pkt_room_id, extra={"via": "killfeed"})
                            if death_room is not None:
                                death_room.had_self_death = True  # One death per life
                            # Wins-only: no immediate defeat (deaths still count above).

            # --- Death via alv (self elim not in killfeed) ---
            # Some eliminations don't appear in killfeed (e.g. zone damage, disconnect)
            # The "alive" packet with is_alive=false or is_eliminated=true signals death
            if pkt_type in ("alive", "eliminated", "ALIVE", "ELIMINATED"):
                try:
                    # Extract alive/eliminated flags from packet
                    alv_is_alive = pkt.get("is_alive") if isinstance(pkt, dict) else getattr(pkt, "is_alive", None)
                    alv_is_elim = pkt.get("is_eliminated") if isinstance(pkt, dict) else getattr(pkt, "is_eliminated", None)
                    alv_player = pkt.get("player_name") or player_name or instigator  # Which player
                    _alv_via = "explicit-name" if alv_player else "unknown"
                    _alv_actors = (None, None)
                    try:
                        if isinstance(pkt, dict):
                            _alv_actors = _extract_actors(pkt)
                    except Exception:
                        pass

                    # Resolve nameless alv via actor map FIRST (correct attribution:
                    # actor 2's death must not count as mine just because I'm also dead)
                    if not alv_player and isinstance(pkt, dict):
                        try:
                            _pa, _pt = _alv_actors  # Threaded from Engine params
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
                                        _alv_via = f"actor_map[{_nr}]"
                                        break
                            if alv_player:
                                try:
                                    _mine_v = _is_self(_get_room(pkt_room_id, view), view, user, alv_player)
                                except Exception:
                                    _mine_v = "?"
                                log_engine("proc", session_id[:13], action="alv-resolve",
                                           actors=str((_pa, _pt)), name=alv_player, via=_alv_via, mine=_mine_v)
                        except Exception:
                            pass
                    # Last resort: no name AND no actor info — if I'm dead in room state,
                    # assume this nameless alv=false is mine (my own broadcast without 255)
                    if not alv_player and pkt_room_id and isinstance(pkt, dict) and pkt.get("is_alive") is False:
                        _pa2, _pt2 = _alv_actors
                        if _pa2 is None and _pt2 is None:  # Truly anonymous — heuristic allowed
                            try:
                                room_alv = _get_room(pkt_room_id, view)  # Resolve room
                                if room_alv and me_name:  # Have room and know my name
                                    for pp in room_alv.players.values():  # Check each player
                                        if _names_equal(pp.username, me_name) and not pp.is_alive:  # My player is dead
                                            alv_player = me_name  # Found myself
                                            alv_is_alive = False  # Confirm dead
                                            _alv_via = "assume-mine heuristic"
                                            log_engine("proc", session_id[:13], action="alv-assume-mine", name=me_name)
                                            break                # Stop searching
                            except Exception:
                                pass   # Silently ignore lookup errors
                        else:
                            log_sys(f"PROCESSOR alv actors={(_pa2, _pt2)} unmapped — not mine, skip")

                    # Check if this alv packet indicates death
                    is_dead_alv = (alv_is_alive is False) or bool(alv_is_elim)
                    if is_dead_alv and not _me_hosting_only and alv_player and _is_self(_get_room(pkt_room_id, view), view, user, alv_player):  # I died
                        _ar0 = _get_room(pkt_room_id, view)
                        _arid = getattr(_ar0, "room_id", None) or pkt_room_id
                        _arv = getattr(_ar0, "match_version", None)
                        _arv = _arv if _arv is not None else pkt_match_ver
                        dedup_key_alv = (discord_id, _arid, _arv)
                        if dedup_key_alv not in death_counted_for_match:  # Not yet counted
                            room_alv = _get_room(pkt_room_id, view)  # Resolve room
                            # One death per life: stale alv=false repeats (still dead from
                            # an earlier counted death, never revived) must not recount
                            if room_alv and not room_alv.had_self_death:
                                old_deaths_alv = user.get("deaths", 0)  # Pre-increment value for the log
                                new_deaths_alv = _bump_user(user, dirty, "deaths")  # Queued for batch-end flush
                                # Queue death event (with "via": "alv" to track source)
                                _buf_event(ev_rows, discord_id, session_id, "death",
                                           {"gm": eff_gm, "killer": None, "via": "alv",
                                            "room_id": pkt_room_id, "code": pkt_short, "match_version": pkt_match_ver})
                                log_engine("proc", session_id[:13], action="death", via="alv",
                                           name=alv_player, deaths=f"{old_deaths_alv}->{new_deaths_alv}",
                                           actors=str(_alv_actors))
                                death_counted_for_match.add(dedup_key_alv)  # Mark as counted
                                _precision.schedule("death", session_id=session_id, discord_id=discord_id,
                                                    expected=new_deaths_alv, match_version=pkt_match_ver,
                                                    room_id=pkt_room_id, extra={"via": "alv"})
                                room_alv.had_self_death = True  # One death per life
                                # Wins-only: no immediate defeat (deaths still count above).
                            else:
                                log_sys(f"PROCESSOR DEATH via alv skip already counted this life")
                except Exception as e:
                    log_err(f"processor alv death failed {e}")

            # --- Join events (new player joined room) ---
            # No DB row: the engine tracks players/rooms in-memory and the
            # batch-end roster sync persists membership. Only stG, alv,
            # killfeed and leave produce stored events.
            if is_join or pkt_type in ("join", "JOIN", "Join", "create_room", "join_room", "join_random_room"):
                joiner = player_name or instigator  # Who joined
                if joiner:             # Have a valid name
                    log_sys(f"PROCESSOR JOIN {joiner} gm={eff_gm}")

            # --- Leave events (player left or was kicked) ---
            if is_leave or pkt_type in ("leave", "LEAVE", "Leave"):
                leaver = player_name or instigator  # Who left
                if leaver:              # Have a valid name
                    # Queue as killfeed event with kill_type="left"
                    _buf_event(ev_rows, discord_id, session_id, "killfeed",
                               {"gm": eff_gm, "instigator": leaver, "instigated": None,
                                "kill_type": "left", "room_id": pkt_room_id, "code": pkt_short,
                                "match_version": pkt_match_ver})
                    # Queue as discrete leave event (separate from killfeed)
                    _buf_event(ev_rows, discord_id, session_id, "leave",
                               {"gm": eff_gm, "player": leaver, "player_name": leaver,
                                "room_id": pkt_room_id, "code": pkt_short, "match_version": pkt_match_ver})
                    log_sys(f"PROCESSOR LEAVE {leaver} gm={eff_gm}")

                    # Host leave destroys the Photon room (creator gone) -> the
                    # ranked session can never continue or seal. Auto-end the
                    # DB room so it doesn't linger as a lobby/live zombie and
                    # clients polling by-code see ended instead of a joinable
                    # ghost. Independent of defeat bookkeeping (fires in any
                    # state/mode — the room is gone regardless).
                    try:
                        if pkt_short and _names_equal(leaver, me_name):
                            from server.lib import ranked as _rk_leave
                            _lr = _rk_leave.get_ranked_room_fresh(str(pkt_short).upper())
                            if (_lr and str(_lr.get("status") or "") in ("lobby", "live")
                                    and str(_lr.get("host_discord_id") or "") == str(discord_id or "")):
                                supabase.table("ranked_rooms").update({"status": "ended", "short_code": None}).eq("id", str(_lr.get("id"))).execute()
                                log_ranked(f"[ranked] room {_lr.get('id')} auto-ended + code cleared (host left)")
                    except Exception as e_rl:
                        log_err(f"processor host-leave auto-end failed {e_rl}")

                    # Wins-only: no defeat via leave (leave events still buffered
                    # above for walkover detection).

                    # Walkover win: whole enemy team left (duo/small-team modes + 1v1s).
                    # My own leave never walkovers (wins-only: no defeat via leave).
                    try:
                        _wo_gm = str(eff_gm or "").lower()
                        _wo_room = _get_room(pkt_room_id, view)  # Resolve independently (room2 is self-leave scoped)
                        if (not _me_hosting_only and _wo_gm and not _is_kd_only(eff_gm) and _wo_room is not None
                                and _wo_room.state.value == "match" and me_name
                                and not _names_equal(leaver, me_name)
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
                                await db_io(_write_win, discord_id, session_id, user, eff_gm, pkt_room_id, pkt_short, _wo_room.match_version, _wo_winner, _wo_room, ev_rows, dirty)
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
                # Track phase changes in-memory only (engine serves the live
                # zone; only stG, alv, killfeed and leave produce stored events)
                if sane_phase and str(zone_phase) != str(last_zone_phase):
                    log_sys(f"PROCESSOR ZONE {last_zone_phase}→{zone_phase} time={vt}")
                    last_zone_phase = zone_phase  # Update tracker for next comparison

        except Exception as exc:
            log_err(f"processor packet write failed {exc}")  # Log and continue to next packet

    # Save zone phase tracker on view (persists across batches)
    try:
        view._lastZonePhase = last_zone_phase  # Store for next batch
    except Exception:
        pass                         # Silently ignore if view is read-only

    # --- Ranked version continuity ---
    # Fresh Photon rooms start match_version at 0, but seals are keyed per
    # ranked session — fast-forward past already-sealed versions so the next
    # stG=3 seals a new version instead of colliding (and being skipped).
    # Self-limiting (only v0 rooms) and idempotent; lobby rooms cost nothing
    # (no packets means no batches).
    try:
        for _avr_id, _avr in list((view.rooms or {}).items()):
            try:
                if _avr_id == "lobby" or getattr(_avr, "match_version", 0) != 0:
                    continue
                _avc = getattr(_avr, "short_code", None)
                if not _avc:
                    continue
                from server.lib import ranked as _rk_av
                _avrd = await db_io(_rk_av.get_ranked_room, str(_avc).upper())
                if not _avrd or str(_avrd.get("status") or "") not in ("lobby", "live"):
                    continue
                _avcnt = await db_io(_sealed_count, _avrd.get("id"))
                _newv = _aligned_match_version(0, _avcnt)
                if _newv:
                    _avr.match_version = int(_newv)
                    log_ranked(f"[ranked] version align {_avrd.get('id')} room={str(_avr_id)[:8]} -> v{_newv} (continuity past sealed)")
            except Exception:
                continue
    except Exception as exc:
        log_err(f"processor version align failed {exc}")

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
        """Skip the lobby pseudo-room always (it can never produce a valid
        win: its version never advances, so an arm there is undrainable),
        plus lobby rooms with no game mode and no pending win.
        This prevents processing lobby-only batches that have no match data.
        """
        if r.room_id == "lobby":
            return True
        return not r.game_mode and r.last_win_version != r.match_version

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
                room.stg_is_phase_end = False  # resolved — don't let a stale arm linger
                continue  # Already recorded

            # Check if stG=3 just happened as a real phase end (not a zone-timer tick
            # through 3): engine bumps match_version and arms stg_is_phase_end only then.
            # Edge-triggered: the arm survives stG moving to 2/1 afterwards, so a batch
            # containing [3,2] still drains. Consumed on write; cleared on MATCH enter.
            if room.stg_is_phase_end and not room.match_version:
                # armed but version never bumped: this session missed the real
                # stG=3 (or the engine never armed it) — wins can never drain.
                # Log once per room to spot version-stuck sessions.
                try:
                    if getattr(room, "_win_ver0_logged", False) is not True:
                        room._win_ver0_logged = True
                        log_sys(f"PROCESSOR WIN arm-no-version room={str(room_id or '')[:8]} state={getattr(getattr(room, 'state', None), 'value', '?')} — session missed stG=3, wins blocked")
                except Exception:
                    pass
            if room.stg_is_phase_end and room.match_version > 0:
                # Match just ended — check who's alive to determine winner.
                # _phase_end_result matches the live rules (tracked alive, else
                # duo-team incl. presumed-alive quiet members) and falls back to
                # killfeed/flag evidence when the room already emptied.
                me_name_local = (view.me.username if view.me else None) or user.get("game_username")
                # Armed phase ends drain even when the FSM already exited on
                # an intermission blip: the arm proves a real stG=3 fired in
                # this room (continuous party series cycle 3->2->1 with no
                # lobby return), and leavers are excluded by the guard in
                # _phase_end_result above — never by state alone.
                if me_name_local and (room.state.value == "match" or room.stg_is_phase_end) and not _me_hosting_only:
                    if _phase_end_result(room, view, me_name_local) == "win":
                        # I'm alive → WIN (helper marks version so it records once)
                        winner = me_name_local
                        # Team winner only in real duo rooms: a stale _pt team
                        # map (mode changed mid-room) must never rename a solo
                        # winner to a team_id.
                        if room.teams and room.is_duo:
                            my_team = room.get_team(me_name_local)
                            if my_team:
                                winner = my_team.team_id
                        await db_io(_write_win, discord_id, session_id, user, gm, room_id, room.short_code, room.match_version, winner, room, ev_rows, dirty)
                        await db_io(seal_ranked_match, session_id, discord_id, room, room_id, gm)  # host only; no-op otherwise
                        room.stg_is_phase_end = False  # consume arm (version bump re-arms)
                    else:
                        # denied: say why (once per version — the arm stays up
                        # and would otherwise spam every batch)
                        try:
                            _dv = (room_id, getattr(room, "match_version", 0))
                            if getattr(room, "_win_deny_logged", None) != _dv:
                                room._win_deny_logged = _dv
                                # Wins-only: no defeat row exists, so the
                                # DEFEAT line is the only record that the
                                # tracked player actually lost this match.
                                _df = _defeat_fields(room, view, me_name_local, gm)
                                if _df:
                                    log_match("DEFEAT", tag="DEFEAT", event="defeat",
                                              session=session_id, **_df)
                                log_sys(f"PROCESSOR WIN denied me={me_name_local} room={str(room_id or '')[:8]} gm={gm} { _phase_end_debug(room, view, me_name_local)}")
                        except Exception:
                            pass

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
            if not _me_hosting_only and my_k >= 15 and room4.last_win_version != room4.match_version:
                await db_io(_write_win, discord_id, session_id, user, gm4, room_id4, room4.short_code, room4.match_version, my_tid4, room4, ev_rows, dirty)
                log_sys(f"PROCESSOR WIN via race-to-15 team={my_tid4} {my_k}-{foe_best} gm={gm4}")
            # Wins-only: foe hitting 15 is no-win (no defeat).
    except Exception as exc:
        log_err(f"processor race-to-15 failed {exc}")

    # --- Phase-end seal stub (wins-only): no defeat sweep/drain. Deaths
    # still count via killfeed/alv live paths above. Ranked sealing unchanged.
    try:
        for room_id2, room2 in list(view.rooms.items()):
            if _should_skip_room(room2):
                continue
            gm2 = room2.game_mode.value if room2.game_mode else gm_val
            if gm2 and gm2.lower() == "df":
                continue
            if room2.last_defeat_version == room2.match_version and room2.last_defeat_version != -1:
                room2.last_defeat_version = -1  # discard stale defeat marker
            # Armed ends seal even when the FSM already exited on an
            # intermission blip (continuous party series) — the arm, not the
            # state, proves the phase end fired here.
            if room2.stg_is_phase_end and room2.match_version > 0:
                await db_io(seal_ranked_match, session_id, discord_id, room2, room_id2, gm2)
                room2.stg_is_phase_end = False
    except Exception as exc:
        log_err(f"processor phase-end seal stub failed {exc}")

    # --- Ranked seal sweep: one host call per phase end ---
    # Playing hosts usually sealed already via the win/defeat drains
    # above (idempotent no-ops here). hosting_only hosts never drain a
    # win/defeat, so without this their players' kills would never seal.
    # seal_ranked_match skips the spectating host from awards itself.
    # Armed ends seal regardless of FSM state (intermission blip, see above).
    try:
        for room_id_s, room_s in list(view.rooms.items()):
            if room_s.room_id == "lobby":
                continue
            if room_s.stg_is_phase_end and room_s.match_version > 0:
                await db_io(seal_ranked_match, session_id, discord_id, room_s, room_id_s, gm_val)
                room_s.stg_is_phase_end = False
    except Exception as exc:
        log_err(f"processor ranked sweep failed {exc}")

    # --- Comp seal sweep: every session (host or not) feeds the bridge ---
    # seal_comp_match gates on room short_code → tourney, opens the
    # match row if needed, banks unmapped kill events, seals on phase
    # end. Idempotent; never raises.
    try:
        for room_id_c, room_c in list(view.rooms.items()):
            if getattr(room_c, "room_id", None) == "lobby":
                continue
            if not getattr(room_c, "short_code", None):
                continue
            try:
                await db_io(seal_comp_match, session_id, discord_id, room_c, room_id_c, gm_val)
            except Exception:
                pass
    except Exception as exc:
        log_comp(f"sweep failed ({exc})")

    # --- Ranked-tourney seal sweep: same stG 1 / stG 3 bridge, ranked tables ---
    # seal_ranked_tourney_match gates on room short_code → ranked tourney.
    # Its own by-code lookup returns zero rows for comp codes (and vice versa),
    # so a room never double-fires. Idempotent; never raises.
    try:
        for room_id_r, room_r in list(view.rooms.items()):
            if getattr(room_r, "room_id", None) == "lobby":
                continue
            if not getattr(room_r, "short_code", None):
                continue
            try:
                _cur = getattr(view, "current_room_id", None)
            except Exception:
                _cur = None
            try:
                await db_io(seal_ranked_tourney_match, session_id, discord_id, room_r, room_id_r, gm_val,
                            room_id_r == _cur)
            except Exception:
                pass
    except Exception as exc:
        log_comp(f"rt sweep failed ({exc})")

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
                    existing = await db_io(lambda: supabase.table("competitive_matches").select("match_id").eq("match_id", f"{room_id3}_{room3.match_version}").limit(1).execute())
                    if not existing.data:    # No existing record → create
                        total_players = len(room3.players)  # Count players
                        total_teams = len(room3.teams) if room3.teams else 1  # Count teams
                        # Insert competitive match record
                        await db_io(lambda: supabase.table("competitive_matches").insert({
                            "match_id": f"{room_id3}_{room3.match_version}",  # room_id_version
                            "host_pfid": view.me.game_id if view.me else None,  # Host's player ID
                            "verified_by_server": False,  # Not yet verified
                            "total_players": total_players,
                            "total_teams": total_teams,
                        }).execute())
                        # Upsert sealed match record
                        await db_io(lambda: supabase.table("matches").upsert({
                            "match_id": f"{room_id3}_{room3.match_version}",
                            "match_type": "private_casual" if not room3.teams else "competitive",
                            "game_mode": room3.game_mode.value if room3.game_mode else "unknown",
                            "scene": room3.cur_scn,
                            "host_pfid": view.me.game_id if view.me else None,
                            "is_sealed": False,   # Not sealed yet (sealed on host upload)
                        }, on_conflict="match_id").execute())
                        log_sys(f"PROCESSOR competitive {room_id3[:8]} version={room3.match_version}")
                except Exception as e:
                    log_err(f"processor competitive failed {room_id3[:8]} {e}")
    except Exception as e:
        log_err(f"processor competitive outer failed {e}")

    # --- v2.19.0 streaks: batch-end accrual (queued into flush) ---
    # Queues daily/win/kill counter updates; the ladder payout is gated by
    # PAY_STREAK_BONUS (v1: off — seals are the only ladder movers).
    # Best-effort. Never breaks the batch.
    try:
        _streak_bonus = _apply_streaks(user, dirty, ev_rows, view)
        if _streak_bonus and PAY_STREAK_BONUS:
            try:
                from server.lib import ranked as _rk_pay
                _cur_pts = _rk_pay.get_ranked_points(discord_id)
                _rk_pay._bump_points_many({str(discord_id): int(_streak_bonus)})
                log_sys(f"PROCESSOR streak bonus did={discord_id[:6]} +{_streak_bonus} pts={_cur_pts}+{_streak_bonus}")
            except Exception as _e_pay:
                log_err(f"processor streak payout failed {_e_pay}")
    except Exception as _e_st:
        log_err(f"processor streaks failed {_e_st}")

    # --- Batch-end flush: ONE bulk event insert + ONE user update ---
    # All killfeed/kill/death/leave/win/defeat rows queued above go out
    # in a single query, and all counter + field updates merge into one.
    # Stuck-player tripwire: valid kills nobody got credit for. A healthy
    # session sits near zero; a drifting-identity client pins this high
    # while playtime keeps counting — investigate before hours pass.
        try:
            # Thresholded: a single foreign kill in a batch is normal play,
            # not broken identity — only pine when nothing credits across a
            # whole lobby's worth of kills.
            if _kf_unattributed >= 5 and _kf_credited == 0:
                log_engine("proc", session_id[:13], action="unattributed",
                           unattributed=_kf_unattributed, credited=0, me=me_name or "?")
        except Exception:
            pass
    # DB write summary: one SYS line per gameplay batch so a silent
    # write stall (rows buffered but never landing) is visible in one
    # grep instead of inferred from missing counters hours later.
    _n_rows = 0
    try:
        _n_rows = len(ev_rows)
    except Exception:
        pass
    _ev_flushed, _ev_dropped = 0, 0
    try:
        _ev_flushed, _ev_dropped = await db_io(_flush_events, ev_rows) or (0, 0)
    except Exception as exc:
        log_err(f"processor event flush failed {exc}")
        try:
            _ev_dropped = _n_rows
        except Exception:
            pass
    _u_ok = True
    try:
        await db_io(_flush_user_counters, discord_id, pending_user_updates, dirty, user)
    except Exception as exc:
        _u_ok = False
        log_err(f"processor user flush failed {exc}")
    try:
        if _n_rows or (dirty or set()):
            log_engine("proc", session_id[:13], action="db", pkts=len(packets),
                       rows=_n_rows, ev_ok=_ev_flushed, ev_dropped=_ev_dropped,
                       user_ok=_u_ok, dirty=",".join(sorted(dirty or [])) or "-",
                       kills=user.get("kills", 0), deaths=user.get("deaths", 0),
                       wins=user.get("wins", 0))
    except Exception:
        pass

    # --- Playtime sync ---
    # Aggregate per-room playtime into bngc_users and reset counters
    try:
        await db_io(_sync_playtime, view, user, discord_id)  # Call helper
    except Exception as exc:
        log_err(f"processor playtime failed {exc}")

    # --- Sync to bngc_player_stats ---
    # Upsert stats table so SELECT * FROM bngc_player_stats is populated
    try:
        await db_io(_upsert_stats, discord_id, user)  # Call helper
        log_sys(f"PROCESSOR sync player_stats did={discord_id[:6]} kills={user.get('kills',0)} defeats={user.get('defeats',0)} wins={user.get('wins',0)}")
    except Exception as exc:
        log_err(f"processor player_stats sync failed {exc}")


    # Log final stats after processing all packets (ALWAYS — every batch)
    log_sys(f"PROCESSOR DONE session={session_id[:13]} kills={user.get('kills',0)} "
            f"deaths={user.get('deaths',0)} wins={user.get('wins',0)} defeats={user.get('defeats',0)}")
