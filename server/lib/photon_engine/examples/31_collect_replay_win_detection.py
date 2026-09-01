"""
31 — Collect: win detection from replay (players_count==1 + me alive)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine.models import Player, SessionView
from server.lib.photon_engine.enums import State, GameMode

view=SessionView(session_id="sess_win2", state=State.MATCH, game_mode=GameMode.ZW_LG, max_players=3, players_count=1)
view.me=Player(actor_nr=1, username="jew6led", is_alive=True, is_self=True)
view.players[1]=view.me
print("win?" , view.me.is_alive and view.players_count==1)
# Replay a collect file ending with win to test processor win branch
