"""Wager embeds talk score only — the word "kill" never appears in them.

Kills decide Free Mode (df) duels internally, but showing a kill count in the
board, the thread embed or the sealed-match post confused players: a bf/zw_lg
duel displayed a "Kills 2 - 7" row that decided nothing, and a per-match
"0 kills · 1 kills" line read as if it were the score. Every wager embed now
reports the score and nothing else; a kills race shows its kill totals as the
score, since that IS the objective for df.

This asserts on the rendered embed (title, description, every field name and
value, footer) rather than on the source, so it holds no matter which builder
adds the text. Pure embed builders, no Discord gateway.
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
        "duels_nokills_under_test", Path(bot_dir) / "duels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def duels():
    return _load_duels()


def _wager(status, **extra):
    w = {"id": "6c447ecc-1111-2222-3333-444455556666", "kind": "targeted",
         "status": status, "stake": 5, "pot": 10, "mode": "bf",
         "first_to": 3, "race": "wins", "creator_id": "111",
         "opponent_id": "222", "score_creator": 2, "score_opponent": 4,
         "kills_creator": 2, "kills_opponent": 7, "is_mobile_duel": True,
         "sealed": [{"winner_name": "moken trol chris",
                      "kills_c": 0, "kills_o": 1}]}
    w.update(extra)
    return w


def _rendered(embed) -> str:
    """Every user-visible string on the embed, joined."""
    parts = [embed.title or "", embed.description or ""]
    parts += [f.name or "" for f in embed.fields]
    parts += [f.value or "" for f in embed.fields]
    parts += [(embed.footer.text or "") if embed.footer else ""]
    return "\n".join(parts)


def _assert_score_only(embed, label):
    text = _rendered(embed)
    assert "kill" not in text.lower(), f"{label} still mentions kills:\n{text}"


@pytest.mark.parametrize("status", ["open", "pending", "active", "resolved",
                                    "cancelled", "expired"])
def test_board_embed_never_mentions_kills(duels, status):
    _assert_score_only(duels.challenge_embed(_wager(status), "moken", "cis62led"),
                       f"challenge_embed[{status}]")


@pytest.mark.parametrize("status", ["active", "resolved"])
def test_score_embed_never_mentions_kills(duels, status):
    _assert_score_only(duels.score_embed(_wager(status), "moken", "cis62led"),
                       f"score_embed[{status}]")


def test_sealed_match_post_never_mentions_kills(duels):
    seal = {"winner_name": "cis62led", "kills_c": 0, "kills_o": 1}
    _assert_score_only(duels.sealed_match_embed(2, seal, "moken", "cis62led"),
                       "sealed_match_embed")


def test_stats_embed_never_mentions_kills(duels):
    s = {"wins": 4, "losses": 2, "total": 6, "kills": 31, "kills_against": 12,
         "match_wins": 9, "match_losses": 5, "coins_won": 40, "coins_lost": 10}
    _assert_score_only(duels.wager_stats_embed("moken", s), "wager_stats_embed")


def test_kills_race_still_reports_its_score(duels):
    """df scores on kills — that total IS the score, so it stays visible as the
    Score field; only the separate 'Kills' row and the word 'kills' go."""
    w = _wager("active", race="kills", mode="df", first_to=5,
               score_creator=0, score_opponent=0,
               kills_creator=4, kills_opponent=2)
    e = duels.score_embed(w, "moken", "cis62led")
    score = [f for f in e.fields if f.name == "Score"][0]
    assert "4" in score.value and "2" in score.value
    _assert_score_only(e, "score_embed[kills race]")
