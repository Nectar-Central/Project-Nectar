"""
60 — Playtime global + lobby separation
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine import Engine

engine = Engine()
sid = "sess_playtime2"
engine.feed(sid, "f302e601")
view = engine.get_session(sid)
# After session ends, view.playtime has:
# total = lobby + sum(per_gm.values())
# Use for /stats playtime_global / playtime_per_gm sync to bngc_users
print(view.playtime.to_dict())
# Processor syncs: supabase.table("bngc_users").update({"playtime_global":..., "playtime_per_gm": json.dumps(...)})
