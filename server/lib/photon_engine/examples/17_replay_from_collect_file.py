"""
17 — Replay from collect-mode JSON file
Userscript saves {meta, packets:[{rawHex,seq,dir}]}
Feed each rawHex to engine in order.
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

capture_file = Path('logs/photon-capture-123.json')
if capture_file.exists():
    data = json.loads(capture_file.read_text())
    engine = Engine()
    sid='sess_replay'
    for rec in data['packets']:
        pkt=engine.feed(sid, rec['rawHex'])
        if pkt.is_kill:
            print(f"{pkt.killer} -> {pkt.victim}")
    print('replay done', engine.get_session(sid).to_dict())
else:
    print('no capture file, place photon-capture-*.json in logs/')
    engine=Engine()
    for hx in ['f302e601','f404ad01']:
        print(engine.feed('sess_demo', hx).to_dict())
