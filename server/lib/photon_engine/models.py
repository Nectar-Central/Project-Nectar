"""
models.py — Real-named dataclasses for the engine.

No more `properties['255']` or `params[254]` — you get `Player.username`
and `Player.actor_nr`. Every field is documented with the JSON packet
that sets it and a use case.

All classes are plain dataclasses with `to_dict()` for JSON serialization.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal

from .enums import GameMode, PacketType, State


@dataclass
class Player:
    """
    A player in the room — live-mutated as packets arrive.

    JSON source (first Create/Join):
        {
            "opName": "CreateRoom",
            "playerProps": {"255": "true jew", "pfid": "CB6F...", "skid": "cct_...", "pid": "Pickaxe_42"}
        }

    Live updates via PropertiesChanged:
        {"evName": "PropertiesChanged", "properties": {"skid": "cct_123", "ready": true, "alv": true}, "actorNr": 2}
        -> engine mutates players[2].skin / is_ready / is_alive in place
    """

    actor_nr: int              # 254, or -1 if late-found via killfeed (see engine.py)
    username: str              # 255
    game_id: str | None = None # pfid
    skin: str | None = None    # skid
    pickaxe: str | None = None # pid
    is_ready: bool = False     # ready
    is_alive: bool = True      # alv
    is_self: bool = False      # true if this is the session's own player (me)
    pt: str | None = None      # _pt / pt — team id for duos (wwzw/BF2V2/DF duo), maps to RoomView.teams

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Room:
    """
    Room info — from CreateRoom/JoinRoom.

    JSON source:
        {
            "opName": "CreateRoom",
            "params": {"RoomName": "ABC123", "GameProperties": {"255": 12}}
        }
    """

    code: str | None = None         # 255 RoomName
    max_players: int | None = None  # GameProperties[255]
    region: str | None = None       # 230 Address

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Party:
    """
    Party state — from GameProperties.Party and RoomName.

    JSON source:
        {"GameProperties": {"Party": true}, "RoomName": "ABC123"}
    """

    is_party: bool = False          # Party bool
    code: str | None = None         # same as Room.code when is_party

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Zone:
    """
    Zone timing — from stG / bz_*.

    JSON source:
        {"properties": {"stG": 30}} or {"properties": {"bz_phase_start": 10, "bz_zone": "2"}}

    In zonewars, timer is 5:00 per phase. Use helpers for mm:ss.
    """

    time: int | None = None         # stG / bz_phase_start (seconds for this phase)
    phase: str | None = None        # bz_zone / bz_phase
    pattern: str | None = None      # bz_pattern
    started_at: float | None = None # epoch seconds when this phase's time was set
    initial_time: int | None = None # snapshot of time at phase start for countdown

    def set_time(self, seconds: int | None, phase: str | None = None):
        """Set zone time and record start for countdown."""
        import time as _t
        if seconds is not None:
            self.time = int(seconds)
            self.initial_time = int(seconds)
            self.started_at = _t.time()
        if phase is not None:
            self.phase = str(phase)

    def remaining(self) -> int | None:
        """Seconds remaining in current phase (countdown from initial_time)."""
        import time as _t
        if self.time is None:
            return None
        if self.started_at is None or self.initial_time is None:
            return self.time
        elapsed = int(_t.time() - self.started_at)
        return max(0, self.initial_time - elapsed)

    def remaining_mmss(self) -> str | None:
        """mm:ss for remaining time, e.g. 04:30."""
        r = self.remaining()
        if r is None:
            return None
        return f"{r // 60:02d}:{r % 60:02d}"

    def is_at(self, mm: int, ss: int, tolerance: int = 2) -> bool:
        """Check if remaining is near mm:ss (tolerance seconds)."""
        r = self.remaining()
        if r is None:
            return False
        target = mm * 60 + ss
        return abs(r - target) <= tolerance

    def is_between(self, from_mmss: tuple[int, int], to_mmss: tuple[int, int]) -> bool:
        """Check if remaining is between two mm:ss values."""
        r = self.remaining()
        if r is None:
            return False
        a = from_mmss[0] * 60 + from_mmss[1]
        b = to_mmss[0] * 60 + to_mmss[1]
        lo, hi = min(a, b), max(a, b)
        return lo <= r <= hi

    def to_dict(self) -> dict:
        d = asdict(self)
        # add computed helpers for API consumers
        try:
            d["remaining"] = self.remaining()
            d["remaining_mmss"] = self.remaining_mmss()
        except Exception:
            pass
        return d


@dataclass
class KillInfo:
    """
    Killfeed event — from Event 101 Data.

    JSON source:
        {"evName": "Killfeed", "killfeed": "<color> true jew </color> killed <color> Bob </color>"}
        -> parsed via parse_killfeed_text

    Fields are resolved to plain names, not color tags.
    """

    instigator: str        # killer name
    instigated: str        # victim name
    event_type: str        # "killed" / "knocked" / "left"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AbstractPacket:
    """
    One parsed packet — what engine.feed() returns per hex.

    Use case:
        pkt = engine.feed("sess_abc", hex)
        if pkt.packet == PacketType.KILL and pkt.kill.instigator == session.me.username:
            my_kills += 1  # you decide, engine just gives data

    Fields are None if not present in that packet type.
    """

    packet: PacketType
    player: Player | None = None
    room: Room | None = None
    party: Party | None = None
    zone: Zone | None = None
    game_mode: GameMode | None = None
    cur_scn: str | None = None
    kill: KillInfo | None = None
    raw_hex: str = ""
    raw: dict | None = None  # escape hatch: original gpb for power users

    def to_dict(self) -> dict:
        d = asdict(self)
        # Enums to values for JSON
        d["packet"] = self.packet.value
        if self.game_mode:
            d["game_mode"] = self.game_mode.value
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class Team:
    """Duo/team grouping — for WWZW/BF2V2 etc."""

    team_id: str                      # Party code or auto id
    members: list[str] = field(default_factory=list)  # usernames
    is_self_team: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RoomView:
    """
    Scoped view of one Photon room — created on CreateRoom/JoinRoom/JoinRandomRoom.
    All players/zone/kills/state are scoped to this room, not the parent session.
    Each room has its own me (per-room identity, actor_nr may differ per room).
    """

    room_id: str                                    # RoomName UUID e.g. 3a0596a5-f50c-45ef-bbdb-ef4842e89c6d
    code: str | None = None                          # full RoomName UUID
    short_code: str | None = None                    # 5-char pID e.g. PBS05 — private party join code (players type this)
    me: Player | None = None                         # per-room me (not session-wide)
    players: dict[int, Player] = field(default_factory=dict)
    actor_map: dict[int, str] = field(default_factory=dict)  # actorNr -> username, built from ActorList + Join 255 (for alv without 255)
    pending_pt: dict[int, str] = field(default_factory=dict)  # actorNr -> team id stashed from _pt packets for late joiners
    gv: str | None = None  # game variant from Create/Join GameProperties ("solo"/"duo"/...) — duo modes have gv != solo

    @property
    def is_duo(self) -> bool:
        """Duo room iff gv is set and not solo (duo DF, duo zw_lg, wwzw-style teams)."""
        return bool(self.gv) and self.gv.lower() != "solo"
    last_kill_hex: str | None = None  # dedup: last kill rawHex (retransmit guard, not same victim in new round)
    players_count: int = 0
    max_players: int | None = None
    state: State = State.LOBBY
    game_mode: GameMode | None = None
    cur_scn: str | None = None
    zone: Zone = field(default_factory=Zone)
    party: Party = field(default_factory=Party)
    teams: dict[str, Team] = field(default_factory=dict)  # team_id -> Team (duo)
    recent_kills: list[KillInfo] = field(default_factory=list)
    recent_knocks: list[KillInfo] = field(default_factory=list)
    match_version: int = 0                           # increments on stG 3→1 for private zw_lg multi-match per room
    match_playtime: Playtime = field(default_factory=lambda: Playtime())  # per-room playtime (ticks per match_version)
    last_win_version: int = -1                       # dedup win per match_version
    last_defeat_version: int = -1                    # dedup defeat per match_version
    had_self_death: bool = False                     # self death already counted this life (reset on match enter / revive)
    had_self_defeat: bool = False                    # self defeat already recorded this life (reset on match enter / revive)
    last_stg: int | None = None                      # last stG value for win/loss per GM (3→1)
    stg_is_phase_end: bool = False                 # True only when stG=3 arrived as phase end (from below), never timer ticks
    winner: str | None = None                        # last match winner (username or team_id)
    created_at: str | None = None
    # zone bookend for zone modes (zw_lg/wwzw): first zone = start, second zone = win/defeat evaluation
    zone_match_active: bool = False
    last_zone_hex: str | None = None

    def get_team(self, username: str) -> Team | None:
        for t in self.teams.values():
            if username in t.members:
                return t
        return None

    def to_dict(self) -> dict:
        return {
            "room_id": self.room_id,
            "code": self.code,
            "short_code": self.short_code,
            "me": self.me.to_dict() if self.me else None,
            "players": {str(k): v.to_dict() for k, v in self.players.items()},
            "actor_map": {str(k): v for k, v in self.actor_map.items()},
            "pending_pt": {str(k): v for k, v in self.pending_pt.items()},
            "gv": self.gv,
            "players_count": self.players_count,
            "max_players": self.max_players,
            "state": self.state.value,
            "game_mode": self.game_mode.value if self.game_mode else None,
            "cur_scn": self.cur_scn,
            "zone": self.zone.to_dict(),
            "party": self.party.to_dict(),
            "teams": {tid: t.to_dict() for tid, t in self.teams.items()},
            "recent_kills": [k.to_dict() for k in self.recent_kills[-20:]],
            "recent_knocks": [k.to_dict() for k in self.recent_knocks[-20:]],
            "match_version": self.match_version,
            "match_playtime": self.match_playtime.to_dict(),
            "stg_is_phase_end": self.stg_is_phase_end,
            "last_win_version": self.last_win_version,
            "last_defeat_version": self.last_defeat_version,
            "had_self_death": self.had_self_death,
            "had_self_defeat": self.had_self_defeat,
            "winner": self.winner,
        }


@dataclass
class Playtime:
    """Playtime tracking per session."""

    total_seconds: int = 0               # all states (match+lobby)
    per_gm: dict[str, int] = field(default_factory=dict)  # gm -> seconds
    lobby_seconds: int = 0
    last_state: str | None = None
    last_state_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SessionView:
    """
    Live view of one session — holds many rooms.
    Query: session → rooms[room_id] → scoped players/zone/kills.
    Back-compat: top-level players/zone/state proxy to current room.
    """

    session_id: str
    me: Player | None = None
    rooms: dict[str, RoomView] = field(default_factory=dict)  # room_id -> RoomView
    current_room_id: str | None = None
    usernameChangeCount: int = 0
    playtime: Playtime = field(default_factory=Playtime)
    host_mode: bool = False  # set from capture payload; true = server-room host (collaborative replay)
    # philosophy: individualist by default (public/friendly private) -> only self stats;
    # host_mode + short_code -> also track others for comp replay

    # Back-compat proxies — point to current room or empty defaults
    @property
    def current_room(self) -> RoomView | None:
        if self.current_room_id and self.current_room_id in self.rooms:
            return self.rooms[self.current_room_id]
        # fallback: first room
        if self.rooms:
            return next(iter(self.rooms.values()))
        return None

    @property
    def players(self) -> dict[int, Player]:
        r = self.current_room
        return r.players if r else {}

    @property
    def players_count(self) -> int:
        r = self.current_room
        return r.players_count if r else 0

    @players_count.setter
    def players_count(self, v: int):
        if self.current_room:
            self.current_room.players_count = v

    @property
    def max_players(self) -> int | None:
        r = self.current_room
        return r.max_players if r else None

    @max_players.setter
    def max_players(self, v: int | None):
        if self.current_room:
            self.current_room.max_players = v

    @property
    def state(self) -> State:
        r = self.current_room
        return r.state if r else State.LOBBY

    @state.setter
    def state(self, v: State):
        if self.current_room:
            self.current_room.state = v

    @property
    def game_mode(self) -> GameMode | None:
        r = self.current_room
        return r.game_mode if r else None

    @game_mode.setter
    def game_mode(self, v: GameMode | None):
        if self.current_room:
            self.current_room.game_mode = v

    @property
    def cur_scn(self) -> str | None:
        r = self.current_room
        return r.cur_scn if r else None

    @cur_scn.setter
    def cur_scn(self, v: str | None):
        if self.current_room:
            self.current_room.cur_scn = v

    @property
    def zone(self) -> Zone:
        r = self.current_room
        return r.zone if r else Zone()

    @property
    def room(self) -> Room:
        # compat: return Room shim from current RoomView
        r = self.current_room
        if r:
            return Room(code=r.code, max_players=r.max_players)
        return Room()

    @property
    def party(self) -> Party:
        r = self.current_room
        return r.party if r else Party()

    @property
    def recent_kills(self) -> list[KillInfo]:
        r = self.current_room
        return r.recent_kills if r else []

    @property
    def recent_knocks(self) -> list[KillInfo]:
        r = self.current_room
        return r.recent_knocks if r else []

    @property
    def is_playing(self) -> bool:
        return self.state == State.MATCH

    def get_room(self, room_id: str) -> RoomView | None:
        return self.rooms.get(room_id)

    def to_dict(self) -> dict:
        cur = self.current_room
        return {
            "session_id": self.session_id,
            "me": self.me.to_dict() if self.me else None,
            "current_room_id": self.current_room_id,
            "host_mode": self.host_mode,
            "rooms": {rid: rv.to_dict() for rid, rv in self.rooms.items()},
            "playtime": self.playtime.to_dict(),
            # back-compat top-level (current room)
            "players": {str(k): v.to_dict() for k, v in self.players.items()} if cur else {},
            "players_count": self.players_count,
            "max_players": self.max_players,
            "state": self.state.value if cur else State.LOBBY.value,
            "game_mode": self.game_mode.value if self.game_mode else None,
            "cur_scn": self.cur_scn,
            "zone": self.zone.to_dict() if cur else Zone().to_dict(),
            "room": self.room.to_dict(),
            "party": self.party.to_dict() if cur else Party().to_dict(),
            "teams": {tid: t.to_dict() for tid, t in cur.teams.items()} if cur else {},
            "recent_kills": [k.to_dict() for k in self.recent_kills[-20:]],
            "recent_knocks": [k.to_dict() for k in self.recent_knocks[-20:]],
        }
