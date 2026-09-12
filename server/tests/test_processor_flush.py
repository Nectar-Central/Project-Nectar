"""Batch-end write hardening (fake supabase, no network).

- zone_change rows are retired: _buf_event must never queue them.
- User counter flush retries blips so a saved win row can't lose its
  counter increment ("wins not saved").
- Event flush resilience: one poison row must not nuke the batch.
"""
import server.scripts.processor as proc


class _FailOnce:
    """db.table().update().eq().execute() chain: fails the first execute."""

    def __init__(self, calls):
        self.calls = calls
        self.n = 0

    def table(self, name):
        self.calls.append(("table", name))
        return self

    def update(self, payload):
        self.calls.append(("update", dict(payload)))
        return self

    def eq(self, c, v):
        return self

    def execute(self):
        self.n += 1
        if self.n == 1:
            raise ConnectionError("Server disconnected")
        return {"data": [{"discord_id": "x"}]}


class _AlwaysFail(_FailOnce):
    def execute(self):
        self.n += 1
        raise ConnectionError("Server disconnected")


def test_zone_change_never_buffered():
    buf = []
    proc._buf_event(buf, "d", "s", "zone_change", {"phase": 1})
    assert buf == []
    # other types still queue
    proc._buf_event(buf, "d", "s", "win", {})
    assert len(buf) == 1
    proc._buf_event(None, "d", "s", "zone_change", {})  # direct path also drops


def test_user_flush_retries_blip(monkeypatch):
    calls = []
    monkeypatch.setattr(proc, "supabase", _FailOnce(calls))
    user = {"wins": 30}
    ok = proc._flush_user_counters("did1", {}, {"wins"}, user, attempts=3)
    assert ok is True
    updates = [c[1] for c in calls if c[0] == "update"]
    assert updates and updates[-1] == {"wins": 30}


def test_user_flush_gives_up_after_retries(monkeypatch):
    calls = []
    monkeypatch.setattr(proc, "supabase", _AlwaysFail(calls))
    ok = proc._flush_user_counters("did1", {}, {"wins"}, {"wins": 30}, attempts=2)
    assert ok is False


def test_user_flush_empty_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(proc, "supabase", _FailOnce(calls))
    assert proc._flush_user_counters("did1", {}, set(), {}) is True
    assert calls == []


class _FakeTable:
    def __init__(self, fail_on=None):
        # fail_on: 'bulk' (multi-row inserts fail), {'row', i} etc.
        self.fail_on = fail_on
        self.written = []

    def insert(self, payload):
        self._payload = payload
        return self

    def execute(self):
        rows = self._payload if isinstance(self._payload, list) else [self._payload]
        if self.fail_on == "bulk" and len(rows) > 1:
            raise RuntimeError("bulk rejected")
        if isinstance(self.fail_on, dict) and self.fail_on.get("event_type"):
            bad = [r for r in rows if (r or {}).get("event_type") == self.fail_on["event_type"]]
            if bad:
                raise RuntimeError("poison row")
        self.written.extend(rows)
        return {"data": rows}


class _FakeSupabase:
    def __init__(self, table):
        self._table = table

    def table(self, name):
        assert name == "bngc_game_events"
        return self._table


def test_flush_all_ok(monkeypatch):
    t = _FakeTable()
    monkeypatch.setattr(proc, "supabase", _FakeSupabase(t))
    buf = [{"event_type": "kill"}, {"event_type": "killfeed"}]
    assert proc._flush_events(buf) == (2, 0)
    assert buf == []
    assert len(t.written) == 2


def test_flush_empty_noop(monkeypatch):
    t = _FakeTable()
    monkeypatch.setattr(proc, "supabase", _FakeSupabase(t))
    assert proc._flush_events([]) == (0, 0)


def test_flush_bulk_fail_splits_chunks(monkeypatch):
    t = _FakeTable(fail_on="bulk")
    monkeypatch.setattr(proc, "supabase", _FakeSupabase(t))
    buf = [{"event_type": "kill", "i": i} for i in range(5)]
    flushed, dropped = proc._flush_events(buf, chunk=2)
    assert (flushed, dropped) == (5, 0)
    assert buf == []
    assert len(t.written) == 5


def test_flush_poison_row_dropped_rest_saved(monkeypatch):
    t = _FakeTable(fail_on={"event_type": "poison"})
    monkeypatch.setattr(proc, "supabase", _FakeSupabase(t))
    buf = [{"event_type": "kill"}, {"event_type": "poison"}, {"event_type": "death"}]
    flushed, dropped = proc._flush_events(buf, chunk=50)
    assert flushed == 2 and dropped == 1
    assert buf == []
    assert sorted(r["event_type"] for r in t.written) == ["death", "kill"]
