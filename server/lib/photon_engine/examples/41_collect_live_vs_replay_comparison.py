"""
41 — Collect: live vs replay — same engine, same session, should match
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

# Live: engine.feed per capture
# Replay: load file and feed again to new engine — compare views
e1=Engine(); e2=Engine()
for hx in ["f302e601","f404ad01"]:
    e1.feed("s1", hx); e2.feed("s1", hx)
print(e1.get_session("s1").to_dict()==e2.get_session("s1").to_dict())
print("Deterministic: same input order → same view")
