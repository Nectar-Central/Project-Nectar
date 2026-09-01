import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

"""
track_kills.py — Track kills/deaths yourself from engine's abstract data.

Engine does NOT increment kills/wins — it just gives you
pkt.kill.instigator/instigated and view.me. You do the math.

Use case: Your own DB writes, with verification.
"""

from server.lib.photon_engine import Engine

engine = Engine()
session_id = "sess_test_kills"

# Simulate first join to set me
engine.feed(session_id, "f302e3...")  # CreateRoom with playerProps 255="true jew" (first join only)
view = engine.get_session(session_id)
print(f"Me: {view.me.username if view.me else 'not set yet'}")

# Simulate killfeed packets as they arrive
kill_hexes = [
    "f3046502...",  # will be parsed as KILL true jew -> Bob
]

my_kills = 0
my_deaths = 0

for hx in kill_hexes:
    pkt = engine.feed(session_id, hx)
    if not pkt:
        continue

    # Only KILL packets have kill info (engine's AbstractPacket uses is_kill/killer/victim)
    # PhotonPacket: is_kill, killer, victim
    # AbstractPacket: kill.instigator/instigated
    is_kill = getattr(pkt, "is_kill", False) or (getattr(pkt, "packet_type", None) and str(pkt.packet_type) == "kill")
    killer = getattr(pkt, "killer", None) or (getattr(pkt, "kill", None).instigator if getattr(pkt, "kill", None) else None)
    victim = getattr(pkt, "victim", None) or (getattr(pkt, "kill", None).instigated if getattr(pkt, "kill", None) else None)

    if is_kill and killer and victim:
        print(f"{killer} did an elimination -> {victim}")

        # Your verification (you own this):
        me = engine.get_session(session_id).me
        if me and killer == me.username:
            my_kills += 1
            print(f"  -> my kill #{my_kills} (you will write to DB here)")
            # supabase.table("bngc_users").update({"kills": my_kills})...

        if me and victim == me.username:
            my_deaths += 1
            print(f"  -> my death #{my_deaths}")

        # For other vs other, just update players list via alv, not here
        # Late joiners: if instigator not in view.players, engine already added with actor_nr=-1

print(f"Final: kills={my_kills} deaths={my_deaths}")
print(f"Players in view: {[(p.username, p.is_alive) for p in engine.get_session(session_id).players.values()]}")

