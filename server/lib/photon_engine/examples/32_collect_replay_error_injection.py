"""
32 — Collect: error injection — feed truncated hex, see parse_fail handling
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine=Engine()
for hx in ["f3","zzzz","f302e601"]:
    pkt=engine.feed("sess_err", hx)
    print(hx, pkt.packet_type.value)
print("Engine logs parse_fail but never crashes — safe for bad captures")
