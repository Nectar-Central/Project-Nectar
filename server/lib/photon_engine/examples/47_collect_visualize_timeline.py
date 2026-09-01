"""
47 — Collect: timeline — sort by t, print events in order
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

p=Path("logs/photon-capture-123.json")
if p.exists():
    data=json.loads(p.read_text())
    data["packets"].sort(key=lambda r: r["t"])
    engine=Engine()
    for rec in data["packets"][:10]:
        pkt=engine.feed("sess_tl", rec["rawHex"])
        print(f"{rec['t']} {pkt.packet_type.value} {pkt.killer or ''}->{pkt.victim or ''}")
else:
    print("No file")
