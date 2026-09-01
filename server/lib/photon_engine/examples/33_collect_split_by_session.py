"""
33 — Collect: split file that contains multiple sessions (if you appended)
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

p=Path("logs/photon-capture-123.json")
if p.exists():
    data=json.loads(p.read_text())
    # If file has mixed sessions, group by meta.sessionId
    print("session", data["meta"]["sessionId"], "packets", len(data["packets"]))
else:
    print("Single session per file by design — merge manually if needed")
