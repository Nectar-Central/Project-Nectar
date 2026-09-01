"""
43 — Collect: partial replay — first N packets only (e.g. lobby only)
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

p=Path("logs/photon-capture-123.json")
if p.exists():
    data=json.loads(p.read_text())
    for n in [5,10,len(data["packets"])]:
        e=Engine()
        for rec in data["packets"][:n]:
            e.feed("s", rec["rawHex"])
        v=e.get_session("s")
        print(f"n={n} state={v.state.value} count={v.players_count}")
else:
    print("No file")
