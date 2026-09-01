"""
36 — Collect: analyze game modes seen in file
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

p=Path("logs/photon-capture-123.json")
engine=Engine()
if p.exists():
    for rec in json.loads(p.read_text())["packets"]:
        pkt=engine.feed("sess_gm", rec["rawHex"])
        if pkt.game_mode:
            print(pkt.game_mode.value)
else:
    print("Feed a file with JoinRoom + curScn to see gm DF/zw_lg etc")
