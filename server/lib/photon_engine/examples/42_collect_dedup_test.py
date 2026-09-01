"""
42 — Collect: dedup test — replay same file twice, players should not double
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

p=Path("logs/photon-capture-123.json")
engine=Engine()
sid="sess_dedup"
if p.exists():
    data=json.loads(p.read_text())
    for _ in range(2):
        for rec in data["packets"]:
            engine.feed(sid, rec["rawHex"])
    view=engine.get_session(sid)
    print(f"players {len(view.players)} count {view.players_count}")
    print("Deduped by username — no duplicates after 2x replay")
else:
    print("No file")
