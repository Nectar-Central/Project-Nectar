"""The AFK guard must not cancel a wager that is being played.

`last_online` is stamped best-effort on every capture batch, under a hard 6s
DB deadline, and is NULL for anyone who has never been captured. Reading it
is not evidence of absence:

  - NULL / unparsable / a failed read is UNKNOWN, not offline;
  - a player with duel event rows inside the window is demonstrably playing,
    whatever their heartbeat says.

Both were treated as "offline", so a live wager was cancelled and both stakes
refunded mid-game - the guard is the only thing in the resolver that can move
real coins, so it fails open.
"""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _load_duels():
    bot_dir = str(Path("companion/bot").resolve())
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    spec = importlib.util.spec_from_file_location(
        "duels_afk_under_test", Path(bot_dir) / "duels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def _wager():
    return {
        "id": "44444444-4444-4444-4444-444444444444",
        "creator_id": "111", "opponent_id": "222", "mode": "zw_lg",
        "stake": 100, "thread_id": "",
        "active_since": _iso(hours=3),
    }


def _patch(duels, monkeypatch, users, events=None, refunds=None):
    """users: {did: row}; events: {did: iso or None}."""
    async def _row(uid):
        r = users.get(str(uid))
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(duels.db, "get_by_discord", _row)

    async def _ev(did):
        ts = (events or {}).get(str(did))
        return ts
    monkeypatch.setattr(duels, "recent_event_iso", _ev, raising=False)

    async def _refund(uid, stake, wid, why):
        (refunds if refunds is not None else []).append((uid, why))
    monkeypatch.setattr(duels, "_refund", _refund)

    async def _update(_wid, _fields):
        return True
    monkeypatch.setattr(duels, "update_wager", _update)

    async def _board(_bot, _w):
        return None
    monkeypatch.setattr(duels, "refresh_board_embed", _board)
    monkeypatch.setattr(duels, "_resolve_thread", lambda _b, _t: None)
    monkeypatch.setattr(duels, "_untrack_thread", lambda _t: None)

    async def _dm(_b, _u, _m):
        return None
    monkeypatch.setattr(duels, "_dm", _dm)


@pytest.mark.asyncio
async def test_unknown_last_online_never_cancels(monkeypatch):
    # NULL last_online: never stamped, or the 6s write deadline blew
    duels = _load_duels()
    refunds = []
    _patch(duels, monkeypatch,
           {"111": {"last_online": None}, "222": {"last_online": _iso(minutes=5)}},
           events={"111": None}, refunds=refunds)
    assert await duels._afk_guard(None, _wager()) is False
    assert refunds == []


@pytest.mark.asyncio
async def test_a_failed_user_read_never_cancels(monkeypatch):
    # a transient DB error must not look like a player walking away
    duels = _load_duels()
    refunds = []
    _patch(duels, monkeypatch,
           {"111": RuntimeError("db down"), "222": {"last_online": _iso(minutes=5)}},
           events={"111": None}, refunds=refunds)
    assert await duels._afk_guard(None, _wager()) is False
    assert refunds == []


@pytest.mark.asyncio
async def test_a_stale_heartbeat_with_live_duel_rows_never_cancels(monkeypatch):
    # the reported case: playing right now, heartbeat stale anyway
    duels = _load_duels()
    refunds = []
    _patch(duels, monkeypatch,
           {"111": {"last_online": _iso(hours=2)},
            "222": {"last_online": _iso(minutes=5)}},
           events={"111": _iso(minutes=2)}, refunds=refunds)
    assert await duels._afk_guard(None, _wager()) is False
    assert refunds == []


@pytest.mark.asyncio
async def test_a_genuinely_absent_duelist_still_cancels(monkeypatch):
    # the guard must keep working: old heartbeat AND no duel rows at all
    duels = _load_duels()
    refunds = []
    _patch(duels, monkeypatch,
           {"111": {"last_online": _iso(hours=2)},
            "222": {"last_online": _iso(minutes=5)}},
           events={"111": _iso(hours=2)}, refunds=refunds)
    assert await duels._afk_guard(None, _wager()) is True
    assert sorted(r[0] for r in refunds) == ["111", "222"]


@pytest.mark.asyncio
async def test_the_grace_window_still_short_circuits(monkeypatch):
    duels = _load_duels()
    refunds = []
    w = _wager()
    w["active_since"] = _iso(minutes=1)
    _patch(duels, monkeypatch,
           {"111": {"last_online": _iso(hours=5)}, "222": {"last_online": None}},
           events={"111": None}, refunds=refunds)
    assert await duels._afk_guard(None, w) is False
    assert refunds == []
