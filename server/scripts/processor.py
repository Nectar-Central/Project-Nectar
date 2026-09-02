"""
processor.py — Your game logic: kills, deaths, wins, playtime → DB.

This is called *after* Engine has parsed and mutated SessionView.
Engine does NO DB writes, no win math — it just gives you:
  - packets: list[PhotonPacket dict]  (per-hex, with killer/victim/is_kill)
  - view: SessionView               (per-session, live players/zone/me)

We query the Engine classes directly (view.me, view.players, view.zone, view.game_mode)
instead of re-parsing abstraction packets. Packets are only for transient events
(killfeed). User fields (username, game_id, skin) come from view.me.

Called via FastAPI BackgroundTasks in POST /api/capture, so the
userscript gets its new_req_token fast (<20ms), and DB writes
happen async (~50ms). If DB is slow, POST still succeeds.
"""

from __future__ import annotations

from server.lib.supabase_client import supabase, db
from server.lib.logger import log_sys, log_err

# For type hints only — not imported at runtime to avoid cycle
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.lib.photon_engine.models import SessionView, AbstractPacket


def _extract_fields(pkt):
    """Extract common fields from packet (dict or object) — handles PhotonPacket shape."""
    if isinstance(pkt, dict):
        # PhotonPacket.to_dict() uses "packet" and "packet_type" both; prefer "packet"
        pkt_type = pkt.get("packet") or pkt.get("packet_type")
        kill = pkt.get("kill")
        if kill:
            instigator = kill.get("instigator")
            instigated = kill.get("instigated")
            kill_type = kill.get("event_type")
        else:
            instigator = pkt.get("killer") or pkt.get("instigator")
            instigated = pkt.get("victim") or pkt.get("instigated")
            kill_type = None
        player_name = pkt.get("player_name")
        if not player_name:
            p = pkt.get("player")
            if isinstance(p, dict):
                player_name = p.get("username")
        zone_time = pkt.get("zone_time")
        zone_phase = pkt.get("zone_phase")
        cur_scn = pkt.get("cur_scn")
        is_leave = pkt.get("is_leave", False)
        is_kill = pkt.get("is_kill", False)
        is_knock = pkt.get("is_knock", False)
        is_join = pkt.get("is_join", False)
    else:
        pkt_type = getattr(pkt, "packet", None) or getattr(pkt, "packet_type", None)
        if hasattr(pkt_type, "value"):
            pkt_type = pkt_type.value
        kill = getattr(pkt, "kill", None)
        if kill:
            instigator = getattr(kill, "instigator", None)
            instigated = getattr(kill, "instigated", None)
            kill_type = getattr(kill, "event_type", None)
        else:
            instigator = getattr(pkt, "killer", None) or getattr(pkt, "instigator", None)
            instigated = getattr(pkt, "victim", None) or getattr(pkt, "instigated", None)
            kill_type = None
        player_name = getattr(pkt, "player_name", None)
        zone_time = getattr(pkt, "zone_time", None)
        zone_phase = getattr(pkt, "zone_phase", None)
        cur_scn = getattr(pkt, "cur_scn", None)
        is_leave = getattr(pkt, "is_leave", False)
        is_kill = getattr(pkt, "is_kill", False)
        is_knock = getattr(pkt, "is_knock", False)
        is_join = getattr(pkt, "is_join", False)

    return pkt_type, instigator, instigated, kill_type, player_name, zone_time, zone_phase, cur_scn, is_leave, is_kill, is_knock, is_join


async def process_batch(
    session_id: str,
    packets: list,
    view: "SessionView",
) -> None:
    """
    Process a batch of packets for one session_id.

    Queries Engine classes directly: view.me, view.players, view.zone, view.game_mode.

    Writes to bngc_game_events:
      - killfeed: all kill/knock events (entire lobby, not just self)
      - kill/death: self-specific for bngc_users stat tracking
      - zone_change: zone phase transitions
      - win/defeat: end-of-match results

    Also syncs bngc_users fields from SessionView (username, game_id, skin, pickaxe)
    so /info and /stats show live data. game_mode is transient (view only, not DB).
    """

    if not session_id or not packets or not view:
        return

    log_sys(f"PROCESSOR START session={session_id[:8]} packets={len(packets)} state={view.state.value if view else '?'} gm={view.game_mode.value if view.game_mode else '?'} me={view.me.username if view.me else '?'} players={view.players_count}")

    # Fetch discord_id once for this batch
    try:
        sess_res = db.get_session_sync(session_id)
        sess_data = sess_res.get("data")
        discord_id = sess_data.get("discord_id") if sess_data else None
        if not discord_id:
            log_err(f"processor: no discord_id for session {session_id[:8]}")
            return
    except Exception as exc:
        log_err(f"processor: get session failed {session_id[:8]} {exc}")
        return

    # Fetch user once (for kills/wins/playtime)
    try:
        user_res = supabase.table("bngc_users").select("*").eq("discord_id", discord_id).single().execute()
        user = user_res.data
        if not user:
            log_err(f"processor: no user for discord_id {discord_id[:6]}")
            return
    except Exception as exc:
        log_err(f"processor: get user failed {discord_id[:6]} {exc}")
        return

    log_sys(f"PROCESSOR user={discord_id[:6]} kills={user.get('kills',0)} deaths={user.get('deaths',0)} wins={user.get('wins',0)}")

    me_name = view.me.username if view.me else None
    gm_val = view.game_mode.value if view.game_mode else None
    last_zone_phase = getattr(view, "_lastZonePhase", None)
    # helper: validate zone_time 0-600, cap 299 for zw_lg like engine
    def _valid_zone_time(v, gm_hint):
        try:
            iv = int(float(v))
            if 0 <= iv <= 600:
                if iv == 300 and str(gm_hint or "").lower() in ("zw_lg", "wwzw"):
                    return 299
                return iv
        except Exception:
            pass
        return None

    # -------------------------------------------------------------------
    # Sync user fields from SessionView (so /info, /stats are live)
    # bngc_users has no game_mode column — store username/id/skin/pickaxe only
    # -------------------------------------------------------------------
    try:
        updates: dict = {}
        if view.me:
            if view.me.username and view.me.username != user.get("game_username"):
                updates["game_username"] = view.me.username
            if view.me.game_id and view.me.game_id != user.get("game_id"):
                updates["game_id"] = view.me.game_id
            if view.me.skin and view.me.skin != user.get("skin"):
                updates["skin"] = view.me.skin
            if view.me.pickaxe and view.me.pickaxe != user.get("pickaxe"):
                updates["pickaxe"] = view.me.pickaxe
        if updates:
            supabase.table("bngc_users").update(updates).eq("discord_id", discord_id).execute()
            log_sys(f"PROCESSOR sync user {discord_id[:6]} {updates}")
            # Keep local copy in sync
            user.update(updates)
    except Exception as exc:
        log_err(f"processor sync user failed {exc}")

    # -------------------------------------------------------------------
    # Per-packet: killfeed events (ALL kills/knocks, not just self)
    # Query packets for transient events, view for persistent state.
    # -------------------------------------------------------------------

    for pkt in packets:
        pkt_type, instigator, instigated, kill_type, player_name, zone_time, zone_phase, cur_scn, is_leave, is_kill, is_knock, is_join = _extract_fields(pkt)

        # Per-packet room context stamped by Engine (avoids batch-final gm merging)
        pkt_room_id = pkt.get("room_id") or pkt.get("room_code") if isinstance(pkt, dict) else getattr(pkt, "room_id", None)
        pkt_short = pkt.get("short_code") if isinstance(pkt, dict) else getattr(pkt, "short_code", None)
        pkt_match_ver = pkt.get("match_version") if isinstance(pkt, dict) else getattr(pkt, "match_version", None)
        pkt_room_gm = pkt.get("room_gm") if isinstance(pkt, dict) else getattr(pkt, "room_gm", None)
        # Fallback chain: packet game_mode -> room_gm -> global gm_val
        pkt_gm = pkt.get("game_mode") if isinstance(pkt, dict) else getattr(pkt, "game_mode", None)
        if isinstance(pkt_gm, dict):
            pkt_gm = pkt_gm.get("value") if "value" in pkt_gm else str(pkt_gm)
        eff_gm = pkt_gm or pkt_room_gm or gm_val

        log_sys(f"PROCESSOR pkt type={pkt_type} ig={instigator} ed={instigated} kill={is_kill} knock={is_knock} leave={is_leave} me={me_name} gm={eff_gm} room={str(pkt_room_id or '')[:8]}")

        try:
            # --- Killfeed: kill or knock (entire lobby) ---
            is_kf = is_kill or is_knock or pkt_type in ("kill", "KILL", "Kill", "knock", "KNOCK", "Knock")
            if is_kf:
                if instigator and instigated:
                    kf_type = "killed" if (is_kill or pkt_type in ("kill", "KILL", "Kill")) else "knocked"
                    supabase.table("bngc_game_events").insert({
                        "discord_id": discord_id,
                        "session_id": session_id,
                        "event_type": "killfeed",
                        "event_data": {
                            "gm": eff_gm,
                            "instigator": instigator,
                            "instigated": instigated,
                            "kill_type": kf_type,
                            "room_id": pkt_room_id,
                            "code": pkt_short,
                            "short_code": pkt_short,
                            "match_version": pkt_match_ver,
                        },
                        
                    }).execute()

                    # Self-specific kill/death for bngc_users stat tracking
                    if me_name:
                        if instigator == me_name:
                            new_kills = (user.get("kills") or 0) + 1
                            supabase.table("bngc_users").update({"kills": new_kills}).eq("discord_id", discord_id).execute()
                            supabase.table("bngc_game_events").insert({
                                "discord_id": discord_id,
                                "session_id": session_id,
                                "event_type": "kill",
                                "event_data": {"gm": eff_gm, "victim": instigated, "room_id": pkt_room_id, "code": pkt_short, "match_version": pkt_match_ver},
                                
                            }).execute()
                            log_sys(f"PROCESSOR KILL by self gm={eff_gm} victim={instigated} session={session_id[:8]} kills {user.get('kills',0)}->{new_kills}")
                            user["kills"] = new_kills

                        elif instigated == me_name:
                            new_deaths = (user.get("deaths") or 0) + 1
                            supabase.table("bngc_users").update({"deaths": new_deaths}).eq("discord_id", discord_id).execute()
                            supabase.table("bngc_game_events").insert({
                                "discord_id": discord_id,
                                "session_id": session_id,
                                "event_type": "death",
                                "event_data": {"gm": eff_gm, "killer": instigator, "room_id": pkt_room_id, "code": pkt_short, "match_version": pkt_match_ver},
                                
                            }).execute()
                            log_sys(f"PROCESSOR DEATH by {instigator} session={session_id[:8]} deaths {user.get('deaths',0)}->{new_deaths}")
                            user["deaths"] = new_deaths
                            # Defeat fallback: kill victim is me while pcount>1 and no stG — alv may be missing
                            # Set defeat immediately even without alv=false, so we never miss a loss
                            try:
                                gm_low_death = str(eff_gm or "").lower()
                                if gm_low_death and gm_low_death != "df":
                                    room_obj = view.rooms.get(pkt_room_id) if pkt_room_id else None
                                    # fallback to current room if packet room not found
                                    if not room_obj:
                                        room_obj = view.current_room
                                    if room_obj and room_obj.state.value == "match" and room_obj.players_count > 1:
                                        if room_obj.last_defeat_version != room_obj.match_version:
                                            room_obj.last_defeat_version = room_obj.match_version
                                            log_sys(f"PROCESSOR DEFEAT via kill gm={eff_gm} room={str(pkt_room_id or '')[:8]} victim=me")
                                            room_obj.recent_kills.append(__import__("server.lib.photon_engine.models", fromlist=["KillInfo"]).KillInfo(instigator=instigator, instigated=me_name, event_type="defeat"))
                            except Exception as e_kd:
                                log_err(f"processor defeat-via-kill failed {e_kd}")

            # --- Join events (new player joined room) — persist so lobby history is visible ---
            if is_join or pkt_type in ("join", "JOIN", "Join", "create_room", "join_room", "join_random_room"):
                joiner = player_name or instigator or pkt.get("player_name") if isinstance(pkt, dict) else getattr(pkt, "player_name", None)
                # _extract_fields already gave player_name for join; fallback to instigator
                joiner = joiner or player_name or instigator
                if joiner and pkt_type in ("join", "JOIN", "Join", "create_room", "join_room", "join_random_room"):
                    # Deduplicate: only if joiner not already inserted in last batch for same room
                    try:
                        supabase.table("bngc_game_events").insert({
                            "discord_id": discord_id,
                            "session_id": session_id,
                            "event_type": "join",
                            "event_data": {
                                "gm": eff_gm,
                                "player": joiner,
                                "player_name": joiner,
                                "room_id": pkt_room_id,
                                "code": pkt_short,
                                "short_code": pkt_short,
                                "match_version": pkt_match_ver,
                            },
                        }).execute()
                        # Also as killfeed joined for replay visibility
                        supabase.table("bngc_game_events").insert({
                            "discord_id": discord_id,
                            "session_id": session_id,
                            "event_type": "killfeed",
                            "event_data": {
                                "gm": eff_gm,
                                "instigator": joiner,
                                "instigated": None,
                                "kill_type": "joined",
                                "room_id": pkt_room_id,
                                "code": pkt_short,
                                "short_code": pkt_short,
                                "match_version": pkt_match_ver,
                            },
                        }).execute()
                        log_sys(f"PROCESSOR JOIN {joiner} session={session_id[:8]} gm={eff_gm} room={str(pkt_room_id or '')[:8]}")
                    except Exception as exc_j:
                        log_err(f"processor join write failed {joiner}: {exc_j}")

            # --- Leave events (from packet.is_leave or leave type) ---
            if is_leave or pkt_type in ("leave", "LEAVE", "Leave"):
                leaver = player_name or instigator
                if leaver:
                    supabase.table("bngc_game_events").insert({
                        "discord_id": discord_id,
                        "session_id": session_id,
                        "event_type": "killfeed",
                        "event_data": {
                            "gm": eff_gm,
                            "instigator": leaver,
                            "instigated": None,
                            "kill_type": "left",
                            "room_id": pkt_room_id,
                            "code": pkt_short,
                            "match_version": pkt_match_ver,
                        },
                        
                    }).execute()
                    # Also as discrete leave event
                    try:
                        supabase.table("bngc_game_events").insert({
                            "discord_id": discord_id,
                            "session_id": session_id,
                            "event_type": "leave",
                            "event_data": {
                                "gm": eff_gm,
                                "player": leaver,
                                "player_name": leaver,
                                "room_id": pkt_room_id,
                                "code": pkt_short,
                                "match_version": pkt_match_ver,
                            },
                        }).execute()
                    except Exception:
                        pass
                    log_sys(f"PROCESSOR LEAVE {leaver} session={session_id[:8]} gm={eff_gm}")
                    # Defeat via leave: did I leave / get kicked during MATCH with pcount>1
                    # Without alv=false, this is the only signal for early leave-loss
                    try:
                        if leaver == me_name:
                            gm_low_lv = str(eff_gm or "").lower()
                            if gm_low_lv and gm_low_lv != "df":
                                room_obj2 = view.rooms.get(pkt_room_id) if pkt_room_id else None
                                if not room_obj2:
                                    room_obj2 = view.current_room
                                if room_obj2 and room_obj2.state.value == "match" and room_obj2.players_count > 0:
                                    if room_obj2.last_defeat_version != room_obj2.match_version:
                                        room_obj2.last_defeat_version = room_obj2.match_version
                                        log_sys(f"PROCESSOR DEFEAT via leave gm={eff_gm} room={str(pkt_room_id or '')[:8]}")
                                        room_obj2.recent_kills.append(__import__("server.lib.photon_engine.models", fromlist=["KillInfo"]).KillInfo(instigator=leaver, instigated="", event_type="defeat"))
                    except Exception as e_ld:
                        log_err(f"processor defeat-via-leave failed {e_ld}")

            # --- Zone phase changes (compare to last seen) ---
            # Only valid bz_* zone phases, ignore stG-derived huge times for zw_lg
            if zone_phase is not None:
                # Validate time 0-600; huge stG values like 3949545 are dropped
                vt = _valid_zone_time(zone_time, eff_gm) if zone_time is not None else None
                # If zone_time is huge and gm is zw_lg/wwzw, it's stG not zone — skip time but still track phase if phase is sane 0-5
                try:
                    phase_int = int(float(str(zone_phase).strip()))
                    sane_phase = 0 <= phase_int <= 10
                except Exception:
                    sane_phase = True
                if sane_phase and str(zone_phase) != str(last_zone_phase):
                    supabase.table("bngc_game_events").insert({
                        "discord_id": discord_id,
                        "session_id": session_id,
                        "event_type": "zone_change",
                        "event_data": {
                            "gm": eff_gm,
                            "phase": str(zone_phase),
                            "time": vt,
                            "room_id": pkt_room_id,
                            "code": pkt_short,
                            "match_version": pkt_match_ver,
                        },
                        
                    }).execute()
                    log_sys(f"PROCESSOR ZONE phase {last_zone_phase}→{zone_phase} time={vt} gm={eff_gm} session={session_id[:8]}")
                    last_zone_phase = zone_phase
                elif not sane_phase:
                    log_sys(f"PROCESSOR ZONE ignore insane phase {zone_phase} time={zone_time}")

        except Exception as exc:
            log_err(f"processor packet write failed {exc}")

    # Save zone phase tracker on view
    try:
        view._lastZonePhase = last_zone_phase  # type: ignore
    except Exception:
        pass

    # -------------------------------------------------------------------
    # Per-room: win / defeat — drain RoomView.winner / last_win/defeat_version per room/match_version
    # Handles: zw_lg/wwzw stG 3→1, BF2V2 team elimination (leave counts), DF K/D only (no win)
    # -------------------------------------------------------------------

    try:
        for room_id, room in view.rooms.items():
            # Skip lobby rooms
            if room.room_id == "lobby":
                continue
            gm = room.game_mode.value if room.game_mode else gm_val
            gm_low_room = str(gm).lower() if gm else ""

            # DF — no win/defeat, just K/D already handled per-packet
            if gm_low_room == "df":
                continue

            # Check per-room winner set by Engine (stG 3→1 or last alive)
            # room.winner is username or team_id of last alive, room.last_win_version indicates win for me
            if room.winner and room.last_win_version == room.match_version:
                # Only count win for me if I am winner (or my team)
                is_my_win = False
                if view.me and room.winner == view.me.username:
                    is_my_win = True
                elif view.me and room.get_team(view.me.username) and room.winner == room.get_team(view.me.username).team_id:
                    is_my_win = True
                # Also check if winner is my team via _pt
                if is_my_win:
                    # Dedup: check if win already inserted for this room+version
                    try:
                        existing = supabase.table("bngc_game_events").select("id").eq("session_id", session_id).eq("event_type", "win").eq("event_data->>gm", gm).limit(1).execute()
                        # Simple dedup via user wins increment once per match_version
                        pass
                    except: pass
                    new_wins = (user.get("wins") or 0) + 1
                    supabase.table("bngc_users").update({"wins": new_wins}).eq("discord_id", discord_id).execute()
                    supabase.table("bngc_game_events").insert({
                        "discord_id": discord_id,
                        "session_id": session_id,
                        "event_type": "win",
                        "event_data": {"gm": gm, "room_id": room_id, "short_code": room.short_code, "match_version": room.match_version, "winner": room.winner}, 
                    }).execute()
                    log_sys(f"PROCESSOR WIN gm={gm} room={room_id[:8]} short={room.short_code} winner={room.winner} session={session_id[:8]} wins {user.get('wins',0)}->{new_wins}")
                    user["wins"] = new_wins
                    # Prevent re-counting same win version
                    room.last_win_version = -1  # mark drained
                    continue

            # Fallback solo last alive for zw_lg/wwzw (if winner not set but players_count==1 and me alive)
            # Guard: avoid early win when only 1 player ever seen and zero kills (mirror engine.py:798)
            if gm_low_room in ("zw", "zw_lg", "wwzw", "ww", "wwzw") and room.state.value == "match":
                if view.me and view.me.is_alive and room.players_count == 1:
                    if len(room.players) <= 1 and len(room.recent_kills) == 0:
                        pass  # early — not a real win
                    else:
                        only = next(iter(room.players.values()), None)
                        if only and only.username == view.me.username and room.last_win_version != room.match_version:
                            new_wins = (user.get("wins") or 0) + 1
                            supabase.table("bngc_users").update({"wins": new_wins}).eq("discord_id", discord_id).execute()
                            supabase.table("bngc_game_events").insert({
                                "discord_id": discord_id,
                                "session_id": session_id,
                                "event_type": "win",
                                "event_data": {"gm": gm, "room_id": room_id, "short_code": room.short_code, "match_version": room.match_version}, 
                            }).execute()
                            log_sys(f"PROCESSOR WIN gm={gm} room={room_id[:8]} session={session_id[:8]} wins {user.get('wins',0)}->{new_wins}")
                            user["wins"] = new_wins
                            room.last_win_version = room.match_version

            # Per-room defeat via RoomView.last_defeat_version (alv immediate, team elimination)
            for room_id2, room2 in view.rooms.items():
                if room2.room_id == "lobby":
                    continue
                gm2 = room2.game_mode.value if room2.game_mode else gm_val
                if gm2 and gm2.lower() == "df":
                    continue  # DF has no defeat
                if room2.last_defeat_version == room2.match_version:
                    # Already counted via Engine alv/team, drain it
                    if room2.last_defeat_version != -1:
                        new_def = (user.get("defeats") or 0) + 1
                        supabase.table("bngc_users").update({"defeats": new_def}).eq("discord_id", discord_id).execute()
                        supabase.table("bngc_game_events").insert({
                            "discord_id": discord_id,
                            "session_id": session_id,
                            "event_type": "defeat",
                            "event_data": {"gm": gm2, "room_id": room_id2, "short_code": room2.short_code, "match_version": room2.match_version}, 
                        }).execute()
                        log_sys(f"PROCESSOR DEFEAT gm={gm2} room={room_id2[:8]} session={session_id[:8]} defeats {user.get('defeats',0)}->{new_def}")
                        user["defeats"] = new_def
                        room2.last_defeat_version = -1
                    continue
                # Fallback: if me is dead and room has multiple players, count once per room
                if view.me and not view.me.is_alive and room2.players_count > 1 and room2.state.value == "match":
                    # Check if this room is where me died (me in this room's players)
                    if any(p.username == view.me.username for p in room2.players.values()):
                        if room2.last_defeat_version != room2.match_version:
                            new_def = (user.get("defeats") or 0) + 1
                            supabase.table("bngc_users").update({"defeats": new_def}).eq("discord_id", discord_id).execute()
                            supabase.table("bngc_game_events").insert({
                                "discord_id": discord_id,
                                "session_id": session_id,
                                "event_type": "defeat",
                                "event_data": {"gm": gm2, "room_id": room_id2, "short_code": room2.short_code, "match_version": room2.match_version}, 
                            }).execute()
                            log_sys(f"PROCESSOR DEFEAT gm={gm2} room={room_id2[:8]} session={session_id[:8]} defeats {user.get('defeats',0)}->{new_def}")
                            user["defeats"] = new_def
                            room2.last_defeat_version = room2.match_version
            # Competitive handling — private party with short_code, host is me (CreateRoom)
            try:
                for room_id3, room3 in view.rooms.items():
                    if room3.room_id == "lobby":
                        continue
                    # Competitive if private (short_code) and Party true and I'm host (CreateRoom)
                    if room3.short_code and room3.party.is_party and room3.me and room3.me.username == (view.me.username if view.me else None):
                        # Check if this match just ended (win or defeat just drained)
                        # Use match_version to create sealed competitive record
                        is_ended = (room3.last_win_version == -1 and room3.last_defeat_version == -1)  # just drained
                        # Actually check if recent win/defeat was just processed for this room
                        # Create competitive_matches entry if not exists
                        try:
                            # Check if already exists for this room+version
                            existing = supabase.table("competitive_matches").select("match_id").eq("match_id", f"{room_id3}_{room3.match_version}").limit(1).execute()
                            if not existing.data:
                                # Count players and teams for verification
                                total_players = len(room3.players)
                                total_teams = len(room3.teams) if room3.teams else 1
                                # Verify via engine: check kills match deaths
                                kills = len([k for k in room3.recent_kills if k.event_type == "killed"])
                                supabase.table("competitive_matches").insert({
                                    "match_id": f"{room_id3}_{room.match_version}",
                                    "host_pfid": view.me.game_id if view.me else None,
                                    "verified_by_server": False,  # will be verified on host upload
                                    "total_players": total_players,
                                    "total_teams": total_teams,
                                }).execute()
                                # Also insert into matches as sealed
                                supabase.table("matches").upsert({
                                    "match_id": f"{room_id3}_{room3.match_version}",
                                    "match_type": "private_casual" if not room3.teams else "competitive",
                                    "game_mode": room3.game_mode.value if room3.game_mode else "unknown",
                                    "scene": room3.cur_scn,
                                    "host_pfid": view.me.game_id if view.me else None,
                                    "is_sealed": False,
                                }, on_conflict="match_id").execute()
                                log_sys(f"PROCESSOR competitive {room_id3[:8]} short={room3.short_code} version={room3.match_version} players={total_players} teams={total_teams}")
                        except Exception as e2:
                            log_err(f"processor competitive failed {room_id3[:8]} {e2}")
            except Exception as e3:
                log_err(f"processor competitive outer failed {e3}")
            # Playtime sync per GM + total + lobby (from SessionView.playtime and per-room)
            try:
                # Aggregate per-room match_playtime
                per_gm_add = {}
                lobby_add = 0
                # Session-level per_gm already includes lobby vs match split, but per-room is more accurate
                for r in view.rooms.values():
                    if r.room_id == "lobby":
                        continue
                    for gm_key, secs in r.match_playtime.per_gm.items():
                        per_gm_add[gm_key] = per_gm_add.get(gm_key, 0) + secs
                    lobby_add += r.match_playtime.lobby_seconds
                # Also include session-level if rooms didn't capture (fallback)
                if not per_gm_add and view.playtime.per_gm:
                    per_gm_add = view.playtime.per_gm
                    lobby_add = view.playtime.lobby_seconds
                if per_gm_add or lobby_add:
                    # Merge into bngc_users
                    cur_per = {}
                    try:
                        import json as _js
                        cur_per = _js.loads(user.get("playtime_per_gm") or "{}")
                    except: cur_per = {}
                    for gm_k, secs in per_gm_add.items():
                        cur_per[gm_k] = cur_per.get(gm_k, 0) + secs
                    cur_per["lobby"] = cur_per.get("lobby", 0) + lobby_add
                    total_add = sum(per_gm_add.values()) + lobby_add
                    new_total = (user.get("playtime_global") or 0) + total_add
                    supabase.table("bngc_users").update({"playtime_global": new_total, "playtime_per_gm": __import__("json").dumps(cur_per)}).eq("discord_id", discord_id).execute()
                    log_sys(f"PROCESSOR playtime session={session_id[:8]} per_gm={per_gm_add} lobby={lobby_add} total+{total_add}")
                    # Reset per-room counters after flush
                    for r in view.rooms.values():
                        r.match_playtime.total_seconds = 0
                        r.match_playtime.per_gm = {}
                        r.match_playtime.lobby_seconds = 0
                    view.playtime.per_gm = {}
                    view.playtime.lobby_seconds = 0
                    view.playtime.total_seconds = 0
            except Exception as exc2:
                log_err(f"processor playtime failed {exc2}")

    except Exception as exc:
        log_err(f"processor win/defeat failed {exc}")

    log_sys(f"PROCESSOR DONE session={session_id[:8]} kills={user.get('kills',0)} deaths={user.get('deaths',0)} wins={user.get('wins',0)} defeats={user.get('defeats',0)}")
