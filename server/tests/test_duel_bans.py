"""Wager bans: owner-only moderation that blocks create / join / accept.

A banned player must be stopped at the single shared eligibility gate
(`check_eligibility`), so /wager create, the Join button and the Accept
button all refuse them with a readable reason. The ban list is its own table
(`bngc_wager_bans`) — the account lock (`is_locked`) stays untouched so a
wager ban never takes the rest of the account with it.
"""
import importlib.util
import sys
from pathlib import Path

import pytest


def _load_duels():
    bot_dir = str(Path("companion/bot").resolve())
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    spec = importlib.util.spec_from_file_location(
        "duels_ban_under_test", Path(bot_dir) / "duels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Rows:
    def __init__(self, data):
        self.data = data


class _Query:
    """Minimal postgrest query-builder double (select/eq/limit/order/...)."""

    def __init__(self, rows):
        self._rows = rows  # live view: writes must reach the table double
        self._filters = {}
        self._single = False
        self._deleted = False

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filters[col] = str(val)
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def delete(self):
        self._deleted = True
        return self

    def upsert(self, payload, on_conflict=None):
        key = str(payload.get(on_conflict or "discord_id"))
        self._rows[:] = [r for r in self._rows if str(r.get("discord_id")) != key]
        self._rows.append(dict(payload))
        return self

    def update(self, *a, **k):
        return self

    def execute(self):
        if self._deleted:
            self._rows[:] = [r for r in self._rows
                             if str(r.get("discord_id")) != self._filters.get("discord_id")]
            return _Rows(self._rows)
        out = [r for r in self._rows
               if all(str(r.get(k)) == v for k, v in self._filters.items())]
        if self._single:
            return _Rows(out[:1])
        return _Rows(out)


class _SB:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.updates = []
        self.upsserts = []

    def table(self, name):
        self.name = name
        return _Query(self.rows)

    def patch(self, name):
        self.name = name
        return self


@pytest.fixture
def duels():
    return _load_duels()


# --- the ban list ----------------------------------------------------------

@pytest.mark.asyncio
async def test_is_wager_banned_reads_the_table(duels, monkeypatch):
    sb = _SB([{"discord_id": "111", "reason": "cheese"}])
    monkeypatch.setattr(duels, "_sb", lambda: sb)
    assert await duels.is_wager_banned("111") is True
    assert await duels.is_wager_banned("222") is False


@pytest.mark.asyncio
async def test_is_wager_banned_is_false_without_supabase(duels, monkeypatch):
    monkeypatch.setattr(duels, "_sb", lambda: None)
    assert await duels.is_wager_banned("111") is False


@pytest.mark.asyncio
async def test_wager_ban_records_reason_and_actor(duels, monkeypatch):
    sb = _SB()
    monkeypatch.setattr(duels, "_sb", lambda: sb)
    monkeypatch.setattr(duels.db, "_sb_call", _fake_sb_call)
    ok, err = await duels.wager_ban("111", "cheese", banned_by="999")
    assert ok is True and not err
    assert sb.rows[0]["reason"] == "cheese"
    assert sb.rows[0]["banned_by"] == "999"
    assert await duels.is_wager_banned("111") is True
    assert await duels.wager_ban_reason("111") == "cheese"


@pytest.mark.asyncio
async def test_wager_ban_is_idempotent(duels, monkeypatch):
    sb = _SB()
    monkeypatch.setattr(duels, "_sb", lambda: sb)
    monkeypatch.setattr(duels.db, "_sb_call", _fake_sb_call)
    await duels.wager_ban("111", "first", banned_by="999")
    await duels.wager_ban("111", "second", banned_by="999")
    assert len(sb.rows) == 1
    assert await duels.wager_ban_reason("111") == "second"


@pytest.mark.asyncio
async def test_wager_unban_clears_the_row(duels, monkeypatch):
    sb = _SB([{"discord_id": "111", "reason": "cheese"}])
    monkeypatch.setattr(duels, "_sb", lambda: sb)
    monkeypatch.setattr(duels.db, "_sb_call", _fake_sb_call)
    ok, err = await duels.wager_unban("111")
    assert ok is True and not err
    assert await duels.is_wager_banned("111") is False


@pytest.mark.asyncio
async def test_wager_ban_requires_a_user_id(duels):
    assert await duels.wager_ban("", "x") == (False, "Need a user.")
    assert await duels.wager_unban("") == (False, "Need a user.")


# --- eligibility gate ------------------------------------------------------

@pytest.mark.asyncio
async def test_banned_player_cannot_wager(duels, monkeypatch):
    monkeypatch.setattr(duels, "_sb", lambda: _SB())
    monkeypatch.setattr(duels, "wager_ban_reason", _reason_stub("cheese"))
    ok, reason = await duels.check_eligibility("111", 0)
    assert ok is False
    assert "banned" in reason.lower()
    assert "cheese" in reason


@pytest.mark.asyncio
async def test_ban_check_runs_before_the_user_lookup(duels, monkeypatch):
    # A banned player must be told they are banned — not "you need /register",
    # and not "play a tracked match" (the ban gate is the first thing read).
    monkeypatch.setattr(duels, "_sb", lambda: _SB())
    monkeypatch.setattr(duels, "wager_ban_reason", _reason_stub("cheese"))

    async def _explode(_uid):
        raise AssertionError("user row must not be read for a banned player")
    monkeypatch.setattr(duels.db, "get_by_discord", _explode)
    ok, reason = await duels.check_eligibility("111", 0)
    assert ok is False and "banned" in reason.lower()


@pytest.mark.asyncio
async def test_unbanned_player_passes_the_ban_gate(duels, monkeypatch):
    monkeypatch.setattr(duels, "_sb", lambda: _SB())
    monkeypatch.setattr(duels, "wager_ban_reason", _reason_stub(""))

    async def _row(_uid):
        return {"game_username": "cis6led", "nectar_coins": 100}
    monkeypatch.setattr(duels.db, "get_by_discord", _row)

    async def _sessions(_uid):
        return [{"session_id": "sess_1a0ce360"}]
    monkeypatch.setattr(duels, "user_sessions_recent", _sessions)

    async def _no_active(_uid):
        return None
    monkeypatch.setattr(duels, "get_user_active", _no_active)
    ok, reason = await duels.check_eligibility("111", 0)
    assert ok is True, reason


async def _fake_sb_call(fn):
    import inspect
    result = fn()
    if inspect.isawaitable(result):
        result = await result
    return result


def _reason_stub(reason):
    async def _f(_discord_id):
        return reason
    return _f
