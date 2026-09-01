"""
05 — Engine: multiple sessions (like pygame, one Engine many SessionViews)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine = Engine()
for sid in ['sess_alice', 'sess_bob']:
    engine.feed(sid, 'f302e601')
print('sessions:', list(engine._sessions.keys()))
for sid, view in engine._sessions.items():
    print(sid, view.players_count)
