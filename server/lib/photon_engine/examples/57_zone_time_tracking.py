"""
57 — Zone time tracking across phases
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine = Engine()
sid = "sess_zone_track"
# Phase 0 → 5:00, Phase 1 → 3:00, etc. Each bz_zone triggers set_time
view = engine.get_session(sid)
# Simulate two phases from capture file
for phase, secs in [("0", 300), ("1", 180)]:
    # normally engine receives packet with zone_time=secs, zone_phase=phase
    from server.lib.photon_engine.models import RoomView
    if not view.rooms:
        engine.feed(sid, "f302e601")
        view = engine.get_session(sid)
    room = view.current_room
    room.zone.set_time(secs, phase)
    print(f"phase {phase} mmss {room.zone.remaining_mmss()} initial {room.zone.initial_time}")
