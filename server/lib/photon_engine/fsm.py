"""
fsm.py — Strict per-room FSM for LOBBY <-> MATCH.

Owner: engine.py  (called once per packet, after room resolution)

Philosophy (from user spec):
  - LOBBY  enter: authenticate packet, leave packet (for the playing player*)
  - LOBBY  exit : curScn / "scene" packet  (curScn != "MainLobby" -> MATCH)
  - MATCH  enter: curScn / "scene" packet  (curScn != "MainLobby")
  - MATCH  exit : authenticate packet, leave packet (self)
  * individualist: only the user who owns the userscript matters in public/friendly
    private rooms.  Host mode in server-created rooms (short_code) is the only
    collaborative mode that tracks others' kills/wins/defeats.

Invariants enforced here:
  - No other packet type may flip state (killfeed, stG, skin, etc are ignored).
  - No state flicker: if target == current, suppress and log at debug.
- GM (game_mode) is set on MATCH enter via SCENE's gm/curScn inference and
  may be corrected by curScn afterwards (curScn is ground truth: join packets
  default gm=DF). DF/UNKNOWN never clobber a concrete mode, and mid-match
  concrete-vs-concrete mismatches are still dropped as flicker. Cleared only
  on MATCH exit (LOBBY enter).
  - on_enter / on_exit hooks are the ONLY place that mutates state/gm/zone
    and playtime last_state.  Callers must not write room.state directly.
"""

from __future__ import annotations

from .enums import GameMode, PacketType, State
from ..logger import log_engine


def _norm_name(n: str | None) -> str:
    if not n:
        return ""
    from ..gpbinary import _strip_color_tags
    s = _strip_color_tags(str(n)).strip()
    if s.lower().startswith("dc:"):
        s = s[3:].strip()
    return s

def _names_equal(a, b) -> bool:
    na = _norm_name(a); nb = _norm_name(b)
    return bool(na and nb and na == nb)


class RoomFSM:
    """
    Strict two-state machine.  One instance is stateless — all state lives on RoomView.
    Call RoomFSM.transition(room, view, packet, gpb, cur_scn, session_id) per packet.
    Returns (did_transition: bool, from_state, to_state, via).
    """

    # Canonical triggers — nothing else may flip state
    TRIGGER_SCENE = "scene"          # gpb curScn present
    TRIGGER_AUTH  = "authenticate"   # opName Authenticate/AuthenticateOnce
    TRIGGER_LEAVE = "leave_self"     # Leave where player_name == me

    @staticmethod
    def _classify_triggers(packet, gpb, view, cur_scn, room=None):
        triggers: list[str] = []
        params: dict = {}

        if gpb and gpb.get("opName") in ("Authenticate", "AuthenticateOnce"):
            triggers.append(RoomFSM.TRIGGER_AUTH)
        # leave_self only if victim is the playing user (individualist)
        if packet.packet_type == PacketType.LEAVE and packet.player_name:
            me_name = None
            if view.me and view.me.username:
                me_name = view.me.username
            elif room and room.me and room.me.username:
                me_name = room.me.username
            if me_name and _names_equal(packet.player_name, me_name):
                triggers.append(RoomFSM.TRIGGER_LEAVE)
            # NOTE: raw leave from host-mode collaborative path is NOT a trigger here.
            # The engine is individualist by default; host-mode collaborative leaves are
            # handled in processor.py via room short_code.  Engine ignores non-self leaves.

        if cur_scn is not None:
            # scene packet is present when curScn key exists (even if lobby)
            triggers.append(RoomFSM.TRIGGER_SCENE)
            params["cur_scn"] = cur_scn

        return triggers, params

    @staticmethod
    def transition(room, view, packet, gpb, cur_scn, session_id: str):
        """
        Attempt a transition.  Returns True if state changed.
        Performs on_exit/on_enter exactly once per transition.
        """
        triggers, params = RoomFSM._classify_triggers(packet, gpb, view, cur_scn, room)

        # No trigger -> no transition
        if not triggers:
            return False, room.state, room.state, None

        target: State | None = None
        via: str | None = None

        # Priority: AUTH/LEAVE beat SCENE if both present (should not happen in same packet)
        # Evaluate strictly per spec:
        #   LOBBY  --scene(!=MainLobby)--> MATCH
        #   MATCH  --auth/leave--> LOBBY
        #   MATCH  --scene(MainLobby)--> LOBBY   (symmetric; allowed even though spec lists
        #                                          match exit as auth/leave only — scene MainLobby
        #                                          is the only way back without auth in practice)
        cur = room.state

        if cur == State.LOBBY:
            # LOBBY exit: only scene != MainLobby
            if RoomFSM.TRIGGER_SCENE in triggers:
                cs = params.get("cur_scn")
                if cs != "MainLobby":
                    target = State.MATCH
                    via = f"scene:{cs}"
                else:
                    # scene==MainLobby while in LOBBY -> no transition (already lobby)
                    return False, cur, cur, None
            # auth/leave while in LOBBY -> stay lobby (no flicker)
            elif RoomFSM.TRIGGER_AUTH in triggers or RoomFSM.TRIGGER_LEAVE in triggers:
                return False, cur, cur, None

        elif cur == State.MATCH:
            # MATCH exit: auth or leave_self
            if RoomFSM.TRIGGER_AUTH in triggers:
                target = State.LOBBY
                via = "authenticate"
            elif RoomFSM.TRIGGER_LEAVE in triggers:
                target = State.LOBBY
                via = f"leave:{packet.player_name}"
            elif RoomFSM.TRIGGER_SCENE in triggers:
                cs = params.get("cur_scn")
                # strict: scene MainLobby is also a valid match exit (return to lobby)
                # scene !=MainLobby while already in MATCH -> no transition (stay)
                if cs == "MainLobby":
                    target = State.LOBBY
                    via = f"scene:{cs}"
                else:
                    return False, cur, cur, None

        if target is None or target == cur:
            return False, cur, cur, None

    @staticmethod
    def reenter_match_for_series(room, view, session_id: str) -> bool:
        """stG=1 fresh edge while LOBBY: the next match is starting but no
        scene packet arrived to re-enter MATCH (continuous party series with
        no lobby return, or a public room parked by an intermission blip), so
        without this the room sits in LOBBY and phase-end drains never fire
        — wins silently lost after the first match.

        Guards (all must hold): currently LOBBY, not the lobby pseudo-room,
        and I'm still on the roster — leavers (row pruned on leave) and empty
        rooms stay lobby. Public rooms prove the match via match_version > 0,
        but a first public match (v0) still qualifies when I'm on the roster:
        stG=1 with me present means the match is starting even though no scene
        ever arrived (JoinRoom carries no curScn). Unknown identity + a
        populated room (>= 2 tracked players) still re-enters when versioned.
        Runs the normal on_exit/on_enter pair (flag/marker reset, re-arm, map
        reseed), so post-conditions match a scene-driven enter. Numbers the
        match 0->1 on first enter so the win drain gate can fire. Never raises.
        """
        try:
            from .enums import State as _State
            if getattr(room, "state", None) != _State.LOBBY:
                return False
            if str(getattr(room, "room_id", "") or "") == "lobby":
                return False
            me = getattr(room, "me", None) or getattr(view, "me", None)
            me_name = getattr(me, "username", None) if me is not None else None
            try:
                players = list((getattr(room, "players", None) or {}).values())
            except Exception:
                players = []
            _me_on_roster = False
            if me_name:
                try:
                    _me_on_roster = any(_names_equal(getattr(p, "username", None), me_name) for p in players)
                    if not _me_on_roster:
                        return False
                except Exception:
                    return False
            elif len(players) < 2:
                return False
            try:
                _v0 = int(getattr(room, "match_version", 0) or 0) <= 0
            except Exception:
                _v0 = True
            if _v0 and not getattr(room, "short_code", None) and not _me_on_roster:
                return False
            old = room.state
            RoomFSM._on_exit(room, view, old, _State.MATCH, "stg:1", session_id, None, None)
            room.state = _State.MATCH
            RoomFSM._on_enter(room, view, old, _State.MATCH, "stg:1", session_id, None, None, None)
            try:
                if int(getattr(room, "match_version", 0) or 0) <= 0:
                    room.match_version = 1
            except Exception:
                pass
            try:
                room.match_playtime.last_state = _State.MATCH.value
                view.playtime.last_state = _State.MATCH.value
            except Exception:
                pass
            log_engine("state", session_id[:13], old=old.value, new=_State.MATCH.value,
                       room=room.room_id[:8], via="stg:1", curScn="-")
            return True
        except Exception:
            return False

        # ---- guards passed: perform on_exit + on_enter atomically ----
        old = cur
        # on_exit for old state
        RoomFSM._on_exit(room, view, old, target, via, session_id, packet, gpb)
        # flip
        room.state = target
        # on_enter for new state
        RoomFSM._on_enter(room, view, old, target, via, session_id, packet, gpb, cur_scn)

        log_engine("state", session_id[:13], old=old.value, new=target.value, room=room.room_id[:8], via=via,
                   curScn=cur_scn or packet.cur_scn or "-")
        # keep playtime last_state in sync — engine tick reads this
        try:
            room.match_playtime.last_state = target.value
            view.playtime.last_state = target.value
        except Exception:
            pass

        return True, old, target, via

    @staticmethod
    def _on_exit(room, view, old: State, new: State, via, session_id, packet, gpb):
        # MATCH -> LOBBY : clear zone-active, keep gm/match_version for post-match drain
        # do NOT clear game_mode here — processor drains win/defeat after exit; clear on next MATCH enter
        if old == State.MATCH and new == State.LOBBY:
            room.zone_match_active = False
            # usernameChangeCount reset is handled on AUTH/SCENE MainLobby; keep here for leave
            # do not bump match_version here — stG 3 bumps it in engine's stG handler

    @staticmethod
    def _on_enter(room, view, old: State, new: State, via, session_id, packet, gpb, cur_scn):
        # LOBBY -> MATCH : lock GM exactly once
        if old == State.LOBBY and new == State.MATCH:
            # Prefer packet.game_mode (from gm param) else infer from curScn.
            # curScn is ground truth: join/matchmaking packets default gm=DF, so a
            # DF/blank room adopts the scene mode (ZoneWars_LateGame -> zw_lg).
            # adopt_game_mode never clobbers with DF and only corrects
            # concrete-vs-concrete, so lobby scenes stay safe.
            gm = packet.game_mode if packet and packet.game_mode else None
            if (not gm or gm == GameMode.DF or gm == GameMode.UNKNOWN) and cur_scn:
                try:
                    scene_gm = GameMode.from_curscn(cur_scn, gm or getattr(room, "game_mode", None))
                    if scene_gm is not None:
                        gm = scene_gm
                except Exception:
                    pass
            if gm:
                try:
                    if room.adopt_game_mode(gm):
                        log_engine("room", session_id[:13], action="gm", gm=room.game_mode.value, room=room.room_id[:8], via=f"fsm:{via}")
                    elif gm != room.game_mode:
                        log_engine("debug", session_id[:13],
                                   msg=f"gm flicker suppressed {room.game_mode.value} != {gm.value}", room=room.room_id[:8])
                except Exception:
                    pass
            # if still no gm, keep pending_gm plumbing in engine.py to fill later on pending pop
            # (stG-driven re-enters pass cur_scn=None: never wipe the known scene)
            if cur_scn is not None:
                room.cur_scn = cur_scn
            # new match = new life: reset per-life death/defeat flags + drain markers
            room.had_self_death = False
            room.had_self_defeat = False
            room.last_win_version = -1
            room.last_defeat_version = -1
            # clear any unconsumed phase-end arm from the previous match (drain is
            # edge-triggered now: a batch ending on stG=2 must not leak its arm here)
            room.stg_is_phase_end = False
            # re-arm the stG=3 one-shot for this match (scene-driven starts
            # may never emit stG=1; the bump still needs its single shot)
            try:
                room.stg3_armed = True
            except Exception:
                pass
            # fresh routing + history: drop previous match's actor names, kills, knocks
            # (rebuilt from Join/alv/killfeed packets; players/teams/counts are live-managed)
            try:
                room.actor_map = {}
                room.recent_kills = []
                room.recent_knocks = []
                # ...except self: keep my actor routing so a nameless alv=false
                # about me (e.g. a quiet zone elim with no killfeed) still
                # resolves after the wipe. Actor numbers are stable per room
                # and later Joins overwrite entries, so a stale seed can't shadow.
                # Only when I've actually joined THIS room (in players): a scene
                # packet arriving before my join would otherwise seed a stale
                # actor from a previous room and misattribute others to me.
                _self = room.me or view.me
                _an = getattr(_self, "actor_nr", -1) if _self is not None else -1
                _un = getattr(_self, "username", None) if _self is not None else None
                _joined = bool(_un) and any(
                    _names_equal(p.username, _un) for p in room.players.values()
                )
                if _joined and isinstance(_an, int) and _an not in (None, -1, 0):
                    room.actor_map[int(_an)] = _un
            except Exception:
                pass
            log_engine("debug", session_id[:13], msg="room reset for new match (map/kills cleared)", room=room.room_id[:8])
        elif new == State.LOBBY and via and via.startswith("scene:"):
            # SCENE MainLobby enter -> reset cur_scn and optionally clear pending stg
            room.cur_scn = cur_scn
            room.zone_match_active = False
