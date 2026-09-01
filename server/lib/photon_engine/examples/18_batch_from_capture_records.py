"""
18 — Batch feed like POST /api/capture does
records come from userscript: [{rawHex,seq,dir,t}]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine = Engine()
sid='sess_batch'
records = [{'rawHex':'f302e601','seq':1,'dir':'out'},{'rawHex':'f404ad01','seq':2,'dir':'in'}]
packets=[]
for rec in records:
    pkt=engine.feed(sid, rec['rawHex'])
    d=pkt.to_dict()
    d['seq']=rec['seq']
    packets.append(d)
print('packets', packets)
print('view', engine.get_session(sid).to_dict())
