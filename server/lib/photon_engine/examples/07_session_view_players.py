"""
07 — SessionView.players: live dict actor_nr -> Player
Deduped by username; mutated on JOIN/ALIVE/KILL.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine
from server.lib.photon_engine.models import Player, SessionView

engine = Engine()
sid = 'sess_players'
view = engine._sessions.setdefault(sid, SessionView(session_id=sid))
view.players[1] = Player(actor_nr=1, username='alice', is_alive=True)
view.players[2] = Player(actor_nr=2, username='bob', is_alive=False)
print({k: v.username for k,v in view.players.items()})
print('count:', view.players_count)
