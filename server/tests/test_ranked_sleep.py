"""v2.19.0 ranked sleep gate: disabled flag blocks lookup + seal."""
from server.lib import ranked as ranked_mod


def test_sleeping_blocks_lookup_and_seal(monkeypatch):
    monkeypatch.setattr(ranked_mod, "RANKED_ENABLED", False)
    assert ranked_mod.ranked_sleeping() is True
    assert ranked_mod.get_ranked_room("ABCDE") is None
    res = ranked_mod.seal_match(rid="R-X", match_version=1, gm="zw_lg",
                                winner_discord_id="K", kills={"K": 1})
    assert res == {"ok": False, "error": "ranked sleeping"}


def test_enabled_allows_lookup_shape(monkeypatch):
    monkeypatch.setattr(ranked_mod, "RANKED_ENABLED", True)
    monkeypatch.setattr(ranked_mod, "_sb", lambda: None)
    assert ranked_mod.ranked_sleeping() is False
    assert ranked_mod.get_ranked_room("ABCDE") is None  # no DB, but not sleeping
