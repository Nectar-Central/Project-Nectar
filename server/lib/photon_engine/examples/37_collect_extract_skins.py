"""
37 — Collect: extract skins/pickaxes from playerProps
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine=Engine()
sid="sess_skin"
# Real skins appear in first Create/Join playerProps skid/pid
# Replay file and inspect view.me
if Path("logs/photon-capture-123.json").exists():
    import json
    data=json.loads(Path("logs/photon-capture-123.json").read_text())
    for rec in data["packets"]:
        engine.feed(sid, rec["rawHex"])
    view=engine.get_session(sid)
    print(view.me.to_dict() if view.me else "no me yet")
else:
    print("Need real capture with CreateRoom to see skid")
