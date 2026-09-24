"""A finished wager must not still read like a live one.

The board embed kept its `Duel wager <id> — N coins each` title (and its
"Accept or Decline below" footer) after the duel resolved, was forfeited or
was cancelled, so a closed wager looked joinable. Both embeds now read
"Wager ended" for resolved / cancelled / expired.
Pure embed builders, no Discord gateway.
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
        "duels_embeds_under_test", Path(bot_dir) / "duels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def duels():
    return _load_duels()


def _wager(status, **extra):
    w = {"id": "6c447ecc-1111-2222-3333-444455556666", "kind": "targeted",
         "status": status, "stake": 2, "pot": 4, "mode": "zw_lg",
         "first_to": 5, "race": "wins", "creator_id": "111",
         "opponent_id": "222", "score_creator": 0, "score_opponent": 0,
         "kills_creator": 0, "kills_opponent": 0}
    w.update(extra)
    return w


def test_ended_statuses_are_recognised():
    duels = _load_duels()
    for st in ("resolved", "cancelled", "expired"):
        assert duels._is_ended(_wager(st)) is True
        assert duels._is_ended(_wager(st.upper())) is True
    for st in ("open", "pending", "active", "", None):
        assert duels._is_ended(_wager(st)) is False
    assert duels._is_ended(None) is False


def test_board_embed_reads_wager_ended_when_finished():
    duels = _load_duels()
    for st in ("resolved", "cancelled", "expired"):
        e = duels.challenge_embed(_wager(st), "cis6led", "chris7")
        assert e.title == "Wager ended"
        # No stale "Accept or Decline below" on a dead wager.
        assert "Accept or Decline" not in (e.footer.text or "")
        assert "Join below" not in (e.footer.text or "")


def test_board_embed_keeps_live_titles_while_playable():
    duels = _load_duels()
    open_e = duels.challenge_embed(_wager("open"), "cis6led", None)
    assert "Duel wager" in open_e.title
    assert "OPEN" in (open_e.description or "")
    pending = duels.challenge_embed(_wager("pending"), "cis6led", "chris7")
    assert "Duel wager" in pending.title
    live = duels.challenge_embed(_wager("active"), "cis6led", "chris7")
    assert "Duel wager" in live.title
    assert "duel live" in (live.footer.text or "").lower()


def test_for_fun_duel_title_before_ending_is_kept():
    duels = _load_duels()
    e = duels.challenge_embed(_wager("open", stake=0, pot=0),
                              "cis6led", "chris7")
    assert e.title.startswith("For fun duel")


def test_thread_score_embed_reads_wager_ended_when_finished():
    duels = _load_duels()
    for st in ("resolved", "cancelled", "expired"):
        e = duels.score_embed(_wager(st), "cis6led", "chris7")
        assert e.title == "Wager ended"


def test_thread_score_embed_keeps_titles_while_live():
    duels = _load_duels()
    assert duels.score_embed(_wager("active"), "a", "b").title == (
        "Nectar Coins Wager `6c447ecc`")
    assert duels.score_embed(_wager("active", pot=0), "a", "b").title == (
        "For fun duel `6c447ecc`")


def test_embeds_never_raise_on_a_broken_wager():
    duels = _load_duels()
    assert duels.challenge_embed({}, "a", "b").title
    assert duels.score_embed({}, "a", "b").title


# --- rules channel ---------------------------------------------------------

RULES_CHANNEL = "1552686996420427786"


def _field(embed, name):
    for f in embed.fields:
        if f.name == name:
            return f
    return None


def test_rules_channel_id_defaults_to_the_wager_rules_channel(duels, monkeypatch):
    monkeypatch.delenv("WAGER_RULES_CHANNEL_ID", raising=False)
    assert duels._rules_channel_id() == RULES_CHANNEL


def test_rules_channel_id_is_env_overridable(duels, monkeypatch):
    monkeypatch.setenv("WAGER_RULES_CHANNEL_ID", "12345")
    assert duels._rules_channel_id() == "12345"
    # A blank value must not produce an empty, broken mention.
    monkeypatch.setenv("WAGER_RULES_CHANNEL_ID", "   ")
    assert duels._rules_channel_id() == RULES_CHANNEL


def test_rules_field_points_at_the_channel_and_asks_to_read_it(duels, monkeypatch):
    monkeypatch.delenv("WAGER_RULES_CHANNEL_ID", raising=False)
    for embed in (duels.challenge_embed(_wager("open"), "cis6led", None),
                  duels.score_embed(_wager("active"), "cis6led", "chris7")):
        f = _field(embed, "📜 Rules")
        assert f is not None
        assert f"<#{RULES_CHANNEL}>" in f.value
        assert "before" in f.value.lower()


def test_rules_field_survives_on_ended_wagers(duels, monkeypatch):
    monkeypatch.delenv("WAGER_RULES_CHANNEL_ID", raising=False)
    e = duels.challenge_embed(_wager("cancelled"), "cis6led", "chris7")
    assert _field(e, "📜 Rules") is not None


# --- board embed refresh ----------------------------------------------------

class _Msg:
    def __init__(self):
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)


class _Channel:
    def __init__(self, msg):
        self.msg = msg
        self.fetched = []

    async def fetch_message(self, mid):
        self.fetched.append(mid)
        return self.msg


class _Bot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, cid):
        return self.channel


async def _fresh(row):
    return dict(row)


def _board_wager(**extra):
    w = _wager("active", channel_id="555", message_id="666",
               thread_id="777", creator_id="111", opponent_id="222")
    w.update(extra)
    return w


@pytest.mark.asyncio
async def test_refresh_board_embed_edits_the_stored_board_message(duels, monkeypatch):
    msg = _Msg()
    ch = _Channel(msg)
    bot = _Bot(ch)
    w = _board_wager()
    monkeypatch.setattr(duels, "get_wager", _fresh)
    ok = await duels.refresh_board_embed(bot, w)
    assert ok is True
    assert ch.fetched == [666]
    assert len(msg.edits) == 1
    embed = msg.edits[0]["embed"]
    assert embed.title.startswith("Duel wager")
    # A live wager keeps no buttons; a dead one can never look joinable.
    assert msg.edits[0].get("view") is None


@pytest.mark.asyncio
async def test_refresh_board_embed_renders_the_ended_state(duels, monkeypatch):
    msg = _Msg()
    bot = _Bot(_Channel(msg))
    ended = _board_wager(status="resolved", winner_id="111",
                         score_creator=5, score_opponent=3)
    monkeypatch.setattr(duels, "get_wager", _fresh)
    assert await duels.refresh_board_embed(bot, ended) is True
    embed = msg.edits[0]["embed"]
    assert embed.title == "Wager ended"
    assert (embed.footer.text or "").startswith("Wager ended")


@pytest.mark.asyncio
async def test_refresh_board_embed_is_a_noop_without_ids(duels, monkeypatch):
    called = []

    async def _should_not_run(_w):
        called.append(1)
        return _w
    monkeypatch.setattr(duels, "get_wager", _should_not_run)
    assert await duels.refresh_board_embed(_Bot(_Channel(_Msg())),
                                           _board_wager(channel_id="", message_id="")) is False
    assert await duels.refresh_board_embed(None, _board_wager()) is False
    assert called == []


@pytest.mark.asyncio
async def test_refresh_board_embed_never_raises(duels, monkeypatch):
    monkeypatch.setattr(duels, "get_wager", _fresh)

    class _Boom:
        def get_channel(self, cid):
            raise RuntimeError("gone")

        async def fetch_channel(self, cid):
            raise RuntimeError("gone")
    assert await duels.refresh_board_embed(_Boom(), _board_wager()) is False
    assert await duels.refresh_board_embed(_Bot(None), _board_wager()) is False


# --- kills and wins counts (no "K" suffix) ---------------------------------

def test_no_embed_uses_a_bare_k_suffix_for_kills(duels):
    import re
    live = _wager("active", score_creator=2, score_opponent=1,
                  kills_creator=9, kills_opponent=7, sealed=[
                      {"room_id": "r", "match_version": 0, "winner": "111",
                       "winner_name": "cis6led", "kills_c": 3, "kills_o": 1}])
    texts = []
    for e in (duels.challenge_embed(live, "cis6led", "chris7"),
              duels.score_embed(live, "cis6led", "chris7"),
              duels.sealed_match_embed(1, live["sealed"][0], "cis6led", "chris7")):
        texts.append(e.title or "")
        texts.append(e.description or "")
        for f in e.fields:
            texts.append(f"{f.name} {f.value}")
    blob = "\n".join(texts)
    # No "12K" / "12 K" style kill counts anywhere.
    assert not re.search(r"\d+\s*K\b", blob)
    assert not re.search(r"\d+-\d+\s*K\b", blob)


def test_board_embed_shows_the_running_score(duels):
    w = _wager("active", score_creator=2, score_opponent=1,
               kills_creator=9, kills_opponent=7)
    e = duels.challenge_embed(w, "cis6led", "chris7")
    score = _field(e, "Score")
    assert score is not None and "2" in score.value and "1" in score.value
    # Score only — a kill count that decided nothing is not shown.
    assert _field(e, "Kills") is None


def test_board_embed_scores_kills_for_a_kills_race(duels):
    w = _wager("active", race="kills", kills_creator=9, kills_opponent=7)
    e = duels.challenge_embed(w, "cis6led", "chris7")
    # Score IS the kills race objective — reported there, and nowhere else.
    score = _field(e, "Score")
    assert score is not None and "9" in score.value and "7" in score.value
    assert _field(e, "Kills") is None


def test_board_embed_has_no_score_before_the_duel_starts(duels):
    e = duels.challenge_embed(_wager("open"), "cis6led", None)
    assert _field(e, "Score") is None
    assert _field(e, "Kills") is None


def test_ended_board_embed_still_shows_the_final_score(duels):
    w = _wager("resolved", winner_id="111", score_creator=5,
               score_opponent=3, kills_creator=12, kills_opponent=9)
    e = duels.challenge_embed(w, "cis6led", "chris7")
    assert e.title == "Wager ended"
    assert "5" in _field(e, "Score").value
    assert _field(e, "Kills") is None


def test_thread_score_embed_lists_sealed_winners_without_kills(duels):
    w = _wager("active", score_creator=2, score_opponent=1,
               kills_creator=9, kills_opponent=7, sealed=[
                   {"winner_name": "cis6led", "kills_c": 3, "kills_o": 1}])
    e = duels.score_embed(w, "cis6led", "chris7")
    assert _field(e, "Kills") is None
    sealed = _field(e, "Sealed matches")
    assert "cis6led" in sealed.value
    assert "3-1" not in sealed.value


def test_sealed_match_embed_reports_only_the_winner(duels):
    e = duels.sealed_match_embed(
        2, {"winner_name": "chris7", "kills_c": 4, "kills_o": 6},
        "cis6led", "chris7")
    assert e.title == "🔒 Sealed — Match 2"
    assert "chris7" in e.description
    assert "4" not in e.description and "6" not in e.description

