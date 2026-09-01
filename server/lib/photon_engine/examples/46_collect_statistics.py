"""
46 — Collect: statistics from file (counts by packet_type)
"""
import sys, json, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

p=Path("logs/photon-capture-123.json")
if p.exists():
    engine=Engine()
    c=collections.Counter()
    for rec in json.loads(p.read_text())["packets"]:
        pkt=engine.feed("sess_stats", rec["rawHex"])
        c[pkt.packet_type.value]+=1
    print(c)
else:
    print("No file")
