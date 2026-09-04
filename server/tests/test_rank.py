"""Unit tests for the kills percentile behind 'better than X% of players'."""
from server.routes.stats import better_than_pct


def test_best_of_100():
    assert better_than_pct(100, 0) == 99


def test_mid_pack():
    assert better_than_pct(100, 49) == 50


def test_last_place_is_zero():
    assert better_than_pct(100, 99) == 0


def test_tiny_pool():
    assert better_than_pct(2, 0) == 50
    assert better_than_pct(2, 1) == 0


def test_degenerate_inputs():
    assert better_than_pct(0, 0) == 0
    assert better_than_pct(1, 0) == 0
    assert better_than_pct(100, 100) == 0
    assert better_than_pct(None, None) == 0
