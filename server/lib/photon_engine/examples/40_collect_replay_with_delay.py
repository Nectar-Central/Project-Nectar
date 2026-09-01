"""
40 — Collect: replay with delay to simulate live timing (t field)
"""
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

p=Path("logs/photon-capture-123.json")
engine=Engine()
if p.exists():
    data=json.loads(p.read_text())
    prev=None
    for rec in data["packets"][:5]:
        if prev:
            delay=min(0.05, (rec["t"]-prev)/1000)
            # time.sleep(delay)  # uncomment for real delay
            print(f"delay {delay:.3f}s")
        engine.feed("sess_delay", rec["rawHex"])
        prev=rec["t"]
else:
    print("No file")
