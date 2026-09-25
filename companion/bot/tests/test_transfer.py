"""Tests for the /admin transfer planner (companion/bot/transfer.py).

The planner is the whole decision - these pin the per-field merge rules, the
refusals, and the invariant that the source is left empty.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transfer import (  # noqa: E402
    EARLIEST_FIELDS, MAX_FIELDS, PROFILE_FIELDS, SOURCE_RESET_DEFAULTS,
    SUM_FIELDS, TransferRefused, plan_transfer,
)

NOW = "2026-01-02T03:04:05+00:00"


def _src(**over):
    row = {"discord_id": "111", "token": "t1", "game_username": "OldName",
           "game_id": "g1", "skin": "S1", "pickaxe": "P1",
           "kills": 500, "deaths": 400, "wins": 30, "defeats": 20,
           "playtime_global": 36000, "playtime_per_gm": {"public": 3600},
           "win_streak": 4, "best_win_streak": 9, "best_match_kills": 25,
           "play_points": 1200, "play_points_hours_paid": 10,
           "lobby_music_url": "https://cdn/old.mp3", "lobby_music_volume": 0.8,
           "nectar_coins": 5000, "nectar_total_earned": 9000,
           "nectar_total_wagered": 4000, "daily_streak": 6,
           "tourney_pr": 42.5, "first_seen": "2024-01-01T00:00:00+00:00",
           "registered_at": "2024-01-01T00:00:00+00:00",
           "last_daily_claim": "2026-01-01T00:00:00+00:00"}
    row.update(over)
    return row


def _dst(**over):
    row = {"discord_id": "222", "token": "t2", "game_username": "NewName",
           "game_id": "g2", "skin": "S2", "pickaxe": "P2",
           "kills": 10, "deaths": 5, "wins": 1, "defeats": 0,
           "playtime_global": 600, "playtime_per_gm": {"public": 600, "duos": 300},
           "win_streak": 1, "best_win_streak": 2, "best_match_kills": 5,
           "play_points": 100, "play_points_hours_paid": 1,
           "nectar_coins": 100, "nectar_total_earned": 100,
           "nectar_total_wagered": 0, "daily_streak": 1,
           "tourney_pr": 30.0, "first_seen": "2025-06-01T00:00:00+00:00",
           "registered_at": "2025-06-01T00:00:00+00:00",
           "last_daily_claim": "2026-01-02T00:00:00+00:00"}
    row.update(over)
    return row


class TestMerges:
    def test_totals_are_summed(self):
        p = plan_transfer(_src(), _dst(), now_iso=NOW)["target"]
        assert p["kills"] == 510
        assert p["deaths"] == 405
        assert p["wins"] == 31
        assert p["playtime_global"] == 36600
        assert p["play_points"] == 1300

    def test_wallet_moves_without_doubling(self):
        p = plan_transfer(_src(), _dst(), now_iso=NOW)
        assert p["target"]["nectar_coins"] == 5100
        assert p["source"]["nectar_coins"] == 0

    def test_records_and_ratings_take_the_better(self):
        p = plan_transfer(_src(), _dst(), now_iso=NOW)["target"]
        assert p["win_streak"] == 4
        assert p["best_win_streak"] == 9
        assert p["best_match_kills"] == 25
        assert float(p["tourney_pr"]) == 42.5

    def test_rating_does_not_regress_when_target_is_higher(self):
        p = plan_transfer(_src(), _dst(tourney_pr=99.0), now_iso=NOW)["target"]
        assert float(p["tourney_pr"]) == 99.0

    def test_account_age_is_the_earliest(self):
        p = plan_transfer(_src(), _dst(), now_iso=NOW)["target"]
        assert p["first_seen"] == "2024-01-01T00:00:00+00:00"
        assert p["registered_at"] == "2024-01-01T00:00:00+00:00"

    def test_daily_claim_stamp_cannot_be_circumvented(self):
        p = plan_transfer(_src(), _dst(), now_iso=NOW)["target"]
        assert p["last_daily_claim"] == "2026-01-01T00:00:00+00:00"

    def test_playtime_map_merges_per_mode(self):
        p = plan_transfer(_src(), _dst(), now_iso=NOW)["target"]
        assert p["playtime_per_gm"] == {"public": 4200, "duos": 300}

    def test_identity_is_taken_from_the_source(self):
        p = plan_transfer(_src(), _dst(), now_iso=NOW)["target"]
        assert p["game_username"] == "OldName"
        assert p["game_id"] == "g1"
        assert p["skin"] == "S1"
        assert p["pickaxe"] == "P1"

    def test_every_field_family_is_represented(self):
        p = plan_transfer(_src(), _dst(), now_iso=NOW)["target"]
        for f in SUM_FIELDS + PROFILE_FIELDS:
            assert f in p
        for f in MAX_FIELDS:
            if f not in ("tourney_pr", "daily_streak"):
                assert f in p
        for f in EARLIEST_FIELDS:
            assert f in p


class TestSourceEmptied:
    def test_counters_zeroed(self):
        s = plan_transfer(_src(), _dst(), now_iso=NOW)["source"]
        for f in SUM_FIELDS:
            assert s[f] == 0
        for f in MAX_FIELDS:
            assert s[f] == 0

    def test_profile_cleared(self):
        s = plan_transfer(_src(), _dst(), now_iso=NOW)["source"]
        # identity is cleared; NOT NULL columns reset to their default instead
        for f in PROFILE_FIELDS:
            if f in SOURCE_RESET_DEFAULTS:
                assert s[f] == SOURCE_RESET_DEFAULTS[f]
            else:
                assert s[f] is None
        assert s["playtime_per_gm"] == {}

    def test_stamped_with_the_target(self):
        s = plan_transfer(_src(), _dst(), now_iso=NOW)["source"]
        assert s["transferred_to"] == "222"
        assert s["transferred_at"] == NOW

    def test_never_reassigned_on_the_source(self):
        s = plan_transfer(_src(), _dst(), now_iso=NOW)["source"]
        for keep in ("token", "discord_id", "discord_username", "is_locked"):
            assert keep not in s


class TestNotNullColumns:
    """The source is emptied, so a NOT NULL column must reset to its default.

    Regression: lobby_music_volume is `not null default 0.5`, and nulling it
    aborted the whole transfer with a 23502.
    """

    def test_source_never_writes_null_into_a_not_null_profile_column(self):
        s = plan_transfer(_src(), _dst(), now_iso=NOW)["source"]
        for f, default in SOURCE_RESET_DEFAULTS.items():
            assert f in PROFILE_FIELDS
            assert s[f] == default, f"{f} would violate its NOT NULL constraint"

    def test_lobby_music_volume_resets_to_the_column_default(self):
        s = plan_transfer(_src(lobby_music_volume=0.9), _dst(), now_iso=NOW)["source"]
        assert s["lobby_music_volume"] == 0.5

    def test_nullable_profile_fields_are_still_cleared(self):
        s = plan_transfer(_src(), _dst(), now_iso=NOW)["source"]
        for f in ("game_username", "game_id", "skin", "pickaxe", "lobby_music_url"):
            assert f not in SOURCE_RESET_DEFAULTS
            assert s[f] is None

    def test_the_targets_volume_is_taken_from_the_source(self):
        t = plan_transfer(_src(lobby_music_volume=0.9), _dst(lobby_music_volume=0.2),
                          now_iso=NOW)["target"]
        assert t["lobby_music_volume"] == 0.9

    def test_no_source_patch_value_is_none_for_a_sum_or_max_field(self):
        s = plan_transfer(_src(), _dst(), now_iso=NOW)["source"]
        for f in SUM_FIELDS + MAX_FIELDS:
            assert s[f] is not None

    def test_reset_defaults_cover_every_declared_field(self):
        # a reset default for a field that is not transferred would be dead
        for f in SOURCE_RESET_DEFAULTS:
            assert f in PROFILE_FIELDS


class TestRefusals:
    def test_same_account(self):
        with pytest.raises(TransferRefused):
            plan_transfer(_src(), _src(), now_iso=NOW)

    def test_missing_ids(self):
        with pytest.raises(TransferRefused):
            plan_transfer({"discord_id": ""}, _dst(), now_iso=NOW)
        with pytest.raises(TransferRefused):
            plan_transfer(_src(), {"discord_id": " "}, now_iso=NOW)

    def test_source_already_transferred(self):
        with pytest.raises(TransferRefused, match="already transferred"):
            plan_transfer(_src(transferred_to="999"), _dst(), now_iso=NOW)

    def test_target_is_itself_a_transferred_account(self):
        with pytest.raises(TransferRefused, match="transferred account"):
            plan_transfer(_src(), _dst(transferred_to="111"), now_iso=NOW)

    def test_source_without_a_token_is_not_an_account(self):
        with pytest.raises(TransferRefused, match="token"):
            plan_transfer(_src(token=""), _dst(), now_iso=NOW)

    def test_refusal_produces_no_patch(self):
        with pytest.raises(TransferRefused):
            plan_transfer(_src(), _src(), now_iso=NOW)


class TestEdgeCases:
    def test_empty_target_takes_the_source_verbatim(self):
        blank = {"discord_id": "222", "token": "t2"}
        p = plan_transfer(_src(), blank, now_iso=NOW)
        assert p["target"]["kills"] == 500
        assert p["target"]["game_username"] == "OldName"

    def test_null_and_junk_values_do_not_crash(self):
        p = plan_transfer(
            _src(kills=5, tourney_pr=None, playtime_per_gm=None),
            _dst(kills="junk", playtime_per_gm="not-json"), now_iso=NOW)
        assert p["target"]["kills"] == 5
        assert float(p["target"]["tourney_pr"]) == 30.0
        assert "playtime_per_gm" not in p["target"]

    def test_null_source_values_leave_the_target_alone(self):
        p = plan_transfer(_src(kills=None, wins=None), _dst(), now_iso=NOW)
        assert p["target"]["kills"] == 10
        assert p["target"]["wins"] == 1

    def test_blank_strings_are_not_treated_as_identity(self):
        p = plan_transfer(_src(game_username="   "), _dst(), now_iso=NOW)
        assert "game_username" not in p["target"]

    def test_ranked_points_are_reported_for_the_caller_to_apply(self):
        p = plan_transfer(_src(), _dst(), now_iso=NOW, ranked_points=1234)
        assert p["ranked_points"] == 1234
        assert "ranked_points" not in p["target"]

    def test_moves_never_mutate_the_inputs(self):
        s, d = _src(), _dst()
        plan_transfer(s, d, now_iso=NOW)
        assert s["kills"] == 500
        assert d["kills"] == 10
        assert d["playtime_per_gm"] == {"public": 600, "duos": 300}
