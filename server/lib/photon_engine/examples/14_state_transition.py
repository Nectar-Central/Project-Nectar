"""
14 — State: LOBBY -> MATCH via curScn
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine
from server.lib.photon_engine.enums import State

engine = Engine()
sid='sess_state'
init = engine.get_session(sid).state if engine.get_session(sid) else State.LOBBY
print('init', init)
engine.feed(sid, 'f404ad01')
view=engine.get_session(sid)
for cur in ['MainLobby','Game']:
    view.cur_scn=cur
    print(cur, view.state)
