"""
DEPRECATED — photon_api.py is legacy. Engine is now standalone (gpbinary only).

photon_api.py — Simple, developer-friendly wrapper over the Photon parser/decryptor.
Kept for examples/back-compat only. Production path (Engine/processor/capture) no longer imports it.
Use server.lib.photon_engine.Engine instead.

---
Original doc:

Goal: Hide the low-level GpBinary Reader, hex handling, and color-tag
parsing behind clean enums, classes, and functions.

A new developer should not need to understand:
    - F3 / F4 framing
    - GpBinaryV16 LE minimal
    - <color> tags in killfeed
    - ActorNr vs 255 vs pfid

They should just do:

    from server.lib.photon_api import Photon, GameMode, PacketType

    packet = Photon.parse("f302e6...")
    if packet.is_kill:
        print(f"{packet.killer} killed {packet.victim}")

Or event-driven:

    session = GameSession(discord_id="123", self_name="true jew")
    session.on_kill(lambda killer, victim: print(f"kill {killer} -> {victim}"))
    session.on_win(lambda gm: print(f"win in {gm}"))
    session.feed_hex("f302...")   # updates players, zone, win/defeat internally

Or for real-time capture (WebSocket / queue consumer), use MatchTracker —
it wraps GameSession and handles match lifecycle (new match = new session)
for you, so kills/deaths/players don't leak across matches:

    tracker = MatchTracker(self_name="true jew")
    tracker.on_kill(lambda killer, victim: print(f"kill {killer} -> {victim}"))
    tracker.on_win(lambda gm: print(f"win in {gm.human_name}"))

    # call this for every packet as it arrives, IN ORDER
    tracker.feed(hex_string)

IMPORTANT — MatchTracker / GameSession are NOT thread-safe. If packets
arrive from multiple threads (or multiple asyncio tasks), route them
through a single consumer (one queue.Queue + one dedicated thread, or
one asyncio task per match) rather than calling feed() concurrently.
For multiple simultaneous matches (e.g. tracking several players at
once), keep one MatchTracker per player/connection in a dict — do not
share a single tracker across matches.

The wrapper keeps full control — you can still access raw fields via
`packet.raw` or `packet.gpb` if you need low-level details.

Design principles (from code-simplification skill):
    - Preserve behavior exactly (delegates to gpbinary.py, no re-implementation)
    - Follow project conventions (snake_case, type hints, 4-space indent)
    - Prefer clarity over cleverness (explicit names, no nested ternaries)
    - Scope to what changed (wraps recent capture logic, doesn't rewrite DB)

DEBUG:
    Set PHOTON_API_DEBUG = True (below) to enable verbose print() tracing
    of parsing decisions and session state transitions. Leave it False
    in production to avoid log spam / perf overhead on hot capture loops.
"""

from __future__ import annotations

import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Callable, Any
from threading import Lock

# Reuse the existing low-level parser — we wrap it, we don't duplicate it
from .gpbinary import (
    parse_gpbinary,
    is_useful,
    human_sentence,
    parse_killfeed_text,
    is_realistic_curscn,
)


# ---------------------------------------------------------------------------
# Debug toggle
# ---------------------------------------------------------------------------

# Flip to True locally when debugging capture issues. Kept as a module-level
# constant (not an env var) so it's a single obvious on/off switch when
# handing this file to a coding agent.
PHOTON_API_DEBUG = False


def _debug(tag: str, msg: str) -> None:
    """Internal debug print — no-ops unless PHOTON_API_DEBUG is True."""

    if PHOTON_API_DEBUG:
        print(f"[photon_api:{tag}] {msg}")


# ---------------------------------------------------------------------------
# Enums — clear, discoverable names for developers
# ---------------------------------------------------------------------------

class GameMode(str, Enum):
    """
    Game modes as seen in `gm` / `curScn`.

    Use `GameMode.from_string("MainWorld")` to infer from curScn.
    """

    DF = "DF"               # Free Mode / MainWorld
    ZW = "zw"               # Zone Wars
    ZW_LG = "zw_lg"          # Lategame Zone Wars
    WW = "WW"               # Wild West
    WWZW = "wwzw"            # Duo Wild West (zone wars)
    BF = "BF"               # Boxfights
    BF2V2 = "BF2V2"          # Boxfights 2v2
    UNKNOWN = "unknown"

    @classmethod
    def from_curscn(cls, curscn: str | None) -> "GameMode | None":
        """Infer GameMode from a curScn string."""

        if not curscn:
            return None

        low = str(curscn).lower()

        if "lategame" in low or "zw_lg" in low:
            result = cls.ZW_LG
        elif "wildwest" in low or "wild west" in low or "wwzw" in low:
            result = cls.WWZW
        elif "zonewars" in low or "zone wars" in low:
            result = cls.ZW
        elif "boxfights" in low and "2v2" in low:
            result = cls.BF2V2
        elif "boxfights" in low:
            result = cls.BF
        elif "mainworld" in low:
            result = cls.DF
        elif "wild" in low:
            result = cls.WW
        elif "free" in low:
            result = cls.DF
        else:
            result = None

        _debug("GameMode.from_curscn", f"curscn={curscn!r} -> {result}")
        return result

    @property
    def human_name(self) -> str:
        """Human-readable name for logs/UI."""

        names = {
            GameMode.DF: "Free Mode",
            GameMode.ZW: "Zone Wars",
            GameMode.ZW_LG: "Lategame Zone Wars",
            GameMode.WW: "Wild West",
            GameMode.WWZW: "Duo Wild West",
            GameMode.BF: "Boxfights",
            GameMode.BF2V2: "Boxfights 2v2",
        }
        return names.get(self, self.value)


class PacketType(str, Enum):
    """High-level packet categories — hides opCode/evCode numbers."""

    AUTHENTICATE = "authenticate"
    JOIN_ROOM = "join_room"
    CREATE_ROOM = "create_room"
    JOIN = "join"                 # Player joined (event 255)
    LEAVE = "leave"               # Player left (event 254)
    ALIVE = "alive"               # alv property
    ELIMINATED = "eliminated"     # alv=false
    KILL = "kill"                 # killfeed killed
    KNOCK = "knock"               # killfeed knocked
    ZONE = "zone"                 # stG / bz_phase
    SCENE = "scene"               # curScn
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# PhotonPacket — one parsed packet, simple properties
# ---------------------------------------------------------------------------

@dataclass
class PhotonPacket:
    """
    One parsed Photon packet — the only object a developer needs.

    Attributes are all plain Python types, no Gp wrappers.

    Example:
        p = Photon.parse("f3046502...")
        if p.is_kill and p.killer == "true jew":
            print("you got a kill!")
    """

    # Raw hex and low-level gpb (for power users)
    raw_hex: str
    raw: dict[str, Any] | None = None

    # High-level fields (None if not present)
    packet_type: PacketType = PacketType.UNKNOWN
    game_mode: GameMode | None = None
    killer: str | None = None
    victim: str | None = None
    player_name: str | None = None  # for Join/Leave/alv
    zone_time: int | None = None
    zone_phase: str | None = None
    cur_scn: str | None = None
    party: str | None = None  # "party" / "solo"

    # Convenience flags
    is_kill: bool = False
    is_knock: bool = False
    is_join: bool = False
    is_leave: bool = False
    is_eliminated: bool = False
    is_alive: bool = False

    # Human sentence for logs (or None if noise)
    sentence: str | None = None

    def to_dict(self) -> dict:
        """Serialize to dict compatible with engine SessionView + processor.

        Includes both flat Photon fields and nested `kill` object for
        backward compat with AbstractPacket consumers.
        """
        d: dict[str, any] = {
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
        }
        # Nested kill object for AbstractPacket compat
        if self.is_kill or self.is_knock:
            d["kill"] = {
                "instigator": self.killer,
                "instigated": self.victim,
                "event_type": "killed" if self.is_kill else "knocked",
            }
        # Keep booleans even when False, drop Nones
        return {k: v for k, v in d.items() if v is not None or k.startswith("is_")}

    @property
    def is_useful(self) -> bool:
        """Was this packet useful for Rich Presence?"""

        return self.sentence is not None

    @property
    def gm_human(self) -> str:
        """Human game mode name."""

        if self.game_mode:
            return self.game_mode.human_name
        return "unknown"


# ---------------------------------------------------------------------------
# Photon — stateless parser (simple functions)
# ---------------------------------------------------------------------------

class Photon:
    """
    Stateless parser — no state, just converts hex → PhotonPacket.

    This is the simplest entry point. No need to understand F3 framing.

    Usage:
        packet = Photon.parse("f302e6...")
        if packet.packet_type == PacketType.KILL:
            handle_kill(packet.killer, packet.victim)
    """

    @staticmethod
    def parse(hex_string: str) -> PhotonPacket:
        """
        Parse a single hex-encoded packet into a PhotonPacket.

        Never raises — on failure returns a packet with packet_type=UNKNOWN.
        """

        # Default packet (unknown)
        packet = PhotonPacket(raw_hex=hex_string)

        try:
            gpb = parse_gpbinary(hex_string)

            if not gpb:
                _debug("Photon.parse", f"parse_gpbinary returned falsy for hex len={len(hex_string) if hex_string else 0}")
                return packet

            # Keep raw for power users
            packet.raw = gpb

            # Game mode (direct or inferred from curScn)
            gm_str = gpb.get("gm")
            if gm_str:
                try:
                    packet.game_mode = GameMode(gm_str)
                except ValueError:
                    # Try case-insensitive
                    low = str(gm_str).lower()
                    for mode in GameMode:
                        if mode.value.lower() == low:
                            packet.game_mode = mode
                            break
                    if packet.game_mode is None:
                        _debug("Photon.parse", f"unrecognized gm value: {gm_str!r}")

            # curScn
            cur = gpb.get("curScn") or gpb.get("properties", {}).get("curScn")
            if cur:
                packet.cur_scn = str(cur)
                # Infer gm if not already set
                if not packet.game_mode:
                    inferred = GameMode.from_curscn(str(cur))
                    if inferred:
                        packet.game_mode = inferred

            # Party
            party_val = gpb.get("Party")
            if party_val is not None:
                packet.party = "party" if party_val else "solo"

            # Zone
            props = gpb.get("properties", {})
            if "stG" in props or "bz_phase_start" in props:
                packet.zone_time = props.get("bz_phase_start", props.get("stG"))
            if "bz_zone" in props:
                packet.zone_phase = str(props["bz_zone"])
            elif "bz_phase" in props:
                packet.zone_phase = str(props["bz_phase"])

            # Player name (for Join/alv)
            if "255" in props:
                packet.player_name = str(props["255"])

            # Killfeed
            kill_raw = gpb.get("killfeed")
            if kill_raw:
                parsed = parse_killfeed_text(kill_raw)

                if parsed["type"] == "kill":
                    packet.packet_type = PacketType.KILL
                    packet.is_kill = True
                    packet.killer = parsed["killer"]
                    packet.victim = parsed["victim"]
                    _debug("Photon.parse", f"KILL {packet.killer} -> {packet.victim}")

                elif parsed["type"] == "knock":
                    packet.packet_type = PacketType.KNOCK
                    packet.is_knock = True
                    packet.killer = parsed["killer"]
                    packet.victim = parsed["victim"]
                    _debug("Photon.parse", f"KNOCK {packet.killer} -> {packet.victim}")

            # Join / Leave / alv
            elif gpb.get("evName") == "Join":
                packet.packet_type = PacketType.JOIN
                packet.is_join = True
                _debug("Photon.parse", f"JOIN player={packet.player_name!r}")
                # player_name already set from properties

            elif gpb.get("evName") == "Leave":
                packet.packet_type = PacketType.LEAVE
                packet.is_leave = True
                _debug("Photon.parse", f"LEAVE player={packet.player_name!r}")

            elif "alv" in props:
                # alv is alive flag
                if props["alv"]:
                    packet.packet_type = PacketType.ALIVE
                    packet.is_alive = True
                else:
                    packet.packet_type = PacketType.ELIMINATED
                    packet.is_eliminated = True

                # alv packet may also have a name
                if not packet.player_name and "255" in props:
                    packet.player_name = str(props["255"])

                _debug("Photon.parse", f"ALV player={packet.player_name!r} alive={bool(props['alv'])}")

            elif gpb.get("opName") in ("CreateRoom", "JoinRoom", "JoinRandomRoom"):
                packet.packet_type = PacketType.JOIN_ROOM if "Join" in str(gpb.get("opName")) else PacketType.CREATE_ROOM

            elif gpb.get("opName") in ("Authenticate", "AuthenticateOnce"):
                packet.packet_type = PacketType.AUTHENTICATE

            elif "curScn" in props:
                packet.packet_type = PacketType.SCENE
                _debug("Photon.parse", f"SCENE curScn={props.get('curScn')!r}")

            elif "stG" in props or "bz_zone" in props:
                packet.packet_type = PacketType.ZONE

            # Human sentence (for logs)
            packet.sentence = human_sentence(gpb)

        except Exception as exc:
            # Never crash the caller — return unknown packet, but log why
            # in debug mode so failures aren't silently swallowed.
            _debug("Photon.parse", f"EXCEPTION while parsing hex (len={len(hex_string) if hex_string else 0}): {exc!r}")

        return packet

    @staticmethod
    def is_essential(hex_string: str) -> bool:
        """
        Quick check: should this packet be sent to the server?

        Checks only the first 3 bytes (type + code) — no full parse.
        Mirrors the userscript's isEssentialBytes for speed.
        """

        if not hex_string or len(hex_string) < 6:
            return False

        try:
            data = bytes.fromhex(hex_string)
            if len(data) < 3:
                return False

            msg_type = data[1]
            code = data[2]

            # Init packets are noise
            if msg_type in (6, 7):
                return False

            # OperationRequest / Response
            if msg_type in (2, 3):
                return code in {225, 226, 227, 230, 231}

            # EventData
            if msg_type == 4:
                if code in (200, 201, 202, 204, 206):
                    return False
                return code in {101, 253, 254, 255}

            # Keep Session (type 1)
            return msg_type in (1, 5)

        except Exception as exc:
            _debug("Photon.is_essential", f"EXCEPTION: {exc!r}")
            return False

    @staticmethod
    def human(hex_string: str) -> str | None:
        """One-liner: hex → human sentence or None if noise."""

        packet = Photon.parse(hex_string)
        return packet.sentence

    @staticmethod
    def filter_useful(hex_packets: list[str]) -> list["PhotonPacket"]:
        """Parse multiple packets and return only useful ones."""

        return [Photon.parse(h) for h in hex_packets if Photon.is_essential(h)]


# ---------------------------------------------------------------------------
# GameSession — stateful helper for win/defeat/players/zone
# ---------------------------------------------------------------------------

@dataclass
class GameSession:
    """
    Stateful game session — tracks players, kills, wins, zone.

    You feed it hex packets via `feed_hex()`, it updates state and
    fires callbacks for kills/wins. No need to understand alv or
    ActorNr — the session does it.

    Example:
        session = GameSession(self_name="true jew", game_mode=GameMode.ZW_LG)

        session.on_kill(lambda killer, victim: print(f"{killer} killed {victim}"))
        session.on_win(lambda gm: print(f"Win in {gm.human_name}"))
        session.on_eliminated(lambda name: print(f"{name} is eliminated"))

        for hex_packet in capture_file:
            session.feed_hex(hex_packet)

        print(f"Players left: {session.players_left}")

        # With context manager (auto-cleanup):
        with GameSession(self_name="true jew") as session:
            for hex_packet in capture_file:
                session.feed_hex(hex_packet)

    Thread-safe: can be used from async contexts with internal Lock.
    """

    # Config
    self_name: str | None = None
    game_mode: GameMode | None = None

    # State (read-only from outside, update via feed_hex)
    players: dict[int, dict] = field(default_factory=dict)  # actorNr -> {name, alive, actor_nr}
    self_alive: bool = True
    kills: int = 0
    deaths: int = 0
    zone_time: int | None = None
    zone_phase: str | None = None
    cur_scn: str | None = None

    # Internal
    _on_kill: list[Callable[[str, str], None]] = field(default_factory=list, repr=False)
    _on_win: list[Callable[[GameMode], None]] = field(default_factory=list, repr=False)
    _on_eliminated: list[Callable[[str], None]] = field(default_factory=list, repr=False)
    _pending_self_death: bool = False
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def __enter__(self) -> "GameSession":
        """Context manager entry — returns self."""

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit — clears callbacks (no hanging refs)."""

        # Clear callbacks to avoid leaks, but keep state for inspection
        self._on_kill.clear()
        self._on_win.clear()
        self._on_eliminated.clear()

    # -------------------------------------------------------------------
    # Convenience helpers
    # -------------------------------------------------------------------

    @property
    def players_left(self) -> int:
        """Number of players still in the room."""

        return len(self.players)

    @property
    def is_playing(self) -> bool:
        """Are we in a real game (not lobby)?"""

        return self.cur_scn != "MainLobby" and self.game_mode is not None

    def get_player_name(self, actor_nr: int) -> str | None:
        """Get player name by actor number."""

        player = self.players.get(int(actor_nr))
        return player["name"] if player else None

    def get_alive_players(self) -> list[str]:
        """Get list of alive player names."""

        return [info["name"] for info in self.players.values() if info.get("alive")]

    def _check_win(self) -> None:
        """Check win condition: self alive and no other alive players."""

        if not self.self_name or not self.is_playing:
            return

        # Get alive players excluding self
        alive_others = [
            info["name"]
            for info in self.players.values()
            if info.get("alive") and info["name"] != self.self_name
        ]

        # Win if self is alive and no other players remain
        if self.self_alive and not alive_others:
            for callback in self._on_win:
                try:
                    callback(self.game_mode or GameMode.UNKNOWN)
                except Exception:
                    pass

    # -------------------------------------------------------------------
    # Callback registration (with validation)
    # -------------------------------------------------------------------

    def on_kill(self, callback: Callable[[str, str], None]) -> None:
        """Register a callback for kills: `callback(killer, victim)`."""

        if not callable(callback):
            raise TypeError("Callback must be callable")
        self._on_kill.append(callback)

    def on_win(self, callback: Callable[[GameMode], None]) -> None:
        """Register a callback for wins: `callback(game_mode)`."""

        if not callable(callback):
            raise TypeError("Callback must be callable")
        self._on_win.append(callback)

    def on_eliminated(self, callback: Callable[[str], None]) -> None:
        """Register a callback for eliminations: `callback(player_name)`."""

        if not callable(callback):
            raise TypeError("Callback must be callable")
        self._on_eliminated.append(callback)

    # -------------------------------------------------------------------
    # Main entry point (thread-safe)
    # -------------------------------------------------------------------

    def feed_hex(self, hex_string: str) -> PhotonPacket | None:
        """
        Feed a raw hex packet — updates state, fires callbacks, returns packet.

        Thread-safe via internal Lock.
        This is the main method. Call it for every captured packet.
        """

        with self._lock:
            return self._feed_hex_inner(hex_string)

    def _feed_hex_inner(self, hex_string: str) -> PhotonPacket | None:
        """Inner implementation — assumes lock is held."""

        packet = Photon.parse(hex_string)

        # Update cur_scn / game_mode
        if packet.cur_scn:
            self.cur_scn = packet.cur_scn
            if not self.game_mode:
                inferred = GameMode.from_curscn(packet.cur_scn)
                if inferred:
                    self.game_mode = inferred

        if packet.game_mode:
            self.game_mode = packet.game_mode

        # Zone
        if packet.zone_time is not None:
            self.zone_time = packet.zone_time
        if packet.zone_phase is not None:
            self.zone_phase = packet.zone_phase

        # Extract actorNr from raw if available (for accurate player tracking)
        actor_nr = None
        if packet.raw:
            # Try to get actorNr from parsed gpb
            try:
                # gpb may have actorNr in params
                raw_params = packet.raw.get("params", {})
                if isinstance(raw_params, dict) and "ActorNr" in raw_params:
                    val = raw_params["ActorNr"]
                    if isinstance(val, dict) and "value" in val:
                        actor_nr = int(val["value"])
                    else:
                        actor_nr = int(val)
                elif packet.raw.get("actorNr") is not None:
                    actor_nr = int(packet.raw["actorNr"])
            except Exception:
                actor_nr = None

        # Join / Leave — track actual actor numbers when available
        if packet.is_join and packet.player_name:
            key = int(actor_nr) if actor_nr is not None else hash(packet.player_name) % 100000
            # Only add if not already present
            if key not in self.players:
                self.players[key] = {"name": packet.player_name, "alive": True, "actor_nr": actor_nr}

        if packet.is_leave and packet.player_name:
            # Remove by actorNr if known, else by name
            if actor_nr is not None and int(actor_nr) in self.players:
                self.players.pop(int(actor_nr), None)
            else:
                for key, info in list(self.players.items()):
                    if info["name"] == packet.player_name:
                        self.players.pop(key, None)
                        break

        # Eliminated / Alive
        if packet.is_eliminated and packet.player_name:
            for callback in self._on_eliminated:
                try:
                    callback(packet.player_name)
                except Exception:
                    pass

            # If it's other player, remove from players
            if packet.player_name != self.self_name:
                # Try by actorNr first
                if actor_nr is not None and int(actor_nr) in self.players:
                    self.players.pop(int(actor_nr), None)
                else:
                    for key, info in list(self.players.items()):
                        if info["name"] == packet.player_name:
                            self.players.pop(key, None)
                            break
            else:
                # FIXED: was self.selfAlive (typo), now correct snake_case
                self.self_alive = False

        if packet.is_alive and packet.player_name and packet.player_name != self.self_name:
            # Other player respawned / alive
            key = int(actor_nr) if actor_nr is not None else hash(packet.player_name) % 100000
            if key not in self.players:
                self.players[key] = {"name": packet.player_name, "alive": True, "actor_nr": actor_nr}

        # Kill
        if packet.is_kill and packet.killer and packet.victim:
            # Fire callbacks for any kill
            for callback in self._on_kill:
                try:
                    callback(packet.killer, packet.victim)
                except Exception:
                    pass

            # Count for self (use consistent snake_case)
            if packet.killer == self.self_name:
                self.kills += 1
            if packet.victim == self.self_name:
                self.deaths += 1
                self._pending_self_death = True

            # Update players — remove victim
            for key, info in list(self.players.items()):
                if info["name"] == packet.victim:
                    self.players.pop(key, None)
                    break

            # Win check via helper (clearer)
            self._check_win()

        return packet


# ---------------------------------------------------------------------------
# MatchTracker — the recommended entrypoint for real-time / live capture
# ---------------------------------------------------------------------------

class MatchTracker:
    """
    Real-time-friendly wrapper around GameSession.

    A bare GameSession has no concept of "match over" — kills, deaths,
    and players just keep accumulating forever, and win-detection breaks
    after the first match if you keep feeding it packets from match #2.
    MatchTracker fixes that: it owns a GameSession internally and swaps
    in a fresh one whenever it detects you've returned to MainLobby,
    so state never leaks across matches.

    This is the entrypoint to use for anything fed by a live source —
    a WebSocket handler, a queue consumer thread, a polling loop in
    match_checker.py, etc. Call `feed()` once per packet, in order.

    THREAD SAFETY: not thread-safe. feed() mutates internal state
    (players dict, kill/death counters, session identity) with no
    locking. If packets can arrive from more than one thread/task,
    route them through a single consumer (one queue.Queue + one
    dedicated thread, or one asyncio task) rather than calling feed()
    concurrently — concurrent calls can race on the players dict and
    produce false or missed win detections.

    MULTIPLE SIMULTANEOUS MATCHES: don't share one MatchTracker across
    players/connections — keep a dict of `player_id -> MatchTracker`
    so each match has fully isolated state.

    Example (real-time WebSocket handler):

        tracker = MatchTracker(self_name="true jew")
        tracker.on_kill(lambda k, v: print(f"{k} -> {v}"))
        tracker.on_win(lambda gm: print(f"WIN in {gm.human_name}"))

        async for hex_string in websocket:
            tracker.feed(hex_string)   # order matters — feed as it arrives
    """

    def __init__(self, self_name: str) -> None:
        self.self_name = self_name

        # Internal — recreated on every match reset. Access via the
        # public properties below (players_left, self_alive, etc.)
        # rather than reaching into `_session` directly.
        self._session: GameSession | None = None

        # Callbacks registered by the caller — re-attached to every
        # fresh GameSession created on reset, so callers only need to
        # call on_kill/on_win/on_eliminated once, not per-match.
        self._on_kill_callbacks: list[Callable[[str, str], None]] = []
        self._on_win_callbacks: list[Callable[[GameMode], None]] = []
        self._on_eliminated_callbacks: list[Callable[[str], None]] = []

        self._start_new_session()

    def _start_new_session(self) -> None:
        """Create a fresh GameSession and re-attach all registered callbacks."""

        _debug("MatchTracker", f"starting new session for self_name={self.self_name!r}")

        self._session = GameSession(self_name=self.self_name)

        for callback in self._on_kill_callbacks:
            self._session.on_kill(callback)
        for callback in self._on_win_callbacks:
            self._session.on_win(callback)
        for callback in self._on_eliminated_callbacks:
            self._session.on_eliminated(callback)

    def on_kill(self, callback: Callable[[str, str], None]) -> None:
        """Register a callback for kills: `callback(killer, victim)`.

        Persists across match resets — register once, fires for every match.
        """

        self._on_kill_callbacks.append(callback)
        self._session.on_kill(callback)

    def on_win(self, callback: Callable[[GameMode], None]) -> None:
        """Register a callback for wins: `callback(game_mode)`.

        Persists across match resets — register once, fires for every match.
        """

        self._on_win_callbacks.append(callback)
        self._session.on_win(callback)

    def on_eliminated(self, callback: Callable[[str], None]) -> None:
        """Register a callback for eliminations: `callback(player_name)`.

        Persists across match resets — register once, fires for every match.
        """

        self._on_eliminated_callbacks.append(callback)
        self._session.on_eliminated(callback)

    def feed(self, hex_string: str) -> PhotonPacket | None:
        """
        Feed one live packet. Call this for every packet, in order, as
        it arrives — do not call concurrently from multiple threads.

        Automatically resets to a fresh session when a return to
        MainLobby is detected, so kills/deaths/players never leak
        from one match into the next.
        """

        if self._session is None:
            self._start_new_session()

        packet = self._session.feed_hex(hex_string)

        # Match-over detection: we're back in MainLobby and not
        # currently tracked as "playing" -> reset for the next match.
        if packet.cur_scn == "MainLobby" and not self._session.is_playing:
            _debug("MatchTracker", "detected return to MainLobby, resetting session")
            self._start_new_session()

        return packet

    def reset(self) -> None:
        """Manually force a new match session (e.g. on explicit match-end signal)."""

        _debug("MatchTracker", "manual reset requested")
        self._start_new_session()

    # --- Read-only pass-through state, so callers don't need to reach
    # --- into `tracker._session` directly (which changes identity on reset).

    @property
    def players_left(self) -> int:
        """Number of players still in the room for the current match."""

        return self._session.players_left if self._session else 0

    @property
    def self_alive(self) -> bool:
        """Is the tracked player currently alive in the current match?"""

        return self._session.self_alive if self._session else True

    @property
    def kills(self) -> int:
        """Kill count for the current match."""

        return self._session.kills if self._session else 0

    @property
    def deaths(self) -> int:
        """Death count for the current match."""

        return self._session.deaths if self._session else 0

    @property
    def game_mode(self) -> GameMode | None:
        """Detected game mode for the current match."""

        return self._session.game_mode if self._session else None

    @property
    def is_playing(self) -> bool:
        """Are we in a real match right now (not lobby)?"""

        return self._session.is_playing if self._session else False