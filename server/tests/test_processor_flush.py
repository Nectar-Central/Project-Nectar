"""Batch-end write hardening (fake supabase, no network).

- zone_change rows are retired: _buf_event must never queue them.
- User counter flush retries blips so a saved win row can't lose its
  counter increment ("wins not saved").
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
