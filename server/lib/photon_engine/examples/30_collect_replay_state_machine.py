"""
30 — Collect: state machine LOBBY→MATCH from curScn in file
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine=Engine()
sid="sess_state_tl"
# curScn drives state; feed collect file that contains curScn changes
print(engine.get_session(sid).state if engine.get_session(sid) else "LOBBY")
engine.feed(sid,"f404ad01")
print(engine.get_session(sid).state)
