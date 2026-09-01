"""
45 — Collect: processor dry-run (what would be written to bngc_game_events)
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

p=Path("logs/photon-capture-123.json")
engine=Engine()
sid="sess_proc"
if p.exists():
    data=json.loads(p.read_text())
    for rec in data["packets"]:
        engine.feed(sid, rec["rawHex"])
view=engine.get_session(sid)
print("me", view.me.to_dict() if view.me else None)
print("kills", [k.to_dict() for k in view.recent_kills])
print("Would sync bngc_users and insert killfeed/* events per packet")
# Call processor.process_batch(sid, [p.to_dict() for p in packets], view) to actually write
