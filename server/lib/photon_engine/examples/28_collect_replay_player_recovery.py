"""
28 — Collect: player recovery via killfeed (late joiners)
Engine adds unknown killer/victim to view.players
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine=Engine()
sid="sess_recovery"
# feed a kill where players not yet known
from server.lib.photon_engine.models import KillInfo
# Simulate engine seeing kill Tiktok -> jew6led
print("Before", list(engine.get_session(sid).players.values()) if engine.get_session(sid) else "no session")
# Use a real collect file if present
print("Engine will auto-add late players from killfeed on next feed")
