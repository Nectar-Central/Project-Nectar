import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

"""
full_session.py — Full game from lobby to win, using engine + your DB logic.

This is the "do the job" script you asked for: engine gives you data,
you verify, then you write to DB. No engine-side kills/wins.

It replays a real sess_mth capture and shows where YOU would
insert supabase writes.
"""

import json
import time
from pathlib import Path

from server.lib.photon_engine import Engine
from server.lib.photon_engine.enums import GameMode

# Pick a real session file that has a full game (with kills)
# Use app/logs/sessions/*.json (now valid JSON arrays, not JSONL)
sess_file = sorted(Path("app/logs/sessions").glob("*.json"), key=lambda p: p.stat().st_size, reverse=True)[0]
print(f"Replaying session file: {sess_file.name} ({sess_file.stat().st_size} bytes)")

data = json.loads(sess_file.read_text(encoding="utf-8"))
print(f"  {len(data)} packets in file")

engine = Engine()
sid = "sess_replay_full"

# Your counters (you own, not engine)
my_kills = 0
my_deaths = 0
my_wins = 0

for entry in data:
    # Reconstruct hex if available, else skip
    # In the new per-session JSON, we store abstract, not rawHex, so for demo we need rawHex from captures
    # Fallback: use the entry's packet type to simulate
    # For real replay, use photon-sniffer/captures/*.jsonl which has rawHex
    pass

# Better: replay from raw captures that still have rawHex
cap_file = list(Path("photon-sniffer/captures").glob("*.jsonl"))[1]
print(f"\nReplaying raw captures from {cap_file.name} for full logic demo...")

engine2 = Engine()
sid2 = "sess_full_demo"

# Simulate first join to set me (only first Create/Join counts)
# In real use, this comes from the first POST /api/capture's first record's playerProps
# Here we just feed in order

for line in open(cap_file, encoding="utf-8"):
    rec = json.loads(line)
    hx = rec.get("rawHex")
    if not hx or len(hx) < 10:
        continue

    pkt = engine2.feed(sid2, hx)
    view = engine2.get_session(sid2)

    # Your verification (replace with supabase writes):
    if pkt and pkt.packet.value == "kill" and pkt.kill:
        # pkt.kill has instigator/instigated as Player objects via engine's AbstractPacket
        # In this engine version, pkt.kill is KillInfo with .instigator/.instigated as str
        killer = pkt.kill.instigator if hasattr(pkt.kill, 'instigator') else pkt.killer
        victim = pkt.kill.instigated if hasattr(pkt.kill, 'instigated') else pkt.victim

        if view and view.me and killer == view.me.username:
            my_kills += 1
            print(f"[{view.players_count} players] {killer} did an elimination -> {victim} (my kill #{my_kills})")
            # Here you would: supabase.table("bngc_users").update({"kills": my_kills})...

        # Track deaths similarly
        if view and view.me and victim == view.me.username:
            my_deaths += 1
            print(f"  -> I died to {killer} (death #{my_deaths})")

    # Your win check (don't trust engine's old on_win, do it yourself with real count)
    if view and view.state.value == "match" and view.players_count == 1 and view.me and view.me.is_alive:
        # Extra guard you wanted: ensure max_players was seeded (not just Joins)
        if view.max_players is not None:
            my_wins += 1
            print(f"WIN candidate in {view.game_mode.human_name if view.game_mode else '?'} with {view.players_count}/{view.max_players} left")
            # Here you would: supabase.table("bngc_users").update({"wins": my_wins})...

view = engine2.get_session(sid2)
if view:
    print("\n--- Final SessionView ---")
    print(f"me: {view.me.username if view.me else None} game_id={view.me.game_id if view.me else None} skin={view.me.skin if view.me else None}")
    print(f"players: {view.players_count}/{view.max_players} (dict size {len(view.players)})")
    print(f"zone: time={view.zone.time} phase={view.zone.phase}")
    print(f"cur_scn: {view.cur_scn} state={view.state}")
    print(f"my totals: kills={my_kills} deaths={my_deaths} wins={my_wins}")
    print(f"players detail:")
    for p in view.players.values():
        print(f"  {p.username} actor={p.actor_nr} alive={p.is_alive} skin={p.skin}")

