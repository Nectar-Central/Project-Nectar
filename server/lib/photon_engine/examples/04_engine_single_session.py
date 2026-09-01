"""
04 — Engine: single session feed
SessionView is mutated in place; no DB.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine = Engine()
sid = 'sess_demo1'
for hx in ['f302e601', 'f404ad01']:
    pkt = engine.feed(sid, hx)
    print(f'fed {hx[:8]} -> {pkt.packet_type.value}')
view = engine.get_session(sid)
print('view:', view.to_dict())
