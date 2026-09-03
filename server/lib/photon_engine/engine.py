from dataclasses import dataclass
from typing import Any

from ..gpbinary import parse_gpbinary, parse_killfeed_text, _strip_color_tags
from ..logger import log_engine

from .enums import GameMode, PacketType, State
from .models import KillInfo, Player, SessionView


def _norm_name(n: str | None) -> str:
    if not n:
        return ""
    s = _strip_color_tags(str(n)).strip()
    if s.lower().startswith("dc:"):
        s = s[3:].strip()
    return s
def _names_equal(a, b) -> bool:
    na = _norm_name(a); nb = _norm_name(b)
    return bool(na and nb and na == nb)

def _resolve_actor(room, nr: int | None) -> str | None:
    """Resolve actorNr -> player username via actor_map, room.players key, or player.actor_nr.
    Actor 0 = room-level properties (curScn/stG/pID), never a player — returns "room"."""
    if nr is None:
        return None
    try:
        if int(nr) == 0:
            return "room"
    except Exception:
        pass
    try:
        if nr in room.actor_map:
            return str(room.actor_map[nr])
        if nr in room.players:
            return str(room.players[nr].username)
        for p in room.players.values():
            if p.actor_nr == nr:
                return str(p.username)
    except Exception:
        pass
    return None


def _find_player(room, actor=None, target=None, name=None):
    """Resolve (key, player): TargetActorNr subject first, then sender ActorNr, then name.
    _pt/skin packets address the subject ONLY via TargetActorNr (actor is the room
    master), so target must win. Actor 0 = room-level, never a player — skipped."""
    for nr in (target, actor):
        if nr is None:
            continue
        try:
            nr = int(nr)
        except Exception:
            continue
        if nr == 0:
            continue
        try:
            if nr in room.players:
                return nr, room.players[nr]
        except Exception:
            pass
        nm = None
        try:
            nm = room.actor_map.get(nr)
        except Exception:
            pass
        if nm:
            for k, p in room.players.items():
                if _names_equal(p.username, nm):
                    return k, p
        for k, p in room.players.items():
            if p.actor_nr == nr:
                return k, p
    if name:
        for k, p in room.players.items():
            if _names_equal(p.username, name):
                return k, p
    return None, None

def _is_valid_pid(v: str | None) -> str | None:
    """Validate 5-char private join code (e.g. PBS05) — rejects ghost 'n'."""
    if not v:
        return None
    s = str(v).strip()
    if len(s) != 5:
        return None
    import re
    if not re.fullmatch(r"[A-Z0-9]{5}", s, re.IGNORECASE):
        return None
    return s.upper()


@dataclass
class EnginePacket:
    """Engine-native packet — no photon_api dependency. Built from gpb directly."""

    raw_hex: str = ""
    raw: dict[str, Any] | None = None
    packet_type: PacketType = PacketType.UNKNOWN
    game_mode: GameMode | None = None
    killer: str | None = None
    victim: str | None = None
    player_name: str | None = None
    zone_time: int | None = None
    zone_phase: str | None = None
    cur_scn: str | None = None
    party: str | None = None
    is_kill: bool = False
    is_knock: bool = False
    is_join: bool = False
    is_leave: bool = False
    is_eliminated: bool = False
    is_alive: bool = False
    sentence: str | None = None
    room_id: str | None = None
    room_code: str | None = None
    short_code: str | None = None
    match_version: int | None = None
    room_gm: str | None = None
    actor_nr: int | None = None       # sender actor (params.ActorNr) — original value
    target_actor_nr: int | None = None  # subject actor (params.TargetActorNr) — original value

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "packet": self.packet_type.value,
            "packet_type": self.packet_type.value,
            "game_mode": self.game_mode.value if self.game_mode else None,
            "killer": self.killer,
            "victim": self.victim,
            "player_name": self.player_name,
            "zone_time": self.zone_time,
            "zone_phase": self.zone_phase,
            "cur_scn": self.cur_scn,
            "party": self.party,
            "is_kill": self.is_kill,
            "is_knock": self.is_knock,
            "is_join": self.is_join,
            "is_leave": self.is_leave,
            "is_alive": self.is_alive,
            "is_eliminated": self.is_eliminated,
            "sentence": self.sentence,
            "raw_hex": self.raw_hex,
            "room_id": self.room_id,
            "room_code": self.room_code,
            "short_code": self.short_code,
            "match_version": self.match_version,
            "room_gm": self.room_gm,
            "actor_nr": self.actor_nr,
            "target_actor_nr": self.target_actor_nr,
        }
        if self.is_kill or self.is_knock:
            d["kill"] = {
                "instigator": self.killer,
                "instigated": self.victim,
                "event_type": "killed" if self.is_kill else "knocked",
            }
        return {k: v for k, v in d.items() if v is not None or k.startswith("is_")}


class Engine:
    """
    In-process, per-session engine for Photon packets.
    
    Holds many SessionViews, each keyed by session_id.
    Feed it hex, it mutates that session's Player/Room/Party/Zone live.
    No DB writes, no win math — you decide.
    """

    def __init__(self):
        self._sessions: dict[str, SessionView] = {}
        self._pending_pid: dict[str, str] = {}  # session_id -> pID from JoinRandomRoom without RoomName (private join)
        self._pending_gm: dict[str, GameMode] = {}  # session_id -> gm from JoinRandomRoom without RoomName

    def feed(self, session_id: str, hex_string: str) -> EnginePacket:
        """Feed one hex packet for a session — decrypts, updates SessionView/RoomView, returns EnginePacket.

        Rooms are scoped: CreateRoom/JoinRoom/JoinRandomRoom with RoomName creates a new RoomView;
        all subsequent JOIN/LEAVE/ALIVE/KILL/zone/state events are scoped to that room.
        Query: session.rooms[room_id] or session.current_room.
        """

        # Get or create session view for this id
        view = self._sessions.get(session_id)
        if view is None:
            view = SessionView(session_id=session_id)
            self._sessions[session_id] = view
            log_engine("join", session_id[:8], action="new_session")

        # Parse
        gpb = None
        try:
            gpb = parse_gpbinary(hex_string)
        except Exception as e:
            log_engine("parse_fail", session_id[:8], error=str(e))
            gpb = None

        packet = self._gpb_to_abstract(hex_string, gpb)

        # Visibility: log packets that classified as UNKNOWN (nothing should be invisible)
        if packet.packet_type == PacketType.UNKNOWN and gpb:
            unk_props = list((gpb.get("properties") or {}).keys())[:8]
            unk_pp = list((gpb.get("playerProps") or {}).keys())[:8]
            log_engine("debug", session_id[:8], msg=f"unknown pkt ev={gpb.get('evName')}/{gpb.get('opName')} evCode={gpb.get('evCode')} op={gpb.get('op')} props={unk_props} playerProps={unk_pp} hex={hex_string[:32]}..")

        # Ensure we have a RoomView to scope to
        # If no current room, create a default lobby room
        from .models import RoomView
        from datetime import datetime, timezone

        def _ensure_room(room_id: str | None) -> "RoomView":
            if room_id:
                if room_id not in view.rooms:
                    view.rooms[room_id] = RoomView(room_id=room_id, code=room_id, created_at=datetime.now(timezone.utc).isoformat())
                    view.current_room_id = room_id
                    log_engine("room", session_id[:8], action="created", room=room_id[:8])
                elif view.current_room_id != room_id:
                    view.current_room_id = room_id
                    log_engine("room", session_id[:8], action="switched", room=room_id[:8])
                return view.rooms[room_id]
            # no room_id — strict: route to current MATCH room if exists, else current, else lobby
            # this stops killfeeds (no RoomName) from landing in a throwaway lobby room
            # when user is in-game — fixes gm:null / room:lobby bug
            cur = view.rooms.get(view.current_room_id) if view.current_room_id else None
            if cur and cur.state == State.MATCH:
                return cur
            # prefer any MATCH room with a game_mode (in case current_room_id is stale lobby)
            for rv in view.rooms.values():
                if rv.state == State.MATCH and rv.game_mode:
                    view.current_room_id = rv.room_id
                    return rv
            if view.current_room_id and view.current_room_id in view.rooms:
                return view.rooms[view.current_room_id]
            if "lobby" not in view.rooms:
                view.rooms["lobby"] = RoomView(room_id="lobby", code=None, created_at=datetime.now(timezone.utc).isoformat())
                view.current_room_id = "lobby"
            return view.rooms["lobby"]

        # Extract RoomName early to create/switch room on Create/Join
        room_name = None
        if gpb:
            try:
                if gpb.get("params", {}).get("RoomName"):
                    val = gpb["params"]["RoomName"]
                    room_name = str(val["value"] if isinstance(val, dict) and "value" in val else val)
                elif gpb.get("room") and isinstance(gpb["room"], dict) and gpb["room"].get("code"):
                    room_name = gpb["room"]["code"]
                elif gpb.get("room") and hasattr(gpb["room"], "code"):
                    room_name = gpb["room"].code
            except Exception:
                room_name = None

        is_first_join = gpb and gpb.get("opName") in ("CreateRoom", "JoinRoom", "JoinRandomRoom")
        # Prefer packet's room_id (e.g., 5c02c34a...) for curScn/zone packets which have no RoomName — fixes scene detection
        pkt_room_hint = getattr(packet, "room_id", None) or getattr(packet, "room_code", None)
        if not pkt_room_hint and packet.raw and isinstance(packet.raw, dict):
            try:
                _rm = packet.raw.get("room")
                if isinstance(_rm, dict) and _rm.get("code"):
                    pkt_room_hint = _rm.get("code")
            except: pass
        if pkt_room_hint and str(pkt_room_hint) != "lobby":
            room = _ensure_room(str(pkt_room_hint))
        elif is_first_join and room_name:
            room = _ensure_room(room_name)
        else:
            room = _ensure_room(None)

        # Playtime tick — account elapsed since last packet (session + per-room)
        import time as _t
        now = _t.time()
        if view.playtime.last_state_at is None:
            view.playtime.last_state_at = now
            view.playtime.last_state = room.state.value if room else State.LOBBY.value
        else:
            elapsed = int(now - view.playtime.last_state_at)
            if elapsed > 0 and elapsed < 300:  # cap gaps (e.g. server restart)
                view.playtime.total_seconds += elapsed
                # per_gm if in match, else lobby
                if view.playtime.last_state == State.MATCH.value and room and room.game_mode:
                    gm_key = room.game_mode.value
                    view.playtime.per_gm[gm_key] = view.playtime.per_gm.get(gm_key, 0) + elapsed
                else:
                    view.playtime.lobby_seconds += elapsed
            view.playtime.last_state_at = now
        # Per-room playtime tick
        if room.match_playtime.last_state_at is None:
            room.match_playtime.last_state_at = now
            room.match_playtime.last_state = room.state.value
        else:
            elapsed2 = int(now - room.match_playtime.last_state_at)
            if elapsed2 > 0 and elapsed2 < 300:
                room.match_playtime.total_seconds += elapsed2
                if room.match_playtime.last_state == State.MATCH.value and room.game_mode:
                    gm_key2 = room.game_mode.value
                    room.match_playtime.per_gm[gm_key2] = room.match_playtime.per_gm.get(gm_key2, 0) + elapsed2
                else:
                    room.match_playtime.lobby_seconds += elapsed2
            room.match_playtime.last_state_at = now
            view.playtime.last_state_at = now

        # Extract helper values
        actor_nr = self._extract_actor_nr(gpb)
        cur_scn = None
        if gpb and gpb.get("properties", {}).get("curScn"):
            cur_scn = str(gpb["properties"]["curScn"])
        elif packet.cur_scn:
            cur_scn = packet.cur_scn

        # Extract TargetActorNr (may be wrapped {"value": N})
        target_actor_nr = None
        try:
            _targ = (gpb.get("params", {}) or {}).get("TargetActorNr") if gpb else None
            while isinstance(_targ, dict) and "value" in _targ:
                _targ = _targ["value"]
            if _targ is not None and str(_targ).strip().lstrip("-").isdigit():
                target_actor_nr = int(_targ)
        except Exception:
            target_actor_nr = None
        # Resolve actor numbers to player names (None when absent) for all logs below
        actor_name = _resolve_actor(room, actor_nr)
        target_name = _resolve_actor(room, target_actor_nr)
        # Stamp ORIGINAL actor numbers on the packet for the processor (the locals
        # may be reassigned below, e.g. alv TargetActorNr routing — processor re-resolves)
        packet.actor_nr = actor_nr
        packet.target_actor_nr = target_actor_nr
        # Log actorNr + TargetActorNr for every packet with gpb (debug actor routing)
        # Target shown only when present (Join has actor only — no target to misuse)
        if gpb and (actor_nr is not None or target_actor_nr is not None):
            tgt_part = f" targetActorNr={target_actor_nr} ({target_name})" if target_actor_nr is not None else ""
            log_engine("actor", session_id[:8], msg=f"actorNr={actor_nr} ({actor_name}){tgt_part} ev={gpb.get('evName')}/{gpb.get('opName')} type={packet.packet_type.value} room={room.room_id[:8]}", room=room.room_id[:8], actor=actor_nr, actor_name=actor_name, target=target_actor_nr, target_name=target_name)

        # Log PropertiesChanged packets (evCode 253) — key events for debugging
        if gpb and gpb.get("evName") == "PropertiesChanged":
            props = gpb.get("properties", {})
            actor_str = f" actor={actor_nr} ({actor_name}) target={target_actor_nr} ({target_name})"
            # Log important properties in detail (alv, curScn, stG, 255)
            important = {}
            for k in ("alv", "curScn", "stG", "255", "skid", "_pt", "pt"):
                if k in props:
                    important[k] = props[k]
            if important:
                log_engine("props", session_id[:8], msg=f"PropertiesChanged{actor_str} props={important} room={room.room_id[:8]}", room=room.room_id[:8])
            else:
                prop_keys = list(props.keys())[:6]
                log_engine("props", session_id[:8], msg=f"PropertiesChanged{actor_str} keys={prop_keys} room={room.room_id[:8]}", room=room.room_id[:8])

        is_auth = gpb and gpb.get("opName") in ("Authenticate", "AuthenticateOnce")
        # usernameChangeCount reset is AUTH only; scene MainLobby will reset via FSM on_enter
        if is_auth:
            view.usernameChangeCount = 0

        # Helper to extract other player's props (skid/pid/pfid/_pt) from gpb
        def _other_props():
            pp = gpb.get("playerProps") if gpb else None
            props = gpb.get("properties") if gpb else {}
            if not pp and not props:
                return None, None, None, None
            skid = (pp.get("skid") if pp else None) or props.get("skid")
            pid = (pp.get("pid") if pp else None) or props.get("pid")
            pfid = (pp.get("pfid") if pp else None) or props.get("pfid")
            pt = (pp.get("_pt") if pp else None) or (pp.get("pt") if pp else None) or props.get("_pt") or props.get("pt")
            if pt is not None:
                pt = str(pt).strip()
                if not pt:
                    pt = None
            return skid, pid, pfid, pt

        def _rebuild_teams(_room):
            # Group players by pt for duo modes (wwzw/BF2V2/DF duo) — _pt ties to actorNr/TargetActorNr
            try:
                grouped: dict[str, list[str]] = {}
                for pl in _room.players.values():
                    if pl.pt:
                        grouped.setdefault(str(pl.pt), []).append(pl.username)
                # also check actor_map? _pt already on player
                _room.teams.clear()
                for pt_id, members in grouped.items():
                    if len(members) >= 1:
                        is_self = bool(view.me and view.me.username in members) or bool(_room.me and _room.me.username in members)
                        # also check my pt via view.me.pt
                        if view.me and view.me.pt and str(view.me.pt) == str(pt_id):
                            is_self = True
                        _room.teams[pt_id] = Team(team_id=str(pt_id), members=list(members), is_self_team=is_self)
                if grouped:
                    log_engine("team", session_id[:8], room=_room.room_id[:8], teams={k: len(v) for k,v in grouped.items()})
            except Exception:
                pass

        new_name = None
        new_gid = None
        if gpb and gpb.get("playerProps"):
            new_name = gpb["playerProps"].get("255")
            new_gid = gpb["playerProps"].get("pfid")

        # First join for this session — set up me (session-scoped)
        if is_first_join and new_name and (view.usernameChangeCount or 0) == 0:
            if view.me and str(new_name) != str(view.me.username):
                log_engine("name_change", session_id[:8], old=view.me.username, new=new_name)
            if view.me is None:
                view.me = Player(
                    actor_nr=int(actor_nr) if actor_nr is not None else -1,
                    username=str(new_name),
                    game_id=str(new_gid) if new_gid else None,
                    skin=str(gpb["playerProps"].get("skid")) if gpb["playerProps"].get("skid") else None,
                    pickaxe=str(gpb["playerProps"].get("pid")) if gpb["playerProps"].get("pid") else None,
                    is_alive=True,
                    is_self=True,
                    pt=str(gpb["playerProps"].get("_pt") or gpb["playerProps"].get("pt") or "") or None,
                )
            else:
                view.me.username = str(new_name)
                if new_gid:
                    view.me.game_id = str(new_gid)
                if actor_nr is not None:
                    view.me.actor_nr = int(actor_nr)
                _pt_first = gpb["playerProps"].get("_pt") or gpb["playerProps"].get("pt")
                if _pt_first:
                    view.me.pt = str(_pt_first)
            # Seed players_count — only for JoinRandomRoom (public), not CreateRoom (private)
            is_random = gpb.get("opName") == "JoinRandomRoom"
            max_p = None
            if is_random and gpb.get("params", {}).get("GameProperties"):
                try:
                    gp = gpb["params"]["GameProperties"]
                    if isinstance(gp, dict) and "value" in gp:
                        from ..gpbinary import _unwrap
                        gp_val = _unwrap(gp)
                        max_p = gp_val.get("255")
                    else:
                        max_p = gp.get("255")
                except Exception:
                    max_p = None
            if max_p is not None:
                try:
                    room.max_players = int(max_p)
                    room.players_count = int(max_p)
                except Exception:
                    pass
            else:
                if room.players_count == 0:
                    room.players_count = 1
            view.usernameChangeCount = 1
            # Add me to current room (per-room me, not session-wide)
            me_key = view.me.actor_nr if view.me.actor_nr != -1 else hash(view.me.username) % 100000
            room.players[me_key] = view.me
            room.me = view.me  # per-room me
            log_engine("join", session_id[:8], name=new_name, skin=view.me.skin, gm=packet.game_mode.value if packet.game_mode else "?", max=room.max_players, count=room.players_count, room=room.room_id[:8])
            if packet.game_mode and not room.game_mode:
                room.game_mode = packet.game_mode
            # Party
            party_val = gpb.get("Party") if gpb.get("Party") is not None else gpb.get("properties", {}).get("Party")
            if party_val is not None:
                room.party.is_party = bool(party_val)
                if room_name:
                    room.party.code = room_name
            # Debug: log gpb for first join to trace other player joins later
            log_engine("debug", session_id[:8], msg=f"first_join op={gpb.get('opName')} room={room_name} playerProps={list(gpb.get('playerProps', {}).keys())}", room=room.room_id[:8])

        # pID — 5-char code players type to join private room (validated len==5) — handles any packet, not just first join
        # e.g. JoinRandomRoom with pID DBSXH but no RoomName → pending, next JoinRoom with RoomName 004784f4 → assign
        try:
            pID_val2 = None
            if gpb.get("params", {}).get("GameProperties"):
                gp = gpb["params"]["GameProperties"]
                from ..gpbinary import _unwrap as _uw2b
                gp_val = _uw2b(gp) if isinstance(gp, dict) and "value" in gp else gp
                if isinstance(gp_val, dict) and "pID" in gp_val:
                    pID_val2 = gp_val["pID"]
            if not pID_val2 and gpb.get("properties", {}).get("pID"):
                pID_val2 = gpb["properties"]["pID"]
            if not pID_val2 and gpb.get("playerProps", {}).get("pID"):
                pID_val2 = gpb["playerProps"]["pID"]
            valid2 = _is_valid_pid(pID_val2)
            if valid2 and not room.short_code:
                room.short_code = valid2
                log_engine("room", session_id[:8], action="pID", code=room.short_code, room=room.room_id[:8])
            elif pID_val2 and not valid2:
                log_engine("debug", session_id[:8], msg=f"pID ghost ignored {pID_val2!r}", room=room.room_id[:8])
            if not room_name and valid2:
                self._pending_pid[session_id] = valid2
                log_engine("debug", session_id[:8], msg=f"pending pID {valid2} for next room", room=room.room_id[:8])
            elif room_name and not room.short_code and session_id in self._pending_pid:
                pending = self._pending_pid.pop(session_id, None)
                if pending:
                    room.short_code = pending
                    log_engine("room", session_id[:8], action="pID", code=room.short_code, room=room.room_id[:8], note="from pending")
            # Pending GM: JoinRandomRoom with gm but no RoomName → next room gets it (wwzw vs BF2V2 mix)
            if packet.game_mode and not room_name:
                self._pending_gm[session_id] = packet.game_mode
                log_engine("debug", session_id[:8], msg=f"pending gm {packet.game_mode.value} for next room", room=room.room_id[:8])
            elif room_name and not room.game_mode and session_id in self._pending_gm:
                pending_gm = self._pending_gm.pop(session_id, None)
                if pending_gm:
                    room.game_mode = pending_gm
                    log_engine("room", session_id[:8], action="gm", gm=pending_gm.value, room=room.room_id[:8], note="from pending")
        except Exception:
            pass

        # gv — game variant (solo/duo/...) from Create/Join GameProperties.
        # Duo modes (duo DF, duo zw_lg) have gv != "solo" — team logic flows via _pt teams.
        # Unknown modes (v-arena, red-vs-blue, boxfight, realistic-1v1): play each once and
        # read the discover line below (gm/gv/max/curScn) to wire up their IDs.
        try:
            gv_val = None
            gp_keys: list = []
            if gpb.get("params", {}).get("GameProperties"):
                gp = gpb["params"]["GameProperties"]
                from ..gpbinary import _unwrap as _uw_gv
                gp_val = _uw_gv(gp) if isinstance(gp, dict) and "value" in gp else gp
                if isinstance(gp_val, dict):
                    gp_keys = sorted([str(k) for k in gp_val.keys()])[:12]
                    for _gk, _gv in gp_val.items():
                        if str(_gk).lower() == "gv" and _gv is not None:
                            gv_val = str(_gv)
                            break
            if not gv_val and gpb.get("properties", {}).get("gv") is not None:
                gv_val = str(gpb["properties"]["gv"])
            if gv_val and not room.gv:
                room.gv = gv_val
                log_engine("room", session_id[:8], action="gv", gv=room.gv, duo=room.is_duo, room=room.room_id[:8])
            if is_first_join and gpb.get("opName") in ("CreateRoom", "JoinRoom", "JoinRandomRoom"):
                log_engine("room", session_id[:8], action="discover", op=gpb.get("opName"),
                           gm=gpb.get("gm"), gv=room.gv or gv_val, max=room.max_players,
                           curScn=cur_scn, room_name=(room_name or "")[:12], room=room.room_id[:8],
                           gp_keys=gp_keys or None)
        except Exception:
            pass

        # Build actor_map from ActorList + PlayerProperties (for alv without 255) — contract: RoomView.actor_map
        try:
            # ActorList may be in params (unwrapped) or raw resp
            actor_list = None
            if gpb.get("params", {}).get("ActorList") is not None:
                al = gpb["params"]["ActorList"]
                # al may be {"type":"Array","value":[...]} or already unwrapped list
                if isinstance(al, dict) and "value" in al:
                    from ..gpbinary import _unwrap as _uw_al
                    al = _uw_al(al)
                if isinstance(al, list):
                    actor_list = al
            # Also try raw resp for OperationResponse
            if not actor_list and gpb.get("raw", {}).get("resp", {}).get("params"):
                for p in gpb["raw"]["resp"]["params"]:
                    if p.get("keyName") == "ActorList":
                        from ..gpbinary import _unwrap as _uw_al2
                        actor_list = _uw_al2(p["value"])
                        break
            if actor_list:
                # PlayerProperties dict keys may be ints or strings (after _unwrap)
                pp = gpb.get("playerProps") or {}
                # Also try GameProperties? no
                # If raw, need to get from raw resp
                if not pp and gpb.get("raw", {}).get("resp"):
                    for p in gpb["raw"]["resp"]["params"]:
                        if p.get("keyName") == "PlayerProperties":
                            from ..gpbinary import _unwrap as _uw_pp
                            pp = _uw_pp(p["value"])
                            break
                # Flat single-player props (Join 255): {'255': name, ...} with scalar
                # values — this is the JOINER's own props, not an actorNr->props map.
                # Map this packet's actor directly instead of misreading it as a map.
                if isinstance(pp, dict) and isinstance(pp.get("255"), str):
                    if actor_nr is not None:
                        try:
                            room.actor_map[int(actor_nr)] = str(pp["255"])
                            log_engine("debug", session_id[:8], msg=f"actor_map single actor={int(actor_nr)} name={pp['255']} map={sorted(list(room.actor_map.items()))[:8]}", room=room.room_id[:8])
                        except Exception:
                            pass
                    pp_norm: dict | None = None  # flat props: skip the actorNr-map loop below
                else:
                    # Normalize pp keys to str -> props dict for int/str key mismatch
                    pp_norm = {}
                    if isinstance(pp, dict):
                        for k, v in pp.items():
                            try:
                                pp_norm[str(int(k) if isinstance(k, str) and k.strip().lstrip("-").isdigit() else k)] = v
                            except Exception:
                                pp_norm[str(k)] = v
                added: list = []
                missing: list = []
                if pp_norm is None:
                    actor_list = []  # flat single-player props handled above — nothing to map
                for a in actor_list:
                    try:
                        # actor_list items may be {"value":2} or int
                        av = a["value"] if isinstance(a, dict) and "value" in a else a
                        actor_nr = int(av)
                        # Try to get username for this actor (both str and int keys)
                        uname = None
                        entry = pp_norm.get(str(actor_nr))
                        if isinstance(entry, dict):
                            uname = entry.get("255")
                            if isinstance(uname, dict) and "value" in uname:
                                uname = uname["value"]
                        if uname:
                            uname = str(uname)
                            room.actor_map[actor_nr] = uname
                            added.append((actor_nr, uname))
                            # Also ensure player exists in room.players if not already
                            if actor_nr not in room.players:
                                # Create placeholder, will be filled on Join
                                pass
                        else:
                            missing.append(actor_nr)
                    except Exception:
                        continue
                if actor_list:
                    try:
                        al_ints = [int(a["value"] if isinstance(a, dict) and "value" in a else a) for a in actor_list[:8]]
                    except Exception:
                        al_ints = actor_list[:8]
                    log_engine("debug", session_id[:8], msg=f"actor_map built actors={al_ints} pp_keys={sorted(list(pp_norm.keys()))[:8]} added={added[:6]} missing={missing[:6]} map={sorted(list(room.actor_map.items()))[:8]}", room=room.room_id[:8])
        except Exception:
            pass
        # Also handle single Join event to fill actor_map incrementally
        if gpb and gpb.get("playerProps", {}).get("255") and actor_nr is not None:
            try:
                room.actor_map[int(actor_nr)] = str(gpb["playerProps"]["255"])
            except Exception:
                pass

        # GM is locked by FSM on MATCH enter only — do not set here (prevents flicker)
        # pending_gm plumbing still applies below for JoinRandomRoom without RoomName

        # ---- Strict FSM: state/gm only via enter/exit ----
        # LOBBY enter: authenticate / leave(self) ; LOBBY exit: scene(curScn!=MainLobby)
        # MATCH enter: scene(curScn!=MainLobby)   ; MATCH exit: authenticate / leave(self)
        # Everything else (killfeed, stG, zone, skin) is ignored for state.
        try:
            from .fsm import RoomFSM
            did, _old, _new, _via = RoomFSM.transition(room, view, packet, gpb, cur_scn, session_id)
            # keep room.cur_scn always up to date on scene packets (even when no transition)
            if cur_scn is not None:
                room.cur_scn = cur_scn
                if cur_scn == "MainLobby":
                    view.usernameChangeCount = 0
        except Exception as e:
            log_engine("debug", session_id[:8], msg=f"fsm error {e}", room=room.room_id[:8])

        # Zone (room-scoped) — hard 4:59 (299) for zw_lg/wwzw, stG is match marker not zone
        def _valid_zone_time(v):
            try:
                iv = int(float(v))
                return iv if 0 <= iv <= 600 else None
            except Exception:
                return None
        # stG handling — match start/end marker (1=start, 3=end), NOT zone timer.
        # Timer-flavored stG (e.g. 30s countdowns) also passes here: only a 3 arriving
        # from below (None/0/1/2) counts as phase end. A 3 ticking down from above (4->3)
        # is a timer and must not bump versions or arm wins. Must run before zone handling.
        _STG_PHASE_MODES = (GameMode.ZW_LG, GameMode.WWZW, GameMode.BF2V2, GameMode.BF, GameMode.VA, GameMode.RVB, GameMode.R1V1)
        is_stg_packet = gpb and gpb.get("properties") and "stG" in gpb["properties"]
        if is_stg_packet:
            try:
                stg = int(float(gpb["properties"]["stG"]))
                # Log every stG change (match progress 1→2→3) like KILL lines
                if stg != room.last_stg:
                    log_engine("stg", session_id[:8], stg=stg, gm=room.game_mode.value if room.game_mode else None, room=room.room_id[:8])
                # stG=3: match end marker — Processor handles win/defeat calculation
                if stg == 3 and room.last_stg != 3 and room.game_mode and room.game_mode in _STG_PHASE_MODES:
                    if room.last_stg is None or room.last_stg <= 2:
                        room.stg_is_phase_end = True  # arm win path (repeats/timers leave it)
                        me_name = (view.me.username if view.me else None) or (room.me.username if room.me else None)
                        if me_name and room.state == State.MATCH:
                            # bump version for next match — Processor checks stG==3 to calculate win/defeat
                            room.match_version += 1
                            room.zone_match_active = False
                            if len(room.recent_kills) > 20:
                                room.recent_kills = room.recent_kills[-10:]
                            room.recent_knocks = []
                            log_engine("match", session_id[:8], action="new_version", version=room.match_version, room=room.room_id[:8], via="stG 3")
                    # else: timer ticking down through 3 — leave flag untouched
                elif stg != 3:
                    room.stg_is_phase_end = False  # new phase/round in progress — disarm stale end
                room.last_stg = stg
            except Exception:
                pass
        # Zone timer — only from bz_* (not stG) for zw_lg/wwzw, hard 4:59
        # So packet.zone_time that came from stG should be ignored for zw_lg
        is_zone_packet = gpb and gpb.get("properties") and any(k in gpb["properties"] for k in ("bz_phase_start", "bz_zone", "bz_phase", "bz_pattern", "bz_enabled"))
        # Skip stG as zone for zw_lg/wwzw
        if is_stg_packet and room.game_mode and room.game_mode.value in ("zw_lg", "wwzw", "zw"):
            is_zone_packet = False
            # Don't set zone from stG
            pass
        elif is_zone_packet or (packet.zone_time is not None and not is_stg_packet):
            if packet.zone_time is not None:
                vt = _valid_zone_time(packet.zone_time)
                if vt == 300 and room.game_mode and room.game_mode.value in ("zw_lg", "wwzw"):
                    vt = 299
                if vt is not None:
                    room.zone.set_time(vt, packet.zone_phase)
                    if packet.zone_phase and room.zone.phase != packet.zone_phase:
                        log_engine("zone", session_id[:8], old=room.zone.phase, new=packet.zone_phase, mmss=room.zone.remaining_mmss(), room=room.room_id[:8])
                elif packet.zone_time is not None:
                    log_engine("debug", session_id[:8], msg=f"zone_time ignored {packet.zone_time!r} >600", room=room.room_id[:8])
            elif packet.zone_phase is not None:
                old_phase = room.zone.phase
                room.zone.set_time(room.zone.time, packet.zone_phase)
                if old_phase != packet.zone_phase:
                    log_engine("zone", session_id[:8], old=old_phase, new=packet.zone_phase, mmss=room.zone.remaining_mmss(), room=room.room_id[:8])
        if gpb and gpb.get("properties"):
            props = gpb["properties"]
            # Only bz_* for zone, stG already handled above
            if room.zone.time is None and "bz_phase_start" in props:
                cand = props.get("bz_phase_start")
                vt = _valid_zone_time(cand)
                if vt is not None:
                    if vt == 300 and room.game_mode and room.game_mode.value in ("zw_lg", "wwzw"):
                        vt = 299
                    room.zone.set_time(vt, props.get("bz_zone") or props.get("bz_phase"))
            if room.zone.phase is None and ("bz_zone" in props or "bz_phase" in props):
                room.zone.set_time(room.zone.time, str(props.get("bz_zone") or props.get("bz_phase")))
            if "bz_pattern" in props:
                room.zone.pattern = str(props["bz_pattern"])
            # pID update for existing room (private join code) — validated len==5
            if "pID" in props and not room.short_code:
                valid = _is_valid_pid(props["pID"])
                if valid:
                    room.short_code = valid
                    log_engine("room", session_id[:8], action="pID", code=room.short_code, room=room.room_id[:8])
                elif props["pID"]:
                    log_engine("debug", session_id[:8], msg=f"pID ghost ignored {props['pID']!r}", room=room.room_id[:8])
        # pID from GameProperties (CreateRoom with pID PBS05) — validated
        if not room.short_code and gpb and gpb.get("params", {}).get("GameProperties"):
            try:
                gp = gpb["params"]["GameProperties"]
                from ..gpbinary import _unwrap as _uw3
                gp_val = _uw3(gp) if isinstance(gp, dict) and "value" in gp else gp
                if isinstance(gp_val, dict) and "pID" in gp_val and gp_val["pID"]:
                    valid = _is_valid_pid(gp_val["pID"])
                    if valid:
                        room.short_code = valid
                        log_engine("room", session_id[:8], action="pID", code=room.short_code, room=room.room_id[:8])
                    elif gp_val["pID"]:
                        log_engine("debug", session_id[:8], msg=f"pID ghost ignored {gp_val['pID']!r}", room=room.room_id[:8])
            except Exception:
                pass

        # zone bookend wins disabled per user — only stG==3 for wins/defeats
        if room.state == State.LOBBY:
            room.zone_match_active = False
            room.last_zone_hex = None

                # ---- JOIN (room-scoped, fill other players skin/pid/pfid) ---
        # Handle both Event Join (255) and Operation JoinRoom from other players joining existing room
        join_name = packet.player_name
        # Fallback: if packet has no name but gpb has playerProps 255, use that (covers JoinRoom from joiner)
        if not join_name and gpb and gpb.get("playerProps", {}).get("255"):
            join_name = str(gpb["playerProps"]["255"])
        is_join_packet = packet.packet_type in (PacketType.JOIN, PacketType.JOIN_ROOM, PacketType.CREATE_ROOM)
        if is_join_packet and join_name:
            # If this is not the self's first join (already handled), treat as other player joining existing room (normalized for dc: prefix)
            is_self_join = _names_equal(join_name, view.me.username if view.me else None) and is_first_join
            if not is_self_join:
                p_actor = actor_nr
                existing_key = None
                for k, p in room.players.items():
                    if _names_equal(p.username, join_name):
                        existing_key = k
                        break
                if existing_key is not None:
                    was_dead = not room.players[existing_key].is_alive
                    room.players[existing_key].is_alive = True
                    # re-join of a dead player (e.g. multi-round reset) = new life for self
                    if was_dead and (room.players[existing_key].is_self or (view.me and _names_equal(join_name, view.me.username))):
                        room.had_self_death = False
                        room.had_self_defeat = False
                        log_engine("debug", session_id[:8], msg=f"rejoin reset flags for {join_name}", room=room.room_id[:8])
                    skid, pid, pfid, pt = _other_props()
                    if skid: room.players[existing_key].skin = skid
                    if pid: room.players[existing_key].pickaxe = pid
                    if pfid: room.players[existing_key].game_id = pfid
                    if pt: room.players[existing_key].pt = pt
                    elif existing_key in room.pending_pt:
                        pt = room.pending_pt.pop(existing_key)
                        room.players[existing_key].pt = pt
                        log_engine("team_pt", session_id[:8], team=pt, target=existing_key, player=join_name, via="pending apply on existing", room=room.room_id[:8])
                    if pt: _rebuild_teams(room)
                    log_engine("join", session_id[:8], name=join_name, status="existing", room=room.room_id[:8], pt=pt)
                else:
                    key = int(p_actor) if p_actor is not None else hash(join_name) % 100000
                    # ensure unique
                    while key in room.players:
                        key = (key + 1) % 100000
                    skid, pid, pfid, pt = _other_props()
                    # apply pending _pt from TargetActorNr (private duos) if no pt in join props
                    if not pt and key in room.pending_pt:
                        pt = room.pending_pt.pop(key)
                        log_engine("team_pt", session_id[:8], team=pt, target=key, player=join_name, via="pending apply on join", room=room.room_id[:8])
                    room.players[key] = Player(
                        actor_nr=key if p_actor is not None else -1,
                        username=join_name,
                        game_id=str(pfid) if pfid else None,
                        skin=str(skid) if skid else None,
                        pickaxe=str(pid) if pid else None,
                        is_alive=True,
                        is_self=False,
                        pt=pt,
                    )
                    if pt: _rebuild_teams(room)
                    # Increment count for private rooms (CreateRoom) — for random rooms count is seeded from max, don't increment
                    if room.max_players is None or room.players_count < room.max_players or gpb.get("opName") == "CreateRoom":
                        room.players_count += 1
                    log_engine("join", session_id[:8], name=join_name, status="new", count=room.players_count, room=room.room_id[:8])
            # Debug if we missed player_name extraction
            if not packet.player_name and join_name:
                log_engine("debug", session_id[:8], msg=f"JOIN fallback used gpb playerProps {join_name} type={packet.packet_type.value}", room=room.room_id[:8])
        elif packet.packet_type == PacketType.JOIN and not packet.player_name:
            # Debug: JOIN packet without name — log gpb keys to trace
            gpb_keys = list(gpb.keys()) if gpb else []
            props_keys = list(gpb.get("properties", {}).keys()) if gpb and gpb.get("properties") else []
            pp_keys = list(gpb.get("playerProps", {}).keys()) if gpb and gpb.get("playerProps") else []
            log_engine("debug", session_id[:8], msg=f"JOIN no name gpb={gpb.get('evName')}/{gpb.get('opName')} playerProps={pp_keys} props={props_keys} gpb_keys={gpb_keys}", room=room.room_id[:8])

        # ---- LEAVE (room-scoped, log explicitly) ----
        if packet.packet_type == PacketType.LEAVE and packet.player_name:
            to_del = None
            for k, p in room.players.items():
                if _names_equal(p.username, packet.player_name) or (actor_nr is not None and k == int(actor_nr)):
                    to_del = k
                    break
            if to_del is not None:
                room.players.pop(to_del, None)
                if room.players_count > 0:
                    room.players_count -= 1
                log_engine("leave", session_id[:8], name=packet.player_name, count=room.players_count, room=room.room_id[:8])
                room.recent_kills.append(KillInfo(instigator=packet.player_name, instigated="", event_type="left"))
            else:
                # still record leave for processor
                room.recent_kills.append(KillInfo(instigator=packet.player_name, instigated="", event_type="left"))
                log_engine("leave", session_id[:8], name=packet.player_name, count=room.players_count, room=room.room_id[:8], status="not_found")

        # ---- ALIVE / ELIMINATED (room-scoped, also fill skin) ----
        # Contract: RoomView.actor_map[actorNr] resolves alv without 255 (bare alv packets)
        if packet.packet_type in (PacketType.ALIVE, PacketType.ELIMINATED) and (packet.player_name or actor_nr is not None or (gpb and gpb.get("params", {}).get("TargetActorNr") is not None)):
            # Resolve name via actor_map if packet has no 255
            alv_name = packet.player_name
            if not alv_name and actor_nr is not None:
                alv_name = room.actor_map.get(int(actor_nr))
            # Also try TargetActorNr (253) for alv
            if not alv_name and gpb:
                try:
                    targ = gpb.get("params", {}).get("TargetActorNr")
                    if isinstance(targ, dict) and "value" in targ:
                        targ = targ["value"]
                        if isinstance(targ, dict) and "value" in targ:
                            targ = targ["value"]
                    if targ is not None:
                        alv_name = room.actor_map.get(int(targ), alv_name)
                        # also update actor_nr to target for lookup
                        actor_nr = int(targ) if isinstance(targ, int) or str(targ).isdigit() else actor_nr
                except Exception:
                    pass
            target = None
            target_key = None
            if actor_nr is not None and int(actor_nr) in room.players:
                target = room.players[int(actor_nr)]
                target_key = int(actor_nr)
            elif alv_name:
                for k, p in room.players.items():
                    if _names_equal(p.username, alv_name):
                        target = p
                        target_key = k
                        break
                # fallback to packet.player_name if alv_name differs
                if not target and packet.player_name:
                    for k, p in room.players.items():
                        if _names_equal(p.username, packet.player_name):
                            target = p
                            target_key = k
                            break
            if target is None and alv_name:
                for k, p in room.players.items():
                    if _names_equal(p.username, alv_name):
                        target = p
                        target_key = k
                        break
                if target is None:
                    key = int(actor_nr) if actor_nr is not None else -1
                    if key == -1:
                        key = hash(alv_name) % 100000
                        while key in room.players:
                            key = (key + 1) % 100000
                    skid, pid, pfid, pt = _other_props()
                    target = Player(
                        actor_nr=key,
                        username=alv_name,
                        game_id=str(pfid) if pfid else None,
                        skin=str(skid) if skid else None,
                        pickaxe=str(pid) if pid else None,
                        is_alive=packet.is_alive,
                        is_self=_names_equal(alv_name, view.me.username if view.me else None),
                        pt=pt,
                    )
                    if pt: _rebuild_teams(room)
                    room.players[key] = target
                    # also populate actor_map
                    try:
                        if actor_nr is not None:
                            room.actor_map[int(actor_nr)] = alv_name
                    except Exception:
                        pass
                    target_key = key
                    if packet.packet_type == PacketType.ALIVE:
                        room.players_count += 1
                        log_engine("alv", session_id[:8], name=alv_name, alive=True, count=room.players_count, room=room.room_id[:8], actor=actor_nr, target=target_actor_nr)
            if target:
                was_alive = target.is_alive
                target.is_alive = packet.is_alive
                # fill skin/pickaxe/pt from this update (_pt ties to actorNr/TargetActorNr)
                skid, pid, pfid, pt = _other_props()
                if skid: target.skin = skid
                if pid: target.pickaxe = pid
                if pfid: target.game_id = pfid
                if pt:
                    target.pt = pt
                    _rebuild_teams(room)
                # also ready
                try:
                    if gpb and gpb.get("properties", {}).get("ready") is not None:
                        target.is_ready = bool(gpb["properties"]["ready"])
                except Exception:
                    pass
                if not target.is_alive and was_alive:
                    if room.players_count > 0:
                        room.players_count = max(0, room.players_count - 1)
                    log_engine("alv", session_id[:8], name=target.username, alive=False, count=room.players_count, room=room.room_id[:8], actor=actor_nr, target=target_actor_nr)
                elif target.is_alive and not was_alive:
                    room.players_count += 1
                    log_engine("alv", session_id[:8], name=target.username, alive=True, count=room.players_count, room=room.room_id[:8], actor=actor_nr, target=target_actor_nr)
                    # revive/respawn/round-reset for self = new life: reset per-life flags
                    if target.is_self or (view.me and _names_equal(target.username, view.me.username)):
                        room.had_self_death = False
                        room.had_self_defeat = False
                        log_engine("debug", session_id[:8], msg=f"revive reset flags for {target.username}", room=room.room_id[:8])
                if target.is_self or (view.me and _names_equal(target.username, view.me.username)):
                    if view.me:
                        view.me.is_alive = target.is_alive
                        if target.skin: view.me.skin = target.skin
                    if room.me and _names_equal(target.username, room.me.username):
                        room.me.is_alive = target.is_alive
                        if target.skin: room.me.skin = target.skin
        # ---- SKIN / PICKAXE / GAME_ID / _pt via PropertiesChanged without alv (room-scoped) ----
        # Other players skins often arrive as type 4 code 253 with skid/pid but no alv — also _pt for duos via actorNr/TargetActorNr
        if gpb and gpb.get("properties") and packet.packet_type not in (PacketType.ALIVE, PacketType.ELIMINATED):
            props = gpb["properties"]
            if any(k in props for k in ("skid", "pid", "pfid", "BPid", "_pt", "pt")):
                # Subject resolution: TargetActorNr first (subject), then ActorNr (sender),
                # then 255/name. _pt packets carry the subject ONLY in TargetActorNr.
                tname = props.get("255") or packet.player_name
                _tk, tgt = _find_player(room, actor=actor_nr, target=target_actor_nr, name=tname)
                skid, pid, pfid, pt = _other_props()
                if tgt is not None:
                    if pt and tgt.pt != pt:
                        tgt.pt = pt
                        _rebuild_teams(room)
                        log_engine("team_pt", session_id[:8], team=pt, target=_tk, player=tgt.username, via="props", room=room.room_id[:8])
                    if skid and not tgt.skin:
                        tgt.skin = skid
                        log_engine("skin", session_id[:8], name=tgt.username, skin=skid, room=room.room_id[:8])
                        feed_hint = f"skin filled for {tgt.username} via PropertiesChanged"
                    if pid and not tgt.pickaxe:
                        tgt.pickaxe = pid
                    if pfid and not tgt.game_id:
                        tgt.game_id = pfid
                    if "ready" in props:
                        tgt.is_ready = bool(props["ready"])
                elif pt and target_actor_nr:
                    # Subject not in room yet (late joiner): stash _pt for JOIN to apply
                    try:
                        room.pending_pt[int(target_actor_nr)] = pt
                        log_engine("team_pt", session_id[:8], team=pt, target=int(target_actor_nr), via="pending stash", room=room.room_id[:8])
                    except Exception:
                        pass

        # ---- KILLFEED (room-scoped) — validated, dedup only on retransmit (same rawHex)
        if packet.is_kill and packet.killer and packet.victim:
            from ..gpbinary import _strip_color_tags as _strip
            killer = _strip(str(packet.killer)).strip()
            victim = _strip(str(packet.victim)).strip()
            if not killer or not victim or "<" in killer or "<" in victim:
                log_engine("debug", session_id[:8], msg=f"kill dropped empty killer={killer!r} victim={victim!r}", room=room.room_id[:8])
            else:
                # Dedup: only if same rawHex as last kill (Photon retransmit), not same victim in new round (DF 2 kills)
                is_dup = packet.raw_hex and packet.raw_hex == room.last_kill_hex
                if is_dup:
                    log_engine("debug", session_id[:8], msg=f"kill dup ignored {killer}->{victim} same hex", room=room.room_id[:8])
                else:
                    log_engine("killfeed", session_id[:8], killer=killer, victim=victim, room=room.room_id[:8])
                    room.recent_kills.append(KillInfo(instigator=killer, instigated=victim, event_type="killed"))
                    room.last_kill_hex = packet.raw_hex
                    if len(room.recent_kills) > 100:
                        room.recent_kills = room.recent_kills[-100:]
                    # Late join via killfeed — only for valid deduped kills
                    for name, is_target in [(killer, False), (victim, True)]:
                        if name:
                            existing = any(_names_equal(p.username, name) for p in room.players.values())
                            if not existing:
                                key = -1 if len(room.players) > 50 else hash(name) % 100000
                                while key in room.players:
                                    key = (key + 1) % 100000
                                room.players[key] = Player(
                                    actor_nr=-1,
                                    username=name,
                                    is_alive=not is_target,
                                    is_self=(view.me and _names_equal(name, view.me.username)),
                                )
                                log_engine("join", session_id[:8], name=name, source="killfeed", alive=not is_target, room=room.room_id[:8])
        elif packet.is_knock and packet.killer and packet.victim:
            room.recent_knocks.append(KillInfo(instigator=packet.killer, instigated=packet.victim, event_type="knocked"))
            if len(room.recent_knocks) > 100:
                room.recent_knocks = room.recent_knocks[-100:]
        # ---- Kill victim: mark player dead — Processor handles defeat calculation ----
        if packet.is_kill and packet.victim and room.state == State.MATCH:
            # Mark victim as dead in room state
            for p in room.players.values():
                if _names_equal(p.username, packet.victim):
                    p.is_alive = False
                    break
            if view.me and _names_equal(view.me.username, packet.victim):
                view.me.is_alive = False
            if room.me and _names_equal(room.me.username, packet.victim):
                room.me.is_alive = False
            if room.players_count > 0:
                room.players_count = max(0, room.players_count - 1)

        # ---- TEAMS (duo modes: WWZW, BF2V2) ----
        if room.game_mode in (GameMode.WWZW, GameMode.BF2V2) or (room.game_mode and "2v2" in room.game_mode.value):
            if room.party.is_party and room.party.code:
                tid = room.party.code
                if tid not in room.teams:
                    from .models import Team
                    room.teams[tid] = Team(team_id=tid, members=[], is_self_team=True)
                team = room.teams[tid]
                # ensure all party players in team
                for p in room.players.values():
                    if p.username not in team.members:
                        # heuristic: if player is in party (has same code) or is me, add
                        # For now, add any alive teammate that shares party code; fallback add me
                        if view.me and p.username == view.me.username:
                            if p.username not in team.members:
                                team.members.append(p.username)
                        elif p.is_alive or p.username in [m for m in team.members]:
                            # keep team size ≤2 for duo
                            if len(team.members) < 2 and p.username not in team.members:
                                team.members.append(p.username)
                # ensure me always in team
                if view.me and view.me.username not in team.members and len(team.members) < 2:
                    team.members.insert(0, view.me.username)

        # ---- PLAYTIME: ensure last_state reflects current room state for next tick ----
        # (tick handled at top of next feed; also update on state change)
        if view.playtime.last_state != room.state.value:
            view.playtime.last_state = room.state.value

        # Stamp packet with room context for processor (per-packet gm/room, avoids batch-final gm)
        packet.room_id = room.room_id
        packet.room_code = room.code
        packet.short_code = room.short_code
        packet.match_version = room.match_version
        packet.room_gm = room.game_mode.value if room.game_mode else None
        # Prefer room gm over packet gm for killfeed grouping
        if not packet.game_mode and room.game_mode:
            packet.game_mode = room.game_mode

        return packet

    def get_session(self, session_id: str) -> "SessionView | None":
        """Get SessionView for a session_id (read-only)."""
        return self._sessions.get(session_id)

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    def _extract_actor_nr(self, gpb: dict | None) -> int | None:
        """Try to get ActorNr from gpb params."""

        if not gpb:
            return None
        try:
            params = gpb.get("params", {})
            if isinstance(params, dict) and "ActorNr" in params:
                val = params["ActorNr"]
                if isinstance(val, dict) and "value" in val:
                    return int(val["value"])
                return int(val)
            # Also try raw actorNr field
            if gpb.get("actorNr") is not None:
                return int(gpb["actorNr"])
        except Exception:
            pass
        return None

    def _gpb_to_abstract(self, hex_string: str, gpb: dict | None) -> EnginePacket:
        """Build EnginePacket directly from gpb — no photon_api, only gpbinary."""

        pkt = EnginePacket(raw_hex=hex_string, raw=gpb)
        if not gpb:
            return pkt

        # game_mode from gm (exact, case-insensitive, longest-prefix for variants)
        gm_str = gpb.get("gm")
        if gm_str:
            pkt.game_mode = GameMode.from_gm(str(gm_str))

        # curScn
        cur = gpb.get("curScn") or gpb.get("properties", {}).get("curScn")
        if cur:
            pkt.cur_scn = str(cur)
            if not pkt.game_mode:
                try:
                    from .enums import GameMode as GM2

                    inferred = GM2.from_curscn(str(cur)) if hasattr(GM2, "from_curscn") else None
                    # Fallback import from photon_api enum if needed — GameMode has no from_curscn in engine enums
                    if not inferred:
                        # engine GameMode is simple; try substring match
                        low = str(cur).lower()
                        if "lategame" in low:
                            pkt.game_mode = GameMode.ZW_LG
                        elif "zonewars" in low:
                            pkt.game_mode = GameMode.ZW
                        elif "mainworld" in low:
                            pkt.game_mode = GameMode.DF
                except Exception:
                    pass

        # party
        party_val = gpb.get("Party")
        if party_val is not None:
            pkt.party = "party" if party_val else "solo"

        # zone
        props = gpb.get("properties", {})
        if "stG" in props or "bz_phase_start" in props:
            pkt.zone_time = props.get("bz_phase_start", props.get("stG"))
        if "bz_zone" in props:
            pkt.zone_phase = str(props["bz_zone"])
        elif "bz_phase" in props:
            pkt.zone_phase = str(props["bz_phase"])

        # player_name
        if "255" in props:
            pkt.player_name = str(props["255"])

        # killfeed
        kill_raw = gpb.get("killfeed")
        if kill_raw:
            parsed = parse_killfeed_text(kill_raw)
            if parsed["type"] == "kill":
                pkt.packet_type = PacketType.KILL
                pkt.is_kill = True
                pkt.killer = parsed["killer"]
                pkt.victim = parsed["victim"]
            elif parsed["type"] == "knock":
                pkt.packet_type = PacketType.KNOCK
                pkt.is_knock = True
                pkt.killer = parsed["killer"]
                pkt.victim = parsed["victim"]
            return pkt

        # join / leave / alv
        if gpb.get("evName") == "Join":
            pkt.packet_type = PacketType.JOIN
            pkt.is_join = True
        elif gpb.get("evName") == "Leave":
            pkt.packet_type = PacketType.LEAVE
            pkt.is_leave = True
        elif "alv" in props:
            if props["alv"]:
                pkt.packet_type = PacketType.ALIVE
                pkt.is_alive = True
            else:
                pkt.packet_type = PacketType.ELIMINATED
                pkt.is_eliminated = True
            if not pkt.player_name and "255" in props:
                pkt.player_name = str(props["255"])
        elif gpb.get("opName") in ("CreateRoom", "JoinRoom", "JoinRandomRoom"):
            pkt.packet_type = PacketType.JOIN_ROOM if "Join" in str(gpb.get("opName")) else PacketType.CREATE_ROOM
        elif gpb.get("opName") in ("Authenticate", "AuthenticateOnce"):
            pkt.packet_type = PacketType.AUTHENTICATE
        elif "curScn" in props:
            pkt.packet_type = PacketType.SCENE
        elif "stG" in props or "bz_zone" in props:
            pkt.packet_type = PacketType.ZONE

        return pkt