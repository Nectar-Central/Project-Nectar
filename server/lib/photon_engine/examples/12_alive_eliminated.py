"""
12 — ALIVE / ELIMINATED: view.players_count + is_alive transitions
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine = Engine()
sid='sess_alv'
for hx in ['f404ad01','f404ad02']:
    pkt=engine.feed(sid, hx)
    view=engine.get_session(sid)
    print(f'{pkt.packet_type.value} count={view.players_count} players={len(view.players)}')
