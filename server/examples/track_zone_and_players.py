import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

"""
track_zone_and_players.py — Live zone and players list for overlay.

Shows how Player.skin/ready/is_alive and Zone.time/phase mutate live
per session, and how players_count is seeded from max_players.

Use case: Discord Rich Presence or in-game overlay.
"""

from server.lib.photon_engine import Engine
from pathlib import Path
import json

# Use a real capture file to demo
cap_file = list(Path("photon-sniffer/captures").glob("*.jsonl"))[0]
print(f"Replaying {cap_file.name}...")

engine = Engine()
sid = "sess_demo_zone"

for line in open(cap_file, encoding="utf-8"):
    try:
        rec = json.loads(line)
        hx = rec.get("rawHex")
        if not hx:
            continue
        pkt = engine.feed(sid, hx)

        # Log only when zone or players changed (you can watch delta)
        if pkt.zone and pkt.zone.time is not None:
            print(f"Zone tick: time={pkt.zone.time}s phase={pkt.zone.phase}")

        if pkt.packet.value in ("join", "leave", "eliminated", "alive"):
            view = engine.get_session(sid)
            if view:
                print(f"Players: {view.players_count}/{view.max_players} curScn={view.cur_scn}")
                # Players dict has live skin/ready
                for p in view.players.values():
                    print(f"  - {p.username} skin={p.skin} ready={p.is_ready} alive={p.is_alive} actor={p.actor_nr}")

    except Exception as e:
        print(f"skip line: {e}")

view = engine.get_session(sid)
if view:
    print("\nFinal view:")
    print(f"  me: {view.me.username if view.me else None} skin={view.me.skin if view.me else None}")
    print(f"  game_mode: {view.game_mode} ({view.game_mode.human_name if view.game_mode else '?'})")
    print(f"  cur_scn: {view.cur_scn} state={view.state}")
    print(f"  zone: time={view.zone.time} phase={view.zone.phase}")
    print(f"  players: {len(view.players)} dict, count int={view.players_count}")
    # Note: for JoinRandom, players dict may have -1 entries (late-found via killfeed)
    # until their PropertiesChanged with real actorNr promotes -1 -> real
    for actor, p in view.players.items():
        print(f"    {actor}: {p.username} alive={p.is_alive} skin={p.skin}")

