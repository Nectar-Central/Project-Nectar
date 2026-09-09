"""
backfill_play_points.py — one-shot fill of play_points for existing users.

  python -m server.scripts.backfill_play_points          # dry run (default)
  python -m server.scripts.backfill_play_points --apply  # write to Supabase

Formula per user (mirrors the live ledger in processor.py):
  kills*8 + wins*50 + knocks*2 + full_hours*20,
where knocks = killfeed rows with kill_type "knocked" (plus legacy "knock"
rows) attributed to the user's current game_username.

Write rule is max(existing, computed): never takes points away, and the
hours checkpoint only advances — re-runs are safe. Requires SUPABASE_URL +
SUPABASE_SERVICE_ROLE_KEY and the add_play_points_v221.sql migration.
"""

from __future__ import annotations

import sys

from server.lib.ranked import PLAY_KNOCK, PLAY_KILL, PLAY_WIN, PLAY_HOUR, play_points_for
from server.lib.supabase_client import supabase


def _count_knocks(discord_id: str, game_username: str | None) -> int:
    """Knock events attributed to this user. 0 on any error."""
    try:
        if not supabase:
            return 0
        res = supabase.table("bngc_game_events").select("event_type,event_data").eq(
            "discord_id", discord_id).in_("event_type", ["killfeed", "knock"]).limit(5000).execute()
        n = 0
        for row in (res.data or []):
            try:
                gd = (row or {}).get("event_data") or {}
                et = (row or {}).get("event_type")
                if et == "knock":
                    n += 1
                elif et == "killfeed" and str(gd.get("kill_type") or "") == "knocked":
                    if not game_username or str(gd.get("instigator") or "") == str(game_username):
                        n += 1
            except Exception:
                continue
        return n
    except Exception as exc:
        print(f"[backfill] knocks failed {discord_id[:6]}: {exc}")
        return 0


def main(apply: bool = False) -> int:
    if not supabase:
        print("[backfill] Supabase not configured (SUPABASE_URL/SERVICE_ROLE_KEY). Abort.")
        return 1
    offset = 0
    page = 200
    updated = skipped = 0
    while True:
        try:
            res = supabase.table("bngc_users").select(
                "discord_id,discord_username,game_username,kills,wins,"
                "playtime_global,play_points,play_points_hours_paid"
            ).range(offset, offset + page - 1).execute()
        except Exception as exc:
            print(f"[backfill] users page failed @{offset}: {exc}")
            return 1
        rows = res.data or []
        if not rows:
            break
        for u in rows:
            try:
                did = str(u.get("discord_id"))
                kills = int(u.get("kills") or 0)
                wins = int(u.get("wins") or 0)
                playtime = int(u.get("playtime_global") or 0)
                knocks = _count_knocks(did, u.get("game_username"))
                computed = play_points_for(kills, wins, knocks, playtime)
                hours = playtime // 3600
                final = max(int(u.get("play_points") or 0), computed)
                paid = max(int(u.get("play_points_hours_paid") or 0), hours)
                dirty = (final != int(u.get("play_points") or 0)
                         or paid != int(u.get("play_points_hours_paid") or 0))
                tag = u.get("discord_username") or u.get("game_username") or did[:6]
                print(f"[backfill] {tag}: K{kills} W{wins} N{knocks} H{hours} "
                      f"-> {computed} (final {final}, paid {paid}){' DRY' if not apply else ''}")
                if apply and dirty:
                    supabase.table("bngc_users").update(
                        {"play_points": final, "play_points_hours_paid": paid}
                    ).eq("discord_id", did).execute()
                    updated += 1
                elif not dirty:
                    skipped += 1
            except Exception as exc:
                print(f"[backfill] row failed: {exc}")
        offset += page
    print(f"[backfill] done: updated={updated} skipped={skipped} apply={apply}")
    print(f"[backfill] rates: kill={PLAY_KILL} win={PLAY_WIN} knock={PLAY_KNOCK} hour={PLAY_HOUR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
