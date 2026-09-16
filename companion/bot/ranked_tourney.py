"""Ranked tourneys — Discord data layer + embed specs.

Tables (migration supabase/add_ranked_tourney_v247.sql): bngc_ranked_tourneys
and friends. Solo-first: the Join/Leave buttons write single-member
`accepted` regs; duo rows are listed but never touched by the buttons.
Comp tables (`bngc_comp_*`) and ranked-room tables (`ranked_rooms`, …) are
never written here. Every helper is bot-safe (never raises beyond
RuntimeError for a missing DB).
"""
from datetime import datetime, timezone

import database as db
import ranked as ranked_mod

T_TOURNEY = "bngc_ranked_tourneys"
T_MATCH = "bngc_ranked_tourney_matches"
T_REG = "bngc_ranked_tourney_registrations"

STATUSES = ("created", "scheduled", "started", "ended")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sb():
    if not db.USE_SUPABASE or not db.supabase:
        raise RuntimeError("Supabase is not configured")
    return db.supabase


# ---------------------------------------------------------------------------
# Tourneys
# ---------------------------------------------------------------------------

def get_tourney(tid: int) -> dict | None:
    """One ranked tourney row, or None. Never raises."""
    try:
        r = _sb().table(T_TOURNEY).select("*").eq("id", int(tid)).limit(1).execute()
        rows = r.data or []
        return dict(rows[0]) if rows else None
    except Exception:
        return None


def get_tourney_by_code(code: str) -> dict | None:
    """Single non-ended ranked tourney for a 5-char code, else None."""
    try:
        code = str(code or "").strip().upper()
        if len(code) != 5 or not code.isalnum():
            return None
        r = _sb().table(T_TOURNEY).select("*").eq("code", code).neq(
            "status", "ended").limit(2).execute()
        rows = r.data or []
        return dict(rows[0]) if len(rows) == 1 else None
    except Exception:
        return None


def list_tourneys(status: str = "all") -> list:
    """Newest-last tourney rows, optionally filtered. Never raises."""
    try:
        q = _sb().table(T_TOURNEY).select(
            "id,code,name,status,mode,series_len,start_elo,avg_elo,created_at"
        ).order("created_at", desc=False).limit(100)
        if (status or "all") != "all":
            q = q.eq("status", status)
        return [dict(r) for r in (q.execute().data or [])]
    except Exception:
        return []


def create_tourney(*, name, series_len=0, mode="solo",
                   created_by=None, game_mode=None,
                   ping_role_id=None) -> dict:
    """Insert a `created` ranked tourney and return the row. Raises on DB error.

    No room code here: the code is set at /rtourney start and dispatched
    to the roster (create embed shows "Wait for start").
    """
    try:
        series = max(0, int(series_len or 0))
    except Exception:
        series = 0
    mode = "duo" if str(mode or "").lower() == "duo" else "solo"
    row = {
        "name": str(name or "").strip()[:80] or "Ranked Tourney",
        "code": None, "status": "created", "mode": mode,
        "series_len": series,
        "game_mode": (str(game_mode or "").strip() or None),
        "ping_role_id": (str(ping_role_id or "").strip() or None),
        "created_by": str(created_by or "") or None,
    }
    res = _sb().table(T_TOURNEY).insert(row).execute()
    rows = res.data or []
    if not rows:
        raise RuntimeError("insert returned no row")
    return dict(rows[0])


def update_tourney(tid: int, fields: dict) -> dict | None:
    """Patch a tourney row, return the fresh row (or None). Never raises."""
    try:
        _sb().table(T_TOURNEY).update(dict(fields or {})).eq("id", int(tid)).execute()
        return get_tourney(tid)
    except Exception:
        return None


def sealed_count(tid: int) -> int:
    """Sealed+manual match count for a tourney. Never raises."""
    try:
        r = _sb().table(T_MATCH).select("id").eq("tourney_id", int(tid)).in_(
            "status", ["sealed", "manual"]).limit(500).execute()
        return len(r.data or [])
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Registrations (solo button flow)
# ---------------------------------------------------------------------------

def _regs_for(tid: int) -> list:
    try:
        r = _sb().table(T_REG).select(
            "id,member_ids,status").eq("tourney_id", int(tid)).limit(500).execute()
        return [dict(x) for x in (r.data or [])]
    except Exception:
        return []


def is_banned(tid: int, discord_id: str) -> bool:
    """True when the player has a banned reg row in this tourney."""
    try:
        did = str(discord_id)
        return any(str((r or {}).get("status")) == "banned"
                   and did in [str(x) for x in ((r or {}).get("member_ids") or [])]
                   for r in _regs_for(tid))
    except Exception:
        return False


def roster_ids(tid: int) -> list[str]:
    """Accepted solo+duo member ids (duo members included in counts)."""
    try:
        out = []
        for r in _regs_for(tid):
            if str((r or {}).get("status")) != "accepted":
                continue
            for m in ((r or {}).get("member_ids") or []):
                if m and str(m) not in out:
                    out.append(str(m))
        return out
    except Exception:
        return []


def reg_add(tid: int, discord_id: str, display_name: str = "") -> tuple[bool, str]:
    """Join: insert an accepted solo reg. Idempotent. Returns (ok, msg)."""
    try:
        did = str(discord_id or "").strip()
        if not did:
            return False, "Missing Discord id."
        if is_banned(tid, did):
            return False, "You are banned from this tourney."
        regs = _regs_for(tid)
        if any(did in [str(x) for x in ((r or {}).get("member_ids") or [])]
               and str((r or {}).get("status")) == "accepted" for r in regs):
            return True, "Already in."
        try:
            t = get_tourney(tid) or {}
            maxp = int(t.get("max_players") or 0)
            if maxp > 0 and len(roster_ids(tid)) >= maxp:
                return False, "Tourney is full."
        except Exception:
            pass
        names = {}
        if (display_name or "").strip():
            names = {did: {"username": str(display_name)[:32],
                           "display_name": str(display_name)[:32]}}
        _sb().table(T_REG).insert({
            "tourney_id": int(tid), "member_ids": [did],
            "member_names": names, "status": "accepted",
        }).execute()
        return True, "Joined."
    except Exception as exc:
        return False, f"Join failed ({str(exc)[:60]})."


def reg_remove(tid: int, discord_id: str) -> tuple[bool, str]:
    """Leave: delete the player's solo accepted row(s). Duo rows untouched."""
    try:
        did = str(discord_id or "").strip()
        if not did:
            return False, "Missing Discord id."
        regs = _regs_for(tid)
        solo = [r for r in regs
                if str((r or {}).get("status")) == "accepted"
                and [str(x) for x in ((r or {}).get("member_ids") or [])] == [did]]
        if not solo:
            return False, "You're not in this tourney."
        for r in solo:
            try:
                _sb().table(T_REG).delete().eq("id", r.get("id")).execute()
            except Exception:
                continue
        return True, "Left."
    except Exception as exc:
        return False, f"Leave failed ({str(exc)[:60]})."


# ---------------------------------------------------------------------------
# Ranked-points payout (end of tourney, exactly once)
# ---------------------------------------------------------------------------

def points_paid(tid: int) -> bool:
    """True when the tourney's standings were already paid to ranked_points."""
    try:
        t = get_tourney(tid)
        return bool((t or {}).get("points_paid"))
    except Exception:
        return False


def standings_deltas(rows) -> dict:
    """Standings rows -> {discord_id: points} payout map. Pure.

    Solo rows pay directly. Duo team rows (member_ids, no discord_id) split
    evenly, remainder to the first-listed member. Zero totals skipped."""
    out: dict[str, int] = {}
    try:
        for r in (rows or []):
            try:
                pts = int((r or {}).get("points") or 0)
                if not pts:
                    continue
                if (r or {}).get("team"):
                    members = [str(x) for x in ((r or {}).get("member_ids") or []) if x]
                    if not members:
                        continue
                    share, rem = divmod(pts, len(members))
                    for i, m in enumerate(members):
                        out[m] = out.get(m, 0) + share + (rem if i == 0 else 0)
                else:
                    did = str((r or {}).get("discord_id") or "").strip()
                    if did:
                        out[did] = out.get(did, 0) + pts
            except Exception:
                continue
    except Exception:
        pass
    return {d: p for d, p in out.items() if p}


def pay_ranked_points(deltas: dict) -> dict:
    """Add deltas to ranked_points (one read + one upsert). Returns totals.

    Mirrors server _bump_points_many minus the cache. Raises on DB error so
    the caller can abort before flipping any payout flag."""
    clean = {}
    try:
        for did, sub in (deltas or {}).items():
            try:
                d, p = str(did or "").strip(), int(sub or 0)
                if d and p:
                    clean[d] = clean.get(d, 0) + p
            except Exception:
                continue
    except Exception:
        pass
    if not clean:
        return {}
    sb = _sb()
    try:
        cur = sb.table("ranked_points").select("discord_id,points").in_(
            "discord_id", sorted(clean)[:200]).execute()
        have = {}
        for row in (cur.data or []):
            try:
                have[str((row or {}).get("discord_id"))] = int((row or {}).get("points") or 0)
            except Exception:
                continue
    except Exception:
        have = {}
    totals = {d: int(have.get(d, 0)) + p for d, p in clean.items()}
    sb.table("ranked_points").upsert(
        [{"discord_id": d, "points": p} for d, p in totals.items()],
        on_conflict="discord_id").execute()
    return totals


def mark_points_paid(tid: int) -> None:
    """Flag the tourney paid (after a successful bump). Raises on DB error."""
    _sb().table(T_TOURNEY).update({"points_paid": True}).eq("id", int(tid)).execute()


def is_host_row(t, discord_id: str) -> bool:
    """True when the did created the tourney or is a listed host."""
    try:
        did = str(discord_id or "").strip()
        if not did or not t:
            return False
        if did == str((t or {}).get("created_by") or ""):
            return True
        return did in [str(x) for x in ((t or {}).get("hosts") or [])]
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Embed specs (bot.py renders these; same shape as ranked.lobby_embed)
# ---------------------------------------------------------------------------

def set_code(tid: int, code: str | None) -> tuple[bool, str]:
    """Set (or clear with None/"") a tourney's room code. Returns (ok, msg)."""
    try:
        code_c = str(code or "").strip().upper() or None
        if code_c is not None and (len(code_c) != 5 or not code_c.isalnum()):
            return False, "Code must be 5 letters/digits."
        t = get_tourney(tid)
        if not t:
            return False, "No ranked tourney with that ID."
        if str(t.get("status") or "") == "ended":
            return False, "Tourney already ended."
        update_tourney(int(tid), {"code": code_c})
        return True, "Code cleared." if code_c is None else f"Code set to `{code_c}`."
    except Exception as exc:
        return False, f"Couldn't save the code ({str(exc)[:60]})."


def rank_map(rids) -> dict:
    """discord_id -> ranked ladder points. Display-only. Never raises."""
    try:
        ids = sorted({str(d) for d in (rids or []) if str(d or "").strip()})[:200]
        if not ids:
            return {}
        pts = ranked_mod.get_points_many(ids) or {}
        return {str(d): int((pts or {}).get(d, 0)) for d in ids}
    except Exception:
        return {}


def rank_snapshot(rids) -> tuple[float | None, str | None]:
    """(avg ranked points, milestone name) for dids. Display-only."""
    try:
        ids = sorted({str(d) for d in (rids or []) if str(d or "").strip()})[:200]
        if not ids:
            return None, None
        pts = ranked_mod.get_points_many(ids) or {}
        vals = [int(pts.get(d, 0)) for d in ids]
        if not vals:
            return None, None
        avg = round(sum(vals) / len(vals), 1)
        try:
            ms, _fee = ranked_mod.milestone_for(avg)
        except Exception:
            ms = "Unranked"
        return avg, ms
    except Exception:
        return None, None


def host_line(t: dict, rids) -> str:
    """Host mention with playing state: in roster = playing, else hosting."""
    try:
        base = f"<@{t.get('created_by')}>" if (t or {}).get("created_by") else "—"
        if not base or base == "—":
            return base
        try:
            playing = str((t or {}).get("created_by") or "") in [str(x) for x in (rids or [])]
        except Exception:
            playing = False
        return base + (" (playing)" if playing else " (hosting)")
    except Exception:
        return "—"


# ---------------------------------------------------------------------------
# Embed specs (bot.py renders these; same shape as ranked.lobby_embed)
# ---------------------------------------------------------------------------

def lobby_spec(t: dict) -> dict:
    """Spec for the ranked-tourney lobby embed: title, fields, footer."""
    try:
        t = t or {}
        tid = t.get("id")
        try:
            rids = roster_ids(int(tid)) if tid is not None else []
        except Exception:
            rids = []
        try:
            avg, ms = rank_snapshot(rids)
        except Exception:
            avg, ms = None, None
        series = t.get("series_len") or 0
        code = (t.get("code") or "").strip() if t.get("code") else ""
        return {
            "title": f"Ranked Tourney #{tid} — {t.get('name') or 'Untitled'}",
            "fields": [
                ("Host", host_line(t, rids), True),
                ("Status", str(t.get("status") or "created").capitalize(), True),
                ("Players", str(len(rids)), True),
                ("Series", f"{sealed_count(int(tid))}/{series or '∞'}" if tid is not None else "—", True),
                ("Rank", f"avg {avg} pts ({ms})" if avg is not None else "—", True),
                ("Game", str(t.get("game_mode") or "—"), True),
                ("Code", f"`{code}`" if code else "Wait for start", True),
            ],
            "footer": f"rtourney #{tid} · press Join to enter · dashboard: /ranked",
        }
    except Exception:
        return {"title": "Ranked Tourney", "fields": [], "footer": ""}


def live_spec(t: dict, thread_id=None) -> dict:
    """Spec for the flipped-live lobby embed."""
    try:
        t = t or {}
        tid = t.get("id")
        try:
            rids = roster_ids(int(tid)) if tid is not None else []
        except Exception:
            rids = []
        try:
            series = t.get("series_len") or 0
            played = sealed_count(int(tid)) if tid is not None else 0
        except Exception:
            series, played = 0, 0
        return {
            "title": f"Ranked Tourney LIVE #{tid} — {t.get('name') or 'Untitled'}",
            "fields": [
                ("Host", host_line(t, rids), True),
                ("Status", "Live — scoring on the /ranked dashboard", True),
                ("Players", str(len(rids)), True),
                ("Series", f"{played}/{series or '∞'}", True),
            ],
            "footer": f"rtourney #{tid} · thread: {thread_id or t.get('thread_id') or '—'}",
        }
    except Exception:
        return {"title": "Ranked Tourney LIVE", "fields": [], "footer": ""}


def ended_spec(t: dict, thread_id=None) -> dict:
    """Spec for the ended lobby embed."""
    try:
        t = t or {}
        tid = t.get("id")
        try:
            rids = roster_ids(int(tid)) if tid is not None else []
        except Exception:
            rids = []
        return {
            "title": f"Ranked Tourney ENDED #{tid} — {t.get('name') or 'Untitled'}",
            "fields": [
                ("Host", host_line(t, rids), True),
                ("Status", "Ended", True),
                ("Players", str(len(rids)), True),
            ],
            "footer": f"rtourney #{tid} · over · thread: {thread_id or t.get('thread_id') or '—'}",
        }
    except Exception:
        return {"title": "Ranked Tourney ENDED", "fields": [], "footer": ""}
