"""Play-to-earn pays at most 50 Nectar Coins per day.

The kill/win faucet used to hand out 500/day, which made wager coins (capped
at 50/day of profit) pointless next to it. The cap is the faucet ceiling:
everything above it is forfeited, and it must stay at 50 unless someone
deliberately raises EARN_DAILY_CAP in the environment.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest


def _load_earn(monkeypatch):
    bot_dir = str(Path("companion/bot").resolve())
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    monkeypatch.delenv("EARN_DAILY_CAP", raising=False)
    spec = importlib.util.spec_from_file_location(
        "earn_under_test", Path(bot_dir) / "earn.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_default_daily_cap_is_50(monkeypatch):
    earn = _load_earn(monkeypatch)
    assert earn.EARN_DAILY_CAP == 50


def test_cap_credit_defaults_to_the_50_ceiling(monkeypatch):
    earn = _load_earn(monkeypatch)
    # A big batch on a fresh day is cut to 50.
    assert earn.cap_credit(500, 0) == 50
    # Only the remaining room is payable.
    assert earn.cap_credit(30, 20) == 30
    assert earn.cap_credit(30, 40) == 10
    # Nothing left today.
    assert earn.cap_credit(30, 50) == 0
    assert earn.cap_credit(30, 999) == 0


def test_env_still_overrides_the_cap(monkeypatch):
    bot_dir = str(Path("companion/bot").resolve())
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    monkeypatch.setenv("EARN_DAILY_CAP", "500")
    spec = importlib.util.spec_from_file_location(
        "earn_override_under_test", Path(bot_dir) / "earn.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.EARN_DAILY_CAP == 500
    assert mod.cap_credit(500, 0) == 500


def test_kill_and_win_rates_are_unchanged(monkeypatch):
    earn = _load_earn(monkeypatch)
    assert earn.credit_for_events(
        [{"event_type": "kill"}, {"event_type": "win"}]) == (1, 1, 3)
    assert os.getenv("EARN_DAILY_CAP") is None
