"""
26 — Collect: filter essential before replay (mirrors userscript isEssentialBytes)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

def is_essential_hex(hx: str) -> bool:
    if len(hx)<6: return False
    b=bytes.fromhex(hx)
    t,c=b[1],b[2]
    if t in (6,7): return False
    if t in (2,3): return c in {225,226,227,230,231}
    if t==4: return c in {101,253,254,255}
    return t in (1,5)

engine=Engine()
for hx in ["f302e601","f3046502","f404ad01"]:
    if is_essential_hex(hx):
        engine.feed("sess_filt", hx)
        print("fed", hx[:8])
    else:
        print("skipped", hx[:8])
