"""
53 — Rooms: killfeed scoped per room
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine
from server.lib.photon_engine.models import KillInfo, RoomView

engine = Engine()
sid = "sess_53"
engine.feed(sid, "f302e601")
view = engine.get_session(sid)
view.rooms["arena1"] = RoomView(room_id="arena1", code="arena1")
view.current_room_id = "arena1"
view.rooms["arena1"].recent_kills.append(KillInfo(instigator="alice", instigated="bob", event_type="killed"))
print("arena1 kills", view.rooms["arena1"].recent_kills)
print("proxy kills", view.recent_kills)
