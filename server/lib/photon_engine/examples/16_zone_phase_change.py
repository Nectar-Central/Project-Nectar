"""
16 — Zone phase change detection
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine
from server.lib.photon_engine.models import SessionView

engine = Engine()
sid='sess_zonephase'
view=engine._sessions.setdefault(sid, SessionView(session_id=sid))
view.zone.phase='0'
print('before', view.zone.phase)
engine.feed(sid, 'f404ad01')
print('after', view.zone.phase)
