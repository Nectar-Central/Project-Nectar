"""
photon_engine — In-process, per-session engine for Photon packets.

This is the "pygame" of Photon: one Engine holds many SessionViews,
each keyed by session_id. Feed it hex, it mutates that session's
Player/Room/Party/Zone live. No DB writes, no win math — you decide.
"""

from .enums import GameMode, PacketType, State
from .models import Player, Room, Party, Zone, KillInfo, AbstractPacket, SessionView
from .engine import Engine

__all__ = [
    "Engine",
    "SessionView",
    "Player",
    "Room",
    "Party",
    "Zone",
    "KillInfo",
    "AbstractPacket",
    "GameMode",
    "PacketType",
    "State",
]
