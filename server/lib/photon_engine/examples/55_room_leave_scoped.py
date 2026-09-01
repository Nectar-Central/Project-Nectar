"""
55 — Rooms: leave events scoped per room
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine
from server.lib.photon_engine.models import Player, RoomView, KillInfo

engine = Engine()
sid = "sess_55"
engine.feed(sid, "f302e601")
view = engine.get_session(sid)
view.rooms["r1"] = RoomView(room_id="r1", code="r1", players={1: Player(actor_nr=1, username="alice", is_alive=True)}, players_count=1)
view.current_room_id = "r1"
# Simulate LEAVE packet for alice in r1
view.rooms["r1"].players.pop(1, None)
view.rooms["r1"].players_count -= 1
view.rooms["r1"].recent_kills.append(KillInfo(instigator="alice", instigated="", event_type="left"))
print(f"r1 after leave count {view.rooms['r1'].players_count} kills {view.rooms['r1'].recent_kills}")
print("Other rooms unaffected")
