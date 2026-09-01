"""
51 — Rooms: Session → RoomView isolation
Each CreateRoom/JoinRoom creates a RoomView; events are scoped.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine
from server.lib.photon_engine.models import RoomView

engine = Engine()
sid = "sess_51"
# Feed dummy auth
engine.feed(sid, "f302e601")
view = engine.get_session(sid)
print(f"rooms after auth: {list(view.rooms.keys())}")  # lobby
# Simulate two rooms by direct insert (real RoomName comes from CreateRoom)
view.rooms["roomA"] = RoomView(room_id="roomA", code="roomA")
view.rooms["roomB"] = RoomView(room_id="roomB", code="roomB")
view.current_room_id = "roomA"
print(f"current {view.current_room_id} players {list(view.players.keys())}")
view.current_room_id = "roomB"
print(f"switched {view.current_room_id} players {list(view.players.keys())}")
print(view.to_dict()["rooms"].keys())
