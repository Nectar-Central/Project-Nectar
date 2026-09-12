"""Supabase singleton proxy: import-time holders must survive refresh.

Regression: refresh_client() used to swap the module global for a new object
while ~10 modules still held the old (closed) httpx session -> permanent
"client has been closed" failures (stats 500s, processor get-user failures)
until restart.
"""
from server.lib import supabase_client as sc


class _FakeTable:
    def __init__(self, tag):
        self._tag = tag

    def select(self, *a, **k):
        return self

    def execute(self):
        return {"tag": self._tag}


class _FakeClient:
    def __init__(self, tag):
        self._tag = tag

    def table(self, name):
        return _FakeTable(self._tag)


def test_proxy_forwards_to_current_target():
    p = sc._SupabaseProxy()
    assert not p  # unconfigured -> falsy
    p._set(_FakeClient("one"))
    assert p
    assert p.table("t").execute() == {"tag": "one"}
    # import-time holder keeps working after repoint (the refresh case)
    holder = p
    p._set(_FakeClient("two"))
    assert holder.table("t").execute() == {"tag": "two"}


def test_proxy_unconfigured_guards():
    p = sc._SupabaseProxy()
    assert not p
    try:
        p.table("t")
        raised = False
    except AttributeError:
        raised = True
    assert raised


def test_closed_client_is_transient():
    assert sc.is_transient_error("Cannot send a request, as the client has been closed.")
    assert sc.is_transient_error("Server disconnected")
    assert not sc.is_transient_error("PGRST116 no rows")


def test_module_singleton_is_proxy():
    assert isinstance(sc.supabase, sc._SupabaseProxy)
