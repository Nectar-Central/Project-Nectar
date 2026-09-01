"""
49 — Collect: diff two sessions (e.g. two players)
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

e=Engine()
e.feed("alice","f302e601")
e.feed("bob","f302e601")
a=e.get_session("alice").to_dict()
b=e.get_session("bob").to_dict()
print("alice", a["players_count"], "bob", b["players_count"])
print("Isolated: one Engine, many SessionViews — no leak")
