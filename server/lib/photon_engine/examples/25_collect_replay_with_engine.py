"""
25 — Collect → Engine: feed in order, inspect view after each
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine = Engine()
sid="sess_replay25"
# Simulate 3 records from collect file
records=[{"rawHex":"f302e601"},{"rawHex":"f404ad01"},{"rawHex":"f4046501"}]
for rec in records:
    pkt=engine.feed(sid, rec["rawHex"])
    view=engine.get_session(sid)
    print(f"{pkt.packet_type.value} count={view.players_count} kills={len(view.recent_kills)}")
