"""
24 — Collect: replay via Ctrl+Shift+R (userscript posts to /api/capture)
Or local python replay:
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

p = Path("C:\\Users\\assko\\Desktop\\websockets-capture\\server\\logs\\testdata.json")
if not p.exists():
    print("No capture file — create one via Ctrl+Shift+C then S")
    raise SystemExit
data = json.loads(p.read_text())
engine = Engine()
sid = "sess_replay24"
for rec in data["packets"]:
    engine.feed(sid, rec["rawHex"])
print(engine.get_session(sid).to_dict())
