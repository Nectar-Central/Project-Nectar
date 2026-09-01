"""
58 — Teams in duo (WWZW / BF2V2)
Party code → Team. Members auto-added.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine
from server.lib.photon_engine.models import Player, RoomView, Team
from server.lib.photon_engine.enums import GameMode

engine = Engine()
sid = "sess_teams"
engine.feed(sid, "f302e601")
view = engine.get_session(sid)
room = view.current_room
room.game_mode = GameMode.WWZW
room.party.is_party = True
room.party.code = "party_abc"
room.teams["party_abc"] = Team(team_id="party_abc", members=["jew6led", "teammate1"], is_self_team=True)
print(room.teams["party_abc"].to_dict())
print("my team", room.get_team("jew6led"))
# In real engine, feed with Party=true auto-creates team
