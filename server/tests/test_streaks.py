"""v2.19.0 streak math: daily rollover, win/kill thresholds."""
from datetime import date, timedelta

from server.lib import streaks as st


def test_daily_first_claim():
    s, bonus, claimed = st.daily_update(None, date(2026, 9, 8))
    assert (s, bonus, claimed) == (1, 10, True)


def test_daily_same_day_no_double():
    assert st.daily_update(date(2026, 9, 8), date(2026, 9, 8))[2] is False


def test_daily_continue():
    assert st.daily_continue(2) == (3, 25)
    assert st.daily_continue(6) == (7, 75)


def test_daily_gap_resets():
    s, bonus, claimed = st.daily_update(date(2026, 9, 1), date(2026, 9, 8))
    assert (s, bonus, claimed) == (1, 10, True)


def test_win_thresholds():
    assert st.win_bonus(2) == 20
    assert st.win_bonus(3) == 50
    assert st.win_bonus(5) == 150
    assert st.win_bonus(4) == 0


def test_kill_thresholds():
    assert st.kill_bonus(5) == 15
    assert st.kill_bonus(10) == 50
    assert st.kill_bonus(4) == 0


def test_bad_inputs_never_raise():
    assert st.win_bonus("bad") == 0
    assert st.kill_bonus(None) == 0
    assert st.daily_continue("bad")[0] == 1
