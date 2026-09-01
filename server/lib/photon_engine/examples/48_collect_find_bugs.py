"""
48 — Collect: find bugs — assert invariants after each feed
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine=Engine()
sid="sess_bug"
for hx in ["f302e601","f404ad01","f4046501"]:
    engine.feed(sid, hx)
    view=engine.get_session(sid)
    assert view.players_count >=0, "count negative"
    assert len(view.players) <= (view.max_players or 100), "too many players"
    dup=[p.username for p in view.players.values()]
    assert len(dup)==len(set(dup)), f"duplicate {dup}"
print("All invariants ok")
