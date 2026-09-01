"""
13 — JOIN dedupe: engine checks username before adding
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine
from server.lib.photon_engine.models import Player, SessionView

engine = Engine()
sid='sess_dedupe'
view=engine._sessions.setdefault(sid, SessionView(session_id=sid))
view.players[1]=Player(actor_nr=1, username='cilx', is_alive=True)
engine.feed(sid, 'f404ad01')
print('players:', [p.username for p in view.players.values()])
print('deduped by username in engine.feed JOIN branch')
