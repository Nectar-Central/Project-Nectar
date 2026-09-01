"""
54 — Rooms: query via bot GET /api/session/{id}/state
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine = Engine()
sid = "sess_54"
for hx in ["f302e601", "f404ad01"]:
    engine.feed(sid, hx)
# Simulate room creation as engine would on CreateRoom
from server.lib.photon_engine.models import RoomView
view = engine.get_session(sid)
view.rooms["abc123"] = RoomView(room_id="abc123", code="abc123")
view.current_room_id = "abc123"
print(json.dumps(view.to_dict(), indent=2))
print("Bot: GET /api/session/abc123/state → rooms, current_room_id, players (current room)")
print("Bot commands: /currentmatch shows Room, /rooms lists, /players lists current room")
