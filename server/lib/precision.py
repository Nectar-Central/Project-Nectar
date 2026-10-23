"""
precision.py -server-side processing precision score for kills, deaths, wins, defeats.

How it works:
  1. The processor calls schedule() right AFTER each successful DB write
     (kill / death / win / defeat), capturing an immutable snapshot of what
     was written (expected counter value + identifying event fields).
  2. Each scheduled item becomes due VERIFY_DELAY_S (2.0s) later.
  3. Due items are verified opportunistically -at the start of the next
     process_batch() and on every GET /api/health poll -via fresh DB reads:
       - the user's counter is still >= the expected value (write persisted,
         not rolled back or overwritten), AND
       - a matching bngc_game_events row exists for this session/action.
     Both must hold -> precise. Either fails -> imprecise.
     When Supabase is unreachable or the row can't be checked, the item is
     counted as "skipped" (unknown), never as a failure -infra blips must
     not masquerade as processing errors. The skipped count is exposed so
     consumers can see how much evidence the score is based on.
  4. The score is a rolling window (last WINDOW_N outcomes) smoothed with a
     Bayesian prior (PRIOR_OK / PRIOR_N = 99.0%). A healthy pipeline therefore
     reads 99.0% fresh, climbs through 99.9% with volume, and rounds to 100.0%
     at high volume -while a real streak of bad writes can still pull it
     below 99, which is the honest behavior (the score is a measurement,
     not a badge).

Notes:
  - Verification is DB-only on purpose: engine state may live on a different
    serverless worker, but Supabase is the shared source of truth.
  - Pending items live in worker memory (they reference nothing but plain
    data, so losing them on a cold start only shrinks the sample, no bias).
  - Aggregated counters are best-effort persisted to Upstash (24h TTL) so a
    fresh worker inherits history instead of restarting at the prior.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from .supabase_client import db_io

from .logger import log_sys, log_err

VERIFY_DELAY_S = 2.0       # "2s later it checks" -delay between write and verification
WINDOW_N = 500             # rolling window of recent outcomes
PRIOR_OK = 198             # prior pseudo-observations…
PRIOR_N = 200              # …so a fresh/healthy score starts at exactly 99.0%
ACTIONS = ("kill", "death", "win", "defeat")
_COUNTER_FIELD = {"kill": "kills", "death": "deaths", "win": "wins", "defeat": "defeats"}
_MAX_PENDING = 2000        # memory cap for scheduled (not yet due) items
_SNAPSHOT_KEY = "bnc:precision:v1"
_SNAPSHOT_TTL = 86400      # 24h sliding window on the shared snapshot

_pending: deque[dict[str, Any]] = deque()   # items awaiting their 2s verification
_outcomes: deque[bool] = deque(maxlen=WINDOW_N)
_per_action: dict[str, dict[str, int]] = {a: {"ok": 0, "fail": 0} for a in ACTIONS}
_skipped = 0               # unverifiable (infra/unknown) -not counted either way
_snapshot_loaded = False


def _score(ok: int, n: int, prior_ok: int = PRIOR_OK, prior_n: int = PRIOR_N) -> float:
    """Prior-weighted percentage, rounded to 2 decimals."""
    return round(100.0 * (ok + prior_ok) / (n + prior_n), 2)


def schedule(action: str, *, session_id: str, discord_id: str,
             expected: int, match_version: Any = None,
             room_id: str | None = None, extra: dict | None = None) -> None:
    """Queue one write for verification ~2s later. Never raises."""
    try:
        if action not in ACTIONS or not session_id or not discord_id:
            return
        _pending.append({
            "due": time.time() + VERIFY_DELAY_S,
            "action": action,
            "session_id": session_id,
            "discord_id": discord_id,
            "expected": int(expected or 0),
            "match_version": match_version,
            "room_id": room_id,
            "extra": dict(extra or {}),
        })
        while len(_pending) > _MAX_PENDING:
            _pending.popleft()
    except Exception as exc:
        log_err(f"precision schedule failed {exc}")


def _record(ok: bool, action: str) -> None:
    _outcomes.append(bool(ok))
    try:
        _per_action[action]["ok" if ok else "fail"] += 1
    except KeyError:
        pass


def _verify_item(item: dict[str, Any]) -> tuple[bool | None, str]:
    """Re-check one write against fresh DB state.

    Returns (True/False/None, reason): True = precise, False = imprecise
    (with reason "counter" and/or "event"), None = unknown, skip.
    """
    from .supabase_client import supabase

    if not supabase:
        return None, "no-supabase"
    try:
        action = item["action"]
        field = _COUNTER_FIELD[action]
        ures = supabase.table("bngc_users").select(
            "kills,deaths,wins,defeats").eq(
            "discord_id", item["discord_id"]).single().execute()
        user = ures.data if isinstance(ures, object) else None
        if not isinstance(user, dict):
            return None, "no-user-row"
        try:
            counter_ok = int(user.get(field) or 0) >= int(item["expected"])
        except Exception:
            return None, "bad-expected"
        # Newest-first: high-volume sessions (100+ DF kills in one version)
        # push the write out of an unordered limit-50 window (false fails).
        eres = supabase.table("bngc_game_events").select("event_data").eq(
            "session_id", item["session_id"]).eq(
            "event_type", action).order("created_at", desc=True).limit(50).execute()
        rows = eres.data if isinstance(getattr(eres, "data", None), list) else []
        event_ok = False
        want_mv = item.get("match_version")
        want_extra = item.get("extra") or {}
        for r in rows:
            ed = (r or {}).get("event_data") or {}
            if want_mv is not None and ed.get("match_version") != want_mv:
                continue
            if action == "kill" and want_extra.get("victim") is not None:
                if ed.get("victim") != want_extra["victim"]:
                    continue
            if action == "death" and want_extra.get("via") is not None:
                if ed.get("via") != want_extra["via"]:
                    continue
            event_ok = True
            break
        if counter_ok and event_ok:
            return True, ""
        return False, "+".join([s for s, ok in (("counter", counter_ok), ("event", event_ok)) if not ok])
    except Exception as exc:
        log_err(f"precision verify failed ({item.get('action')}) {exc}")
        return None, "exception"


async def verify_due(max_items: int = 10) -> int:
    """Verify all items past their 2s delay (capped). Returns # verified. Never raises."""
    global _skipped
    try:
        now = time.time()
        due = []
        while _pending and _pending[0]["due"] <= now and len(due) < max_items:
            due.append(_pending.popleft())
        # Drop overdue-but-uncapped backlog silently? No -keep for next call.
        for item in due:
            res, reason = await db_io(_verify_item, item)
            if res is None:
                _skipped += 1
            else:
                _record(res, item["action"])
                if not res:
                    log_sys(f"PRECISION imprecise action={item.get('action')} "
                            f"session={str(item.get('session_id') or '')[:8]} "
                            f"expected={item.get('expected')} reason={reason}")
        if due:
            log_sys(f"PRECISION verified {len(due)} pending={len(_pending)} "
                    f"score={current_score_pct()}%")
            await _save_snapshot()
        return len(due)
    except Exception as exc:
        log_err(f"precision verify_due failed {exc}")
        return 0


def current_score_pct() -> float:
    ok = sum(1 for o in _outcomes if o)
    return _score(ok, len(_outcomes))


async def _save_snapshot() -> None:
    """Best-effort shared persistence so fresh workers inherit history."""
    try:
        from .redis_client import cache
        import json as _j
        snap = _j.dumps({
            "outcomes": [1 if o else 0 for o in _outcomes],
            "per_action": _per_action,
            "skipped": _skipped,
        }, separators=(",", ":"))
        await cache.set_raw(_SNAPSHOT_KEY, snap, ttl=_SNAPSHOT_TTL)
    except Exception:
        pass


async def _load_snapshot() -> None:
    global _skipped, _snapshot_loaded
    _snapshot_loaded = True
    try:
        from .redis_client import cache
        import json as _j
        raw = await cache.get_raw(_SNAPSHOT_KEY)
        if not raw:
            return
        snap = _j.loads(raw)
        for o in (snap.get("outcomes") or [])[-WINDOW_N:]:
            _outcomes.append(bool(o))
        for a in ACTIONS:
            pa = (snap.get("per_action") or {}).get(a) or {}
            _per_action[a] = {"ok": int(pa.get("ok") or 0), "fail": int(pa.get("fail") or 0)}
        _skipped = int(snap.get("skipped") or 0)
    except Exception:
        pass


async def get_snapshot() -> dict[str, Any]:
    """Score payload for /api/health. Never raises."""
    try:
        if not _snapshot_loaded and not _outcomes:
            await _load_snapshot()
        ok = sum(1 for o in _outcomes if o)
        n = len(_outcomes)
        per: dict[str, Any] = {}
        for a in ACTIONS:
            pa = _per_action[a]
            pan = pa["ok"] + pa["fail"]
            per[a] = {"ok": pa["ok"], "fail": pa["fail"],
                      "pct": _score(pa["ok"], pan, prior_ok=99, prior_n=100)}
        return {
            "precision_pct": _score(ok, n),
            "verified": n,
            "ok": ok,
            "failed": n - ok,
            "skipped": _skipped,
            "pending": len(_pending),
            "window": WINDOW_N,
            "verify_delay_s": VERIFY_DELAY_S,
            "per_action": per,
        }
    except Exception as exc:
        log_err(f"precision snapshot failed {exc}")
        return {"precision_pct": 99.0, "verified": 0, "ok": 0, "failed": 0,
                "skipped": 0, "pending": len(_pending), "window": WINDOW_N,
                "verify_delay_s": VERIFY_DELAY_S, "per_action": {}}
