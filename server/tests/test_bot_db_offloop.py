"""companion/bot/database.py must not run sync supabase-py IO on the loop.

supabase-py is a blocking HTTP client. Running its calls inline from a
coroutine stalls the whole event loop for a round trip, which is what starves
Discord interactions past their 3s defer window ("interaction expired before
defer"). This pins the fix: every helper goes through _sb_call, so the loop
keeps ticking while a call is in flight.
"""
import asyncio
import importlib.util
import time


def _load_bot_database():
    """Load the bot's data layer directly (no bot runtime, no network)."""
    spec = importlib.util.spec_from_file_location(
        "bot_database_offloop", "companion/bot/database.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _SlowResult:
    def __init__(self, data):
        self.data = data


class _SlowQuery:
    """Chainable builder whose execute() blocks like supabase-py does."""

    def __init__(self, data):
        self._data = data

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def insert(self, *a, **k):
        return self

    def in_(self, *a, **k):
        return self

    def execute(self):
        time.sleep(0.25)
        return _SlowResult(self._data)


class _SlowSB:
    def __init__(self, data):
        self._data = data

    def table(self, _name):
        return _SlowQuery(self._data)


def test_database_reads_keep_the_loop_ticking(monkeypatch):
    mod = _load_bot_database()
    monkeypatch.setattr(mod, "USE_SUPABASE", True)
    monkeypatch.setattr(mod, "supabase", _SlowSB([{"discord_id": "42"}]))

    async def scenario():
        gaps = []

        async def ticker():
            last = time.perf_counter()
            while True:
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        t = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        row = await mod.get_by_discord("42")
        locked = await mod.is_locked("42")
        leaders = await mod.get_leaderboard("kills", 3)
        await asyncio.sleep(0.03)
        t.cancel()
        return row, locked, leaders, max(gaps)

    row, locked, leaders, worst = asyncio.run(scenario())
    # values still come back from the (stubbed) client
    assert row == {"discord_id": "42"}
    assert locked is False
    assert leaders == [{"discord_id": "42"}]
    # three 0.25s round trips inline would show up as a ~0.25s loop gap
    assert worst < 0.15, f"event loop stalled for {worst:.3f}s"


def test_client_timeout_is_short_and_actually_applied():
    """The client's own timeout must be short, and the code must notice when
    the library ignores the setting.

    supabase-py 2.31's postgrest default is 120 SECONDS. A single blocking call
    made inline would then hold the event loop for two minutes — twelve times
    the 10s discord.py heartbeat budget. Setting it is easy to get silently
    wrong: `create_client(..., http_client=...)` is NOT a valid kwarg on this
    version, it raises, and the obvious fallback hands back the 120s default
    with only a log line. This asserts the timeout is genuinely on the client,
    and that the verification path actually rejects a client that ignored it.
    """
    mod = _load_bot_database()
    client = mod._create_supabase_client("https://example.supabase.co", "k" * 40)
    session = (getattr(client.postgrest, "session", None)
               or getattr(client.postgrest, "_client", None))
    timeout = getattr(session, "timeout", None)
    read = getattr(timeout, "read", timeout)
    assert read is not None and float(read) <= 5.0, (
        f"postgrest timeout is {timeout!r} — a blocking call can outlast the "
        f"10s heartbeat budget")
    assert mod.POSTGREST_TIMEOUT_SEC <= 5.0


def test_unapplied_timeout_is_rejected_not_silently_accepted(monkeypatch):
    """If the library ever ignores the option again, the builder must raise
    rather than hand back a 120s client that looks configured."""
    mod = _load_bot_database()

    class _IgnoringClient:
        postgrest = type("P", (), {"session": type(
            "S", (), {"timeout": 120.0})()})()

    import supabase
    monkeypatch.setattr(supabase, "create_client",
                        lambda *a, **k: _IgnoringClient())
    try:
        mod._create_supabase_client("https://example.supabase.co", "k" * 40)
    except RuntimeError as e:
        assert "timeout not applied" in str(e)
    else:
        raise AssertionError(
            "a client with a 120s timeout was accepted — the heartbeat guard "
            "is not doing anything")
