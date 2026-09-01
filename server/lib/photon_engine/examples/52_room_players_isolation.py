"""
52 — Rooms: players isolated per room, with skin/game_id filled
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine
from server.lib.photon_engine.models import Player, RoomView

engine = Engine()
sid = "sess_52"
engine.feed(sid, "f302e601")
view = engine.get_session(sid)
view.rooms["r1"] = RoomView(room_id="r1", code="r1")
view.rooms["r2"] = RoomView(room_id="r2", code="r2")
view.current_room_id = "r1"
view.rooms["r1"].players[1] = Player(actor_nr=1, username="alice", skin="cct_1", game_id="id1", is_alive=True)
view.current_room_id = "r2"
view.rooms["r2"].players[2] = Player(actor_nr=2, username="bob", skin="cct_2", game_id="id2", is_alive=True)
print("r1", [p.username for p in view.rooms["r1"].players.values()])
print("r2", [p.username for p in view.rooms["r2"].players.values()])
print("current proxy", [p.username for p in view.players.values()])
