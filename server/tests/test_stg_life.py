"""Unit tests for the stG new-life rule (double-defeat regression).

Bug: resetting per-life flags at the stG=3 version bump wiped the just-ended
match's guards before its drain ran, so the sweep re-marked and the drain
re-wrote its death+defeat (double defeat in zw_lg after a killfeed elim).
Flags must reset only on a fresh stG=1 (match start), edge-triggered.
"""
import pytest

from server.lib.photon_engine.engine import _stg_starts_new_life


@pytest.mark.parametrize("stg,last,expected", [
    (1, None, True),    # first match start
    (1, 0, True),
    (1, 2, True),
    (1, 3, True),       # normal 3 -> 1 new match
    (1, 1, False),      # repeat must not wipe a life mid-flight
    (2, 1, False),
    (3, 2, False),      # match end: never reset here
    (3, None, False),
    (0, None, False),
    ("1", 3, True),
    ("bad", None, False),
    (None, None, False),
])
def test_new_life_matrix(stg, last, expected):
    assert _stg_starts_new_life(stg, last) is expected
