"""
27 — Collect: analyze killfeed from file
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

p=Path("logs/photon-capture-123.json")
if p.exists():
    data=json.loads(p.read_text())
    engine=Engine()
    for rec in data["packets"]:
        engine.feed("sess_kf", rec["rawHex"])
    view=engine.get_session("sess_kf")
    print("kills:", [k.to_dict() for k in view.recent_kills])
else:
    print("No capture — demo")
    engine=Engine()
    engine.feed("sess_kf","f4046501")
    print(engine.get_session("sess_kf").recent_kills)
