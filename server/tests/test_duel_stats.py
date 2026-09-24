"""/wager stats — the duel record for a player.

There was no way to see your own wager history: the board and thread only
exist while a duel is live, and the coin commands show balances, not records.
`summarize_wagers` folds the bngc_duel_wagers rows (as duelist on either
side) into one record, and `wager_stats_embed` renders it. Pure functions, no
Discord, no DB.
"""
import importlib.util
import sys
from pathlib import Path


def _load_duels():
    bot_dir = str(Path("companion/bot").resolve())
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    spec = importlib.util.spec_from_file_location(
        "duels_stats_under_test", Path(bot_dir) / "duels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ME = "111"
THEM = "222"


def _wager(status="resolved", *, winner=None, stake=10, pot=20, mode="zw_lg",
           race="wins", creator=ME, opponent=THEM, sealed=None, at="2026-09-23T12:00:00+00:00"):
    return {"id": f"w-{at}-{creator}-{opponent}-{status}-{winner}",
            "status": status, "winner_id": winner, "stake": stake, "pot": pot,
            "mode": mode, "race": race, "creator_id": creator,
            "opponent_id": opponent, "sealed": sealed or [],
            "created_at": at, "resolved_at": at, "active_since": at}


def _seal(winner, kc, ko):
    return {"room_id": "r", "match_version": 0, "winner": winner,
            "winner_name": "?", "kills_c": kc, "kills_o": ko}


# --- record ----------------------------------------------------------------

def test_empty_record_is_all_zeros():
    duels = _load_duels()
    s = duels.summarize_wagers([], ME)
    assert s["total"] == 0 and s["wins"] == 0 and s["losses"] == 0
    assert s["win_rate"] == 0 and s["coins_won"] == 0 and s["coins_lost"] == 0
    assert s["kills"] == 0 and s["modes"] == {}


def test_counts_creator_wins_and_losses():
    duels = _load_duels()
    rows = [_wager(winner=ME, at="2026-09-23T12:00:00+00:00"),
            _wager(winner=THEM, at="2026-09-23T13:00:00+00:00"),
            _wager(winner=ME, at="2026-09-23T14:00:00+00:00")]
    s = duels.summarize_wagers(rows, ME)
    assert (s["total"], s["wins"], s["losses"]) == (3, 2, 1)
    assert s["win_rate"] == 67
    assert s["resolved"] == 3


def test_counts_the_opponent_side_too():
    duels = _load_duels()
    # Same rows, but the viewer is the challenged player.
    rows = [_wager(winner=THEM, creator=ME, at="2026-09-23T12:00:00+00:00"),
            _wager(winner=ME, creator=ME, at="2026-09-23T13:00:00+00:00")]
    assert duels.summarize_wagers(rows, THEM)["wins"] == 1
    assert duels.summarize_wagers(rows, THEM)["losses"] == 1


def test_cancelled_and_expired_are_neither_win_nor_loss():
    duels = _load_duels()
    rows = [_wager("cancelled", winner=None), _wager("expired", winner=None)]
    s = duels.summarize_wagers(rows, ME)
    assert s["cancelled"] == 1 and s["expired"] == 1
    assert s["wins"] == 0 and s["losses"] == 0
    # Refunded: neither won nor lost coins.
    assert s["coins_won"] == 0 and s["coins_lost"] == 0


def test_draw_when_a_resolved_wager_has_no_winner():
    duels = _load_duels()
    s = duels.summarize_wagers([_wager("resolved", winner=None)], ME)
    assert s["draws"] == 1
    assert s["wins"] == 0 and s["losses"] == 0


def test_live_wagers_are_counted_but_not_scored():
    duels = _load_duels()
    rows = [_wager("active"), _wager("open", opponent=""), _wager("pending")]
    s = duels.summarize_wagers(rows, ME)
    assert s["live"] == 3
    assert s["wins"] == 0


def test_coins_won_lost_and_net():
    duels = _load_duels()
    rows = [_wager(winner=ME, stake=10, pot=20, at="2026-09-23T12:00:00+00:00"),
            _wager(winner=THEM, stake=10, pot=20, at="2026-09-23T13:00:00+00:00")]
    s = duels.summarize_wagers(rows, ME)
    assert s["coins_won"] == 20
    assert s["coins_lost"] == 10
    assert s["coins_net"] == 10


def test_streak_and_best_streak():
    duels = _load_duels()
    rows = [
        _wager(winner=ME, at="2026-09-23T10:00:00+00:00"),
        _wager(winner=ME, at="2026-09-23T11:00:00+00:00"),
        _wager(winner=THEM, at="2026-09-23T12:00:00+00:00"),
        _wager(winner=ME, at="2026-09-23T13:00:00+00:00"),
        _wager(winner=ME, at="2026-09-23T14:00:00+00:00"),
    ]
    s = duels.summarize_wagers(rows, ME)
    assert s["streak"] == 2
    assert s["best_streak"] == 2
    rows2 = [_wager(winner=THEM, at="2026-09-23T10:00:00+00:00"),
             _wager(winner=ME, at="2026-09-23T11:00:00+00:00")]
    assert duels.summarize_wagers(rows2, ME)["streak"] == 1


def test_kills_are_counted_from_the_viewer_side():
    duels = _load_duels()
    sealed = [_seal(ME, 4, 2), _seal(THEM, 1, 5), _seal(ME, 3, 3)]
    s = duels.summarize_wagers([_wager(winner=ME, sealed=sealed)], ME)
    assert s["kills"] == 8      # 4 + 1 + 3
    assert s["kills_against"] == 10
    assert s["match_wins"] == 2
    assert s["match_losses"] == 1


def test_kills_flip_for_the_opponent_viewer():
    duels = _load_duels()
    sealed = [_seal(ME, 4, 2), _seal(THEM, 1, 5)]
    s = duels.summarize_wagers([_wager(winner=ME, sealed=sealed)], THEM)
    assert s["kills"] == 7      # 2 + 5
    assert s["kills_against"] == 5


def test_mode_breakdown():
    duels = _load_duels()
    rows = [_wager(winner=ME, mode="zw_lg", at="2026-09-23T10:00:00+00:00"),
            _wager(winner=THEM, mode="zw_lg", at="2026-09-23T11:00:00+00:00"),
            _wager(winner=ME, mode="df", at="2026-09-23T12:00:00+00:00")]
    modes = duels.summarize_wagers(rows, ME)["modes"]
    assert modes["zw_lg"] == {"w": 1, "l": 1}
    assert modes["df"] == {"w": 1, "l": 0}


def test_summarize_never_raises_on_junk():
    duels = _load_duels()
    s = duels.summarize_wagers([None, {}, {"sealed": "x", "status": None}], ME)
    assert s["total"] >= 1
    assert duels.summarize_wagers(None, None)["total"] == 0


# --- embed -----------------------------------------------------------------

def test_stats_embed_shows_record_coins_and_match_score():
    duels = _load_duels()
    s = duels.summarize_wagers(
        [_wager(winner=ME, sealed=[_seal(ME, 4, 2)]), _wager(winner=THEM)], ME)
    e = duels.wager_stats_embed("cis6led", s, balance=1234)
    fields = {f.name: f.value for f in e.fields}
    assert "cis6led" in e.title
    assert "1W - 1L" in fields["Record"] and "50%" in fields["Record"]
    assert "2 total" in fields["Duels"]
    assert "20" in fields["Coins"]
    assert "1,234" in fields["Balance"]
    # Match record, not a kill count.
    assert "Kills" not in fields
    assert "match wins" in fields["Matches"]
    assert e.footer.text


def test_stats_embed_handles_a_fresh_player():
    duels = _load_duels()
    e = duels.wager_stats_embed("newbie", duels.summarize_wagers([], "111"), balance=0)
    fields = {f.name: f.value for f in e.fields}
    assert fields["Record"]
    assert e.title


def test_stats_embed_never_raises_on_junk():
    duels = _load_duels()
    assert duels.wager_stats_embed("x", {}, None).title
    assert duels.wager_stats_embed(None, None, None).title
