"""
29 — Collect: zone timeline from file
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine=Engine()
sid="sess_zone_tl"
for hx in ["f404ad01","f404ad02"]:
    engine.feed(sid, hx)
view=engine.get_session(sid)
print(f"zone time={view.zone.time} phase={view.zone.phase}")
print("Replay a real file to see phase 0→1 transitions")
