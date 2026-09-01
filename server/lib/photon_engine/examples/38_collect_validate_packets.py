"""
38 — Collect: validate packets (is_essential, parse success)
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

p=Path("logs/photon-capture-123.json")
if p.exists():
    data=json.loads(p.read_text())
    engine=Engine()
    ok=0
    for rec in data["packets"]:
        pkt=engine.feed("sess_valid", rec["rawHex"])
        if pkt.packet_type.value!="unknown":
            ok+=1
    print(f"{ok}/{len(data['packets'])} parsed as known types")
else:
    print("No file")
