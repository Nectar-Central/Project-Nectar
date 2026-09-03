"""
enums.py — Real names for Photon magic numbers.

Instead of `properties['255']` or `opCode 227`, you get `GameMode.ZW_LG`
or `PacketType.CREATE_ROOM`. All values are documented with the JSON
packet that produces them, so you can grep captures.
"""

from __future__ import annotations

from enum import Enum


class GameMode(str, Enum):
    """
    Game mode as seen in `GameProperties.gm` or inferred from `curScn`.

    JSON packet that sets it:
        {
            "op": 227, "opName": "CreateRoom",
            "params": {
                "GameProperties": {"gm": "zw_lg"}  # <-- this field
            }
        }
        or
        {
            "evCode": 253, "evName": "PropertiesChanged",
            "properties": {"curScn": "MainWorld"}  # <-- inferred
        }

    Use `GameMode.from_curscn("MainWorld")` to infer when `gm` is missing.
    """

    DF = "DF"               # Free Mode / MainWorld
    ZW = "zw"               # Zone Wars
    ZW_LG = "zw_lg"          # Lategame Zone Wars
    WW = "WW"               # Wild West
    WWZW = "wwzw"            # Duo Wild West (zone wars)
    BF = "BF"               # Boxfights
    BF2V2 = "BF2V2"          # Boxfights 2v2
    VA = "VA"               # V-Arena (1v1)
    RVB = "tdm_bvr"         # Red vs Blue (TDM, 2 teams up to 8, first to 15 kills)
    R1V1 = "R_1V1"          # Realistic 1v1
    UNKNOWN = "unknown"

    @classmethod
    def from_gm(cls, gm_str: str | None) -> "GameMode | None":
        """
        Map a raw `gm` string to GameMode.

        Exact match first ("zw_lg"), then case-insensitive ("ZW_LG"), then
        longest-prefix ("zw_lg_duo" -> ZW_LG, "df_duo" -> DF). Longest-first so
        BF2V2 wins over BF. UNKNOWN is never prefix-matched.
        Returns None when nothing matches (caller logs raw string for discovery).
        """

        if not gm_str:
            return None
        try:
            return cls(gm_str)
        except ValueError:
            pass
        low = str(gm_str).lower()
        for m in cls:
            if m is cls.UNKNOWN:
                continue
            if m.value.lower() == low:
                return m
        for m in sorted((m for m in cls if m is not cls.UNKNOWN), key=lambda m: -len(m.value)):
            if low.startswith(m.value.lower()):
                return m
        return None

    @classmethod
    def from_curscn(cls, curscn: str | None) -> "GameMode | None":
        """
        Infer GameMode from a curScn string.

        Example JSON that triggers this:
            {"properties": {"curScn": "WildWest_ZoneWars"}}

        Use case:
            packet = Photon.parse(hex)
            if not packet.game_mode and packet.cur_scn:
                packet.game_mode = GameMode.from_curscn(packet.cur_scn)
        """

        if not curscn:
            return None

        low = str(curscn).lower()

        if "lategame" in low or "zw_lg" in low:
            return cls.ZW_LG
        if "wildwest" in low or "wild west" in low or "wwzw" in low:
            return cls.WWZW
        if "zonewars" in low or "zone wars" in low:
            return cls.ZW
        if "boxfights" in low and "2v2" in low:
            return cls.BF2V2
        if "boxfights" in low:
            return cls.BF
        if "mainworld" in low:
            return cls.DF
        if "wild" in low:
            return cls.WW
        if "free" in low:
            return cls.DF

        return None

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
            GameMode.VA: "V-Arena",
            GameMode.RVB: "Red vs Blue",
            GameMode.R1V1: "Realistic 1v1",
        }
        return names.get(self, self.value)


class PacketType(str, Enum):
    """
    High-level packet categories — hides opCode/evCode numbers.

    JSON packet examples:
        AUTHENTICATE: {"op": 230, "opName": "Authenticate"}
        CREATE_ROOM:  {"op": 227, "opName": "CreateRoom", "params": {"RoomName": "ABC123"}}
        JOIN:         {"evCode": 255, "evName": "Join", "properties": {"255": "Alice"}}
        LEAVE:        {"evCode": 254, "evName": "Leave", "params": {"ActorNr": 3}}
        KILL:         {"evCode": 101, "evName": "Killfeed", "killfeed": "Alice killed Bob"}
        ELIMINATED:   {"evCode": 253, "properties": {"alv": false, "255": "Bob"}}
        ZONE:         {"evCode": 253, "properties": {"stG": 30, "bz_zone": "2"}}
        SCENE:        {"evCode": 253, "properties": {"curScn": "MainLobby"}}
    """

    AUTHENTICATE = "authenticate"      # op 230/231
    CREATE_ROOM = "create_room"        # op 227
    JOIN_ROOM = "join_room"            # op 226
    JOIN_RANDOM_ROOM = "join_random_room"  # op 225
    JOIN = "join"                      # event 255
    LEAVE = "leave"                    # event 254
    ALIVE = "alive"                    # alv=true
    ELIMINATED = "eliminated"          # alv=false
    KILL = "kill"                      # killfeed killed
    KNOCK = "knock"                    # killfeed knocked
    ZONE = "zone"                      # stG / bz_phase
    SCENE = "scene"                    # curScn
    UNKNOWN = "unknown"


class State(str, Enum):
    """Session state — lobby vs match."""

    LOBBY = "lobby"   # curScn == "MainLobby" or Authenticate just happened
    MATCH = "match"   # curScn != "MainLobby" and game_mode is known
