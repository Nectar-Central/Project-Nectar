import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

"""
realtime_feed.py — Real-time feeding: one hex at a time as it arrives.

This is what you do in POST /api/capture or a live WSS handler.
Each hex is fed in order per session_id, engine updates that session's
Player/Room/Zone live. No DB, you decide when to write.

Use case: Overlay that shows live players/zone for one player.
"""

from server.lib.photon_engine import Engine

# One engine holds many sessions (one per Discord user)
# In FastAPI: app.state.engine = Engine() (in-process, see server/main.py)
engine = Engine()

# Simulate two players feeding interleaved (no collision)
# In real life, these hex strings come from userscript records[].rawHex
fake_stream = [
    ("sess_mth_true_jew", "f302e601..."),  # CreateRoom with playerProps 255="true jew"
    ("sess_xyz_other",    "f302e601..."),  # Other player's CreateRoom
]

for session_id, hx in fake_stream:
    # Engine decrypts, creates Player/Room, mutates that session only
    pkt = engine.feed(session_id, hx)

    # pkt is AbstractPacket with real names:
    # pkt.packet == PacketType.CREATE_ROOM, pkt.player.username, pkt.room.code

    view = engine.get_session(session_id)
    print(f"[{session_id[:8]}] me={view.me.username if view.me else None} players={view.players_count}/{view.max_players} zone={view.zone.time}s")

# After a batch, you get the full session view for your own calculations:
view = engine.get_session("sess_mth_true_jew")
if view:
    # view.players is dict[int, Player] — live, with skin/ready/is_alive
    for actor_nr, player in view.players.items():
        print(f"  {actor_nr}: {player.username} skin={player.skin} alive={player.is_alive}")

    # You decide win: don't trust engine's old on_win, check yourself
    alive = [p for p in view.players.values() if p.is_alive]
    if len(alive) == 1 and view.me and view.me.is_alive:
        print(f"WIN candidate in {view.game_mode.human_name if view.game_mode else '?'}")

