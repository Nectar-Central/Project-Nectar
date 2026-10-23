"""
guild_stats.py — fold a finished tournament into per-server player totals.

Why this exists
---------------
`bngc_users` holds one global row per human (login, in-game name, lock state).
That row must not become a scoreboard, because the same person can play in two
different Discord servers and each server wants its own board.

So the per-server numbers live in `nectar_guild_players`, and this module is
the only writer that moves tournament results into them.

Precision rules
---------------
  * Idempotent by construction. Every (tourney, player) pair is written to
    `nectar_guild_stat_awards` first. That table's primary key is the receipt:
    an insert that does nothing means "already banked", so a retry — or a
    double click on Close — can never add the same points twice.
  * Additive, never absolute. Kills/wins/matches/points are incremented by the
    tournament's own totals, so manual point edits made before close are
    included and later edits are not silently overwritten.
  * PR is absolute. A rating is a snapshot, not a delta.
  * Self-healing. If a player row fails to update, its award row is removed
    again, so the next close attempt redoes exactly that player and nothing
    else.
  * Best-effort. A failure here is logged and returns 0. Closing a tournament
    must never fail because a stats write failed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .supabase_client import supabase, db_io
from .logger import log_err, log_sys


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Cached answer to "has add_multi_guild_v264_stat_awards.sql been applied?".
# None = not checked yet. A missing table is a deployment mistake, not a
# transient failure, so it is worth one clear log line instead of N cryptic
# per-player errors and a leaderboard that quietly never updates.
_schema_ready: bool | None = None


async def _check_schema() -> bool:
    global _schema_ready
    if _schema_ready is not None:
        return _schema_ready
    try:
        await db_io(
            lambda: supabase.table("nectar_guild_stat_awards")
            .select("tourney_id").limit(1).execute()
        )
        _schema_ready = True
    except Exception as e:
        _schema_ready = False
        log_err("GUILDSTATS the receipts table is missing — tournament results "
                "cannot be saved to the leaderboard. Run "
                "supabase/add_multi_guild_v264_stat_awards.sql in the Supabase "
                f"SQL editor. ({e})")
    return _schema_ready


async def _claim_award(guild_id: int, tourney_id: int, discord_id: str,
                       kills: int, wins: int, matches: int, points: int,
                       pr: float | None) -> bool:
    """Insert the receipt for this player. True only if it is new.

    `on_conflict do nothing` is the whole safety mechanism: the primary key
    (tourney_id, discord_id) makes a repeat call a no-op that returns no rows.
    """
    row = {
        "guild_id": int(guild_id),
        "tourney_id": int(tourney_id),
        "discord_id": str(discord_id),
        "kills": max(0, int(kills)),
        "wins": max(0, int(wins)),
        "matches": max(0, int(matches)),
        "points": max(0, int(points)),
        "pr": float(pr) if pr is not None else None,
    }
    res = await db_io(
        lambda: supabase.table("nectar_guild_stat_awards")
        .upsert(row, on_conflict="tourney_id,discord_id", ignore_duplicates=True)
        .execute()
    )
    # ignore_duplicates makes a repeat insert return an empty data list.
    return bool(res.data)


async def _release_award(tourney_id: int, discord_id: str) -> None:
    """Drop a receipt whose player update failed, so a retry can redo it."""
    try:
        await db_io(
            lambda: supabase.table("nectar_guild_stat_awards")
            .delete()
            .eq("tourney_id", int(tourney_id))
            .eq("discord_id", str(discord_id))
            .execute()
        )
    except Exception as e:
        log_err(f"GUILDSTATS release {tourney_id}/{discord_id}: {e}")


async def _bump_player(guild_id: int, discord_id: str, kills: int, wins: int,
                       matches: int, points: int, pr: float | None) -> bool:
    """Add this tournament's totals onto one player's server row.

    Read-modify-write rather than a database `col = col + n` expression. That
    is safe in practice because banking only happens when a host closes a
    tournament — two closes for the same player in the same instant is not a
    scenario worth a stored procedure.
    """
    try:
        res = await db_io(
            lambda: supabase.table("nectar_guild_players")
            .select("kills,wins,matches,points")
            .eq("guild_id", int(guild_id))
            .eq("discord_id", str(discord_id))
            .limit(1)
            .execute()
        )
        rows = res.data or []
        current = rows[0] if rows else {}
        patch = {
            "kills": int(current.get("kills") or 0) + max(0, int(kills)),
            "wins": int(current.get("wins") or 0) + max(0, int(wins)),
            "matches": int(current.get("matches") or 0) + max(0, int(matches)),
            "points": int(current.get("points") or 0) + max(0, int(points)),
            "last_played_at": _now_iso(),
        }
        if pr is not None:
            patch["pr"] = float(pr)

        if rows:
            await db_io(
                lambda: supabase.table("nectar_guild_players")
                .update(patch)
                .eq("guild_id", int(guild_id))
                .eq("discord_id", str(discord_id))
                .execute()
            )
        else:
            await db_io(
                lambda: supabase.table("nectar_guild_players")
                .insert({"guild_id": int(guild_id), "discord_id": str(discord_id), **patch})
                .execute()
            )
        return True
    except Exception as e:
        log_err(f"GUILDSTATS bump {guild_id}/{discord_id}: {e}")
        return False


async def _stamp_applied(tourney_id: int) -> None:
    """Mark the tournament as banked (used by the dashboard to show 'banked')."""
    try:
        await db_io(
            lambda: supabase.table("bngc_comp_tourneys")
            .update({"stats_applied_at": _now_iso()})
            .eq("id", int(tourney_id))
            .execute()
        )
    except Exception as e:
        log_err(f"GUILDSTATS stamp {tourney_id}: {e}")


async def accrue_tourney(guild_id: int, tourney_id: int,
                         standings: list[dict] | None,
                         pr_board: list[dict] | None) -> int:
    """Apply one closed tournament to the server board. Returns players touched.

    `standings` rows come straight from routes/comp.py:standings_for_matches, so
    the number written here is exactly the number the board showed.
    `pr_board` rows carry the freshly computed rating per player.
    """
    if not supabase or not guild_id:
        return 0
    if not await _check_schema():
        return 0

    pr_by: dict[str, float] = {}
    for row in (pr_board or []):
        try:
            did = str((row or {}).get("discord_id") or "").strip()
            if did and (row or {}).get("pr") is not None:
                pr_by[did] = float(row["pr"])
        except Exception:
            continue

    # Players who appear on the board, plus anyone who only earned a rating.
    # Ordered so a player who is both is handled once.
    board_by: dict[str, dict] = {}
    for row in (standings or []):
        did = str((row or {}).get("discord_id") or "").strip()
        if did:
            board_by[did] = row or {}
    for did in pr_by:
        board_by.setdefault(did, {})

    if not board_by:
        await _stamp_applied(tourney_id)
        log_sys(f"GUILDSTATS tourney={tourney_id} guild={guild_id} players=0")
        return 0

    touched = 0
    failed = 0
    for did, row in board_by.items():
        try:
            if not await _claim_award(
                guild_id, tourney_id, did,
                kills=int(row.get("kills") or 0),
                wins=int(row.get("wins") or 0),
                matches=int(row.get("matches") or 0),
                points=int(row.get("points") or 0),
                pr=pr_by.get(did),
            ):
                continue  # already banked in an earlier run
            if await _bump_player(
                guild_id, did,
                kills=int(row.get("kills") or 0),
                wins=int(row.get("wins") or 0),
                matches=int(row.get("matches") or 0),
                points=int(row.get("points") or 0),
                pr=pr_by.get(did),
            ):
                touched += 1
            else:
                await _release_award(tourney_id, did)
                failed += 1
        except Exception as e:
            log_err(f"GUILDSTATS row {tourney_id}/{did}: {e}")
            failed += 1
            continue

    if not failed:
        await _stamp_applied(tourney_id)

    log_sys(f"GUILDSTATS tourney={tourney_id} guild={guild_id} "
            f"players={touched} failed={failed}")
    return touched
