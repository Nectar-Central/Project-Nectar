"""Regression: tag-wrapped player names must not leak into stored usernames.

Report: drawer showed 'Game username <color=black>Nryx!hhhj' — the raw
playerProps 255 value flowed through view.me.username into bngc_users.
Fix: _clean_username at every engine ingestion point + strip in the
processor user-sync (which also heals rows dirtied before the fix).

The '<3' counterpart (jesus loves u <3) must keep working — the stripper
has no generic <...> fallback on purpose.
"""
import pytest

from server.lib.photon_engine.engine import _clean_username


@pytest.mark.parametrize("raw,expected", [
    ("<color=black>Nryx!hhhj", "Nryx!hhhj"),          # the reported case (unclosed tag)
    ("<color=black>Nryx!hhhj</color>", "Nryx!hhhj"),  # closed variant
    ("<color=#FC2>Bob</color>", "Bob"),               # hex variant
    ("<b>Alice</b>", "Alice"),                        # other formatting tag
    ("Bob", "Bob"),                                   # plain untouched
    ("jesus loves u <3", "jesus loves u <3"),         # innocent '<' preserved
    ("<color>jesus loves u <3</color>", "jesus loves u <3"),
    ("its_bubba11", "its_bubba11"),
])
def test_clean_username(raw, expected):
    assert _clean_username(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "<color>", "<color=red>"])
def test_clean_username_never_empties(raw):
    # a name that strips to nothing keeps its original (never store "")
    out = _clean_username(raw)
    assert isinstance(out, str)
    if raw:
        assert out == raw


def test_clean_username_never_raises():
    assert _clean_username(12345) == "12345"
    assert _clean_username(object()) != ""
