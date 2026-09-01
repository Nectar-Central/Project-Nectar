"""
06 — SessionView.me: self player (created on first Create/Join)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine = Engine()
sid = 'sess_me'
pkt = engine.feed(sid, 'f302e601')
view = engine.get_session(sid)
print('me:', view.me)
print('tip: me is set only on is_first_join with playerProps[255]')
