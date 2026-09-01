"""
08 — SessionView.zone: stG / bz_phase_start
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine = Engine()
sid='sess_zone'
engine.feed(sid, 'f404ad01')
view=engine.get_session(sid)
print('zone:', view.zone.to_dict())
print('Use zone.time (stG) and zone.phase (bz_zone) for shrinking logic')
