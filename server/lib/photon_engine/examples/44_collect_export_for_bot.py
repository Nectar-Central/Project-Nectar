"""
44 — Collect: export view for bot (SessionView.to_dict → GET /api/session/{id}/state)
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine=Engine()
sid="sess_bot"
for hx in ["f302e601","f404ad01"]:
    engine.feed(sid, hx)
print(json.dumps(engine.get_session(sid).to_dict(), indent=2))
print("Bot fetches this via GET /api/session/<sid>/state")
