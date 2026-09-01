"""
59 — Playtime per GM + total + lobby
view.playtime: total_seconds, per_gm{gm:secs}, lobby_seconds
Engine ticks on each feed (elapsed since last packet).
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine
from server.lib.photon_engine.enums import State, GameMode

engine = Engine()
sid = "sess_playtime"
engine.feed(sid, "f302e601")
view = engine.get_session(sid)
# Simulate 10s in lobby
time.sleep(0.1)
engine.feed(sid, "f302e601")
print("lobby", view.playtime.lobby_seconds, "total", view.playtime.total_seconds)
# Switch to match
view.current_room.state = State.MATCH
view.current_room.game_mode = GameMode.ZW_LG
view.playtime.last_state = State.MATCH.value
time.sleep(0.1)
engine.feed(sid, "f302e601")
print("per_gm", view.playtime.per_gm)
print(view.playtime.to_dict())
