"""Unit tests for /api/players dedup (pure, no DB).

Regression: the same person (mobile + desktop, ghost actors) appeared
twice — e.g. 'cis6led' and 'chris7' rows sharing one discord_id/game_id.
"""
from server.routes.players import dedup_players


def _row(username, game_id=None, discord_id=None, discord_username=None,
         is_self=False, is_alive=True, in_db=False):
    return {"username": username, "game_id": game_id, "skin": None,
            "discord_username": discord_username, "discord_id": discord_id,
            "is_self": is_self, "is_alive": is_alive, "pt": None,
            "late_join": False, "in_db": in_db}


def test_same_discord_collapses_to_one():
    rows = [_row("cis6led", game_id="E7885DD05E68C39F", discord_id="1",
                 discord_username="cis6led", in_db=True),
            _row("chris7", game_id="E7885DD05E68C39F", discord_id="1",
                 discord_username="cis6led", in_db=True)]
    out = dedup_players(rows)
    assert len(out) == 1
    assert out[0]["discord_id"] == "1"


def test_same_game_id_without_discord_collapses():
    rows = [_row("A", game_id="G1"), _row("B", game_id="G1")]
    assert len(dedup_players(rows)) == 1


def test_distinct_people_kept():
    rows = [_row("A", game_id="G1", discord_id="1"),
            _row("B", game_id="G2", discord_id="2")]
    assert len(dedup_players(rows)) == 2


def test_identity_less_rows_never_collapse():
    rows = [_row(None), _row(""), _row("null")]
    assert len(dedup_players(rows)) == 3


def test_winner_prefers_self_then_alive():
    ghost = _row("G", game_id="G9", is_alive=False)
    me = _row("M", game_id="G9", is_self=True, is_alive=False)
    out = dedup_players([ghost, me])
    assert len(out) == 1
    assert out[0]["is_self"] is True


def test_missing_discord_merges_from_loser():
    bare = _row("A", game_id="G1", is_alive=True)
    rich = _row("B", game_id="G1", discord_id="7",
                discord_username="seven", is_alive=False)
    out = dedup_players([bare, rich])
    assert len(out) == 1
    assert out[0]["discord_id"] == "7"
    assert out[0]["is_alive"] is True
