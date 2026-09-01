"""
56 — Zone mm:ss timer (5:00 → 00:00 per phase)
zone.set_time(300, "2") then remaining_mmss(), is_at(), is_between()
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine = Engine()
sid = "sess_zone_mmss"
engine.feed(sid, "f302e601")
view = engine.get_session(sid)
room = view.current_room or list(view.rooms.values())[0]
room.zone.set_time(300, "0")  # 5:00 start of zw_lg
print("start", room.zone.remaining_mmss())  # 05:00
# Simulate 30s elapsed
room.zone.started_at -= 30
print("after 30s", room.zone.remaining_mmss())  # 04:30
print("is_at 4:30?", room.zone.is_at(4, 30))
print("is_between 5:00-4:00?", room.zone.is_between((5, 0), (4, 0)))
# In real match, engine sets time from stG/bz_phase_start automatically
