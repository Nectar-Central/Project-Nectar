"""
39 — Collect: benchmark feed speed
"""
import sys, time, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine=Engine()
recs=[{"rawHex":"f302e601"}]*1000
t0=time.time()
for r in recs:
    engine.feed("sess_bench", r["rawHex"])
print(f"1000 feeds in {time.time()-t0:.3f}s")
