"""
50 — Full: collect → engine → processor → supabase → bot
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine
# from server.scripts.processor import process_batch  # async, needs supabase env
# 1. Userscript: Ctrl+Shift+C → play → Ctrl+Shift+S → logs/photon-capture-*.json
# 2. Replay:
p=Path("logs/photon-capture-123.json")
if p.exists():
    data=json.loads(p.read_text())
    engine=Engine()
    sid=data["meta"]["sessionId"]
    packets=[]
    for rec in data["packets"]:
        pkt=engine.feed(sid, rec["rawHex"])
        packets.append(pkt.to_dict())
    view=engine.get_session(sid)
    print(f"view {view.players_count} kills {len(view.recent_kills)}")
    print("Next: await process_batch(sid, packets, view) to write DB")
    # 3. Bot: GET /api/session/{sid}/state or /info /stats from DB
else:
    print("Create a capture first")
