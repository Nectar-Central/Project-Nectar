"""Regression: usernames containing '<' (e.g. 'jesus loves u <3') must not break
killfeed parsing. Previously any residual '<' discarded the event as 'other',
so those players' kills silently never counted (deaths still came via alv,
wins via stG — matching the mxgi_bngg report: 0 kills, 14 deaths, 35 wins).
"""
import pytest

from server.lib.gpbinary import (
    _has_tag_fragment,
    _strip_color_tags,
    parse_killfeed_text,
)


@pytest.mark.parametrize("raw", [
    "<color>jesus loves u <3</color> killed <color>Bob</color>",
    "<color=#FC2>jesus loves u <3</color> killed <color=#FC2>Bob</color>",
    "jesus loves u <3 killed Bob",
])
def test_heart_name_killer_counts_as_kill(raw):
    r = parse_killfeed_text(raw)
    assert r["type"] == "kill"
    assert r["killer"] == "jesus loves u <3"
    assert r["victim"] == "Bob"


@pytest.mark.parametrize("raw,etype", [
    ("<color>Bob</color> killed <color>jesus loves u <3</color>", "kill"),
    ("<color>Bob</color> eliminated <color>jesus loves u <3</color>", "kill"),
    ("<color>jesus loves u <3</color> knocked <color>Bob</color>", "knock"),
    ("<color>jesus loves u <3</color> eliminated <color>Bob</color>", "kill"),
])
def test_heart_name_victim_and_other_verbs(raw, etype):
    r = parse_killfeed_text(raw)
    assert r["type"] == etype
    assert "jesus loves u <3" in (r["killer"], r["victim"])


def test_plain_names_unchanged():
    r = parse_killfeed_text("<color>Alice</color> killed <color>Bob</color>")
    assert (r["type"], r["killer"], r["victim"]) == ("kill", "Alice", "Bob")


@pytest.mark.parametrize("raw", [
    "<color>Alice</color> killed <empty>",   # empty victim placeholder
    "Alice</col killed Bob",                 # truncated tag inside killer
])
def test_truncated_tags_still_dropped(raw):
    r = parse_killfeed_text(raw)
    assert r["type"] == "other"
    assert r["killer"] is None and r["victim"] is None


def test_strip_preserves_heart_but_removes_tags():
    assert _strip_color_tags("<color>jesus loves u <3</color>") == "jesus loves u <3"
    assert _strip_color_tags("<b>jesus loves u <3</b>") == "jesus loves u <3"
    assert _strip_color_tags("<color>Alice</color>") == "Alice"


@pytest.mark.parametrize("s,expected", [
    ("jesus loves u <3", False),
    ("Alice", False),
    ("a>b", False),
    ("Bob</col", True),
    ("Bob<tr", True),
    ("<color>Alice", True),
    ("Alice<", True),
])
def test_has_tag_fragment(s, expected):
    assert _has_tag_fragment(s) is expected
