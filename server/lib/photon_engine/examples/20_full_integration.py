"""
20 — Full integration: Engine + SessionView + to_dict + JSON/YAML logs
"""
import sys, json, yaml
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine
from server.lib.logger import log_engine_json

engine = Engine()
sid='sess_full'
for hx in ['f302e601','f404ad01']:
    pkt=engine.feed(sid, hx)
    view=engine.get_session(sid)
    log_engine_json('packet', sid[:8], type=pkt.packet_type.value, count=view.players_count)

print(json.dumps(engine.get_session(sid).to_dict(), indent=2))
print(yaml.dump(engine.get_session(sid).to_dict()))
