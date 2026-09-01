"""
09 — SessionView.room & party
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine
from server.lib.photon_engine.models import SessionView

engine = Engine()
sid='sess_room'
view=engine._sessions.setdefault(sid, SessionView(session_id=sid))
print('room:', view.room.to_dict())
print('party:', view.party.to_dict())
