"""
19 — Win/defeat: query view.players_count, view.me.is_alive
Same logic processor uses.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine.models import Player, SessionView
from server.lib.photon_engine.enums import State, GameMode

view=SessionView(session_id='sess_win', state=State.MATCH, game_mode=GameMode.ZW_LG, max_players=12, players_count=1)
view.me=Player(actor_nr=1, username='jew6led', is_alive=True, is_self=True)
view.players[1]=view.me
if view.me.is_alive and view.players_count==1 and view.max_players:
    print('WIN')
if not view.me.is_alive:
    print('DEFEAT')
else:
    print('alive, no defeat')
