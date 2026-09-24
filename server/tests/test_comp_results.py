"""Comp tourney final results must be postable to the results channel.

`/tourney-stop` closes a tourney and used to print nothing but an ephemeral
confirmation, so the final standings of a finished comp tourney only existed
in a command reply nobody kept. These embeds are the archive: a header embed
plus 10 players per embed, addressed to the results channel, built purely so
the layout is testable without Discord.
"""
import importlib.util
from pathlib import Path

import pytest


def _load():
    spec = importlib.util.spec_from_file_location(
        "comp_results_under_test", Path("companion/bot/comp_results.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RESULTS_CHANNEL = "1512793471440916564"


def _board(n, *, points=None, wins=None):
    return [{"discord_id": str(1000 + i), "name": f"Player{i}",
             "points": (points if points is not None else 40 - i),
             "kills": 12 - (i % 5), "wins": (wins if wins is not None else 3 - (i % 3)),
             "matches": 6, "avg_placement": 2.5}
            for i in range(n)]


def _tourney(**extra):
    t = {"id": 12, "name": "Friday Cup", "status": "ended",
         "sealed": 7, "series_len": 7, "mode": "solo"}
    t.update(extra)
    return t


# --- channel ---------------------------------------------------------------

def test_results_channel_defaults_to_the_configured_channel(monkeypatch):
    mod = _load()
    monkeypatch.delenv("COMP_RESULTS_CHANNEL_ID", raising=False)
    assert mod.results_channel_id() == RESULTS_CHANNEL


def test_results_channel_is_env_overridable(monkeypatch):
    mod = _load()
    monkeypatch.setenv("COMP_RESULTS_CHANNEL_ID", "42")
    assert mod.results_channel_id() == "42"
    # A blank value must not produce an unresolvable channel id.
    monkeypatch.setenv("COMP_RESULTS_CHANNEL_ID", "  ")
    assert mod.results_channel_id() == RESULTS_CHANNEL


# --- rows ------------------------------------------------------------------

def test_board_lines_number_globally_with_podium_medals():
    mod = _load()
    lines = mod.board_lines(_board(4))
    assert lines[0].startswith("🥇 <@1000> **Player0**")
    assert lines[1].startswith("🥈 ")
    assert lines[2].startswith("🥉 ")
    assert lines[3].startswith("4. ")
    assert "40 pts" in lines[0] and "12K" in lines[0] and "3W" in lines[0]


def test_board_lines_survive_rows_without_a_discord_id():
    mod = _load()
    lines = mod.board_lines([{"name": "Ghost", "points": 3, "kills": 1, "wins": 0}])
    assert lines and "Ghost" in lines[0] and "<@" not in lines[0]


def test_board_lines_never_raise_on_junk():
    mod = _load()
    assert mod.board_lines(None) == []
    assert mod.board_lines([{"points": "x"}, None, 7]) is not None


# --- embed layout ----------------------------------------------------------

def test_single_embed_for_a_small_roster():
    mod = _load()
    embeds = mod.results_embeds(_tourney(), _board(6))
    assert len(embeds) == 1
    e = embeds[0]
    assert "Friday Cup" in e["title"] and "#12" in e["title"]
    assert "6 players" in e["footer"] and "7/7" in e["footer"]
    assert "🥇" in e["description"]
    assert e["color"] == 0xFFD700


def test_results_are_chunked_by_ten():
    mod = _load()
    embeds = mod.results_embeds(_tourney(), _board(23))
    assert len(embeds) == 3
    # description = header line, blank line, then the chunk's rows
    counts = [len(e["description"].split("\n")) - 2 for e in embeds]
    assert counts == [10, 10, 3]
    # Placement numbering stays global across chunks.
    assert "🥇" in embeds[0]["description"]
    assert "11. <@1010>" in embeds[1]["description"]
    assert "21. <@1020>" in embeds[2]["description"]
    for i, e in enumerate(embeds, 1):
        assert f"{i}/3" in e["footer"]


def test_header_carries_tourney_facts():
    mod = _load()
    embeds = mod.results_embeds(_tourney(sealed=5, series_len=0), _board(2))
    head = embeds[0]["description"].split("\n")[0]
    assert "Friday Cup" in head
    assert "5/∞" in embeds[0]["footer"]


def test_no_standings_yields_no_embeds():
    mod = _load()
    assert mod.results_embeds(_tourney(), []) == []
    assert mod.results_embeds(_tourney(), None) == []
    assert mod.results_embeds({}, []) == []


def test_results_embeds_never_exceed_the_discord_description_limit():
    mod = _load()
    # Pathological names must not silently produce an unsendable embed.
    board = [{"discord_id": str(2000 + i), "name": "N" * 200, "points": 5,
              "kills": 1, "wins": 0} for i in range(40)]
    for e in mod.results_embeds(_tourney(), board):
        assert len(e["description"]) <= 4096
        assert len(e["title"]) <= 256


def test_results_embeds_never_raise_on_junk():
    mod = _load()
    assert mod.results_embeds(None, None) == []
    assert mod.results_embeds({"name": None}, [{}]) is not None


# --- posting ---------------------------------------------------------------

class _Channel:
    def __init__(self, fail_at=None):
        self.sent = []
        self.fail_at = fail_at

    async def send(self, *, embed=None, **kw):
        if self.fail_at is not None and len(self.sent) == self.fail_at:
            raise RuntimeError("rate limited")
        self.sent.append(embed)


class _Bot:
    def __init__(self, channel):
        self.channel = channel
        self.fetched = []

    def get_channel(self, cid):
        return self.channel

    async def fetch_channel(self, cid):
        self.fetched.append(cid)
        return self.channel


async def _fetch_ok(tid):
    return {"tourney": _tourney(), "standings": _board(12)}


@pytest.mark.asyncio
async def test_post_results_sends_every_chunk_to_the_results_channel():
    mod = _load()
    ch = _Channel()
    bot = _Bot(ch)
    slept = []

    async def _sleep(s):
        slept.append(s)
    sent, note = await mod.post_results(bot, 12, _fetch_ok, sleep=_sleep)
    assert sent == 2
    assert "2 embed" in note
    assert len(ch.sent) == 2
    assert ch.sent[0].title.startswith("🏆 Friday Cup")
    # One pause between chunks only — never after the last one.
    assert slept == [2]


@pytest.mark.asyncio
async def test_post_results_resolves_a_missing_channel_by_fetching_it():
    mod = _load()
    ch = _Channel()
    bot = _Bot(ch)
    bot.get_channel = lambda cid: None
    sent, _note = await mod.post_results(bot, 12, _fetch_ok, sleep=_no_sleep)
    assert sent == 2
    assert bot.fetched == [int(mod.results_channel_id())]


@pytest.mark.asyncio
async def test_post_results_reports_an_unreachable_server():
    mod = _load()

    async def _fetch_bad(_tid):
        return {"error": "boom"}
    sent, note = await mod.post_results(_Bot(_Channel()), 12, _fetch_bad)
    assert sent == 0
    assert "boom" in note


@pytest.mark.asyncio
async def test_post_results_skips_an_empty_board():
    mod = _load()

    async def _fetch_empty(_tid):
        return {"tourney": _tourney(), "standings": []}
    ch = _Channel()
    sent, note = await mod.post_results(_Bot(ch), 12, _fetch_empty)
    assert sent == 0 and "no sealed matches" in note
    assert ch.sent == []


@pytest.mark.asyncio
async def test_post_results_stops_cleanly_when_a_send_fails():
    mod = _load()
    ch = _Channel(fail_at=1)
    sent, note = await mod.post_results(_Bot(ch), 12, _fetch_ok, sleep=_no_sleep)
    assert sent == 1
    assert "rate limited" in note


@pytest.mark.asyncio
async def test_post_results_rejects_a_non_numeric_channel():
    mod = _load()
    sent, note = await mod.post_results(
        _Bot(_Channel()), 12, _fetch_ok, channel_id="not-an-id")
    assert sent == 0
    assert "channel id" in note


@pytest.mark.asyncio
async def test_post_results_never_raises():
    mod = _load()

    async def _fetch_boom(_tid):
        raise RuntimeError("network down")
    sent, note = await mod.post_results(_Bot(_Channel()), 12, _fetch_boom)
    assert sent == 0 and note


async def _no_sleep(_s):
    return None
