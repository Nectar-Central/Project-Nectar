"""Wager console logging must explain WHY a match was dropped/denied.

Silent `return`s were the reason "matches not tracked / wins denied" was
undiagnosable. These tests pin the new always-on anomaly lines and the
WAGER_DEBUG-gated rejection trace.
"""
import importlib.util
import os
import sys
from pathlib import Path


def _load_duels():
    bot_dir = str(Path("companion/bot").resolve())
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    spec = importlib.util.spec_from_file_location(
        "duels_logging_under_test", Path(bot_dir) / "duels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RID = "77dc5908-b709-4f59-bdc2-4e4149e61e72"
PUB = "53a855cd-2fd-0000-0000-000000000000"
CREATOR, OPPONENT = "111", "222"
SINCE = "2026-01-01T00:00:00+00:00"
TS = "2026-09-23T14:32:14+00:00"


def _ev(did, et, ver=0, rid=RID, gm="zw_lg", code="LCKGT", extra=None):
    gd = {"room_id": rid, "match_version": ver, "gm": gm,
          "short_code": code, "code": code}
    gd.update(extra or {})
    return {"discord_id": did, "event_type": et, "event_data": gd, "created_at": TS}


def test_trace_reports_pub_rows_dropped():
    duels = _load_duels()
    trace = []
    # A pub kill in a different room with no code — the classic "not tracked".
    duels.group_duel_matches(
        [_ev(CREATOR, "kill", rid=PUB, code=""), _ev(OPPONENT, "death", rid=PUB, code="")],
        CREATOR, OPPONENT, "zw_lg", None, None, SINCE, trace=trace)
    assert any("pub/no code" in t for t in trace), trace
    # Both duelists' rows were seen, so nothing should blame missing presence.
    assert not any("incomplete" in t for t in trace), trace


def test_trace_reports_room_binding_and_mode():
    duels = _load_duels()
    trace = []
    rows = [_ev(CREATOR, "kill", rid=PUB), _ev(OPPONENT, "death", rid=PUB)]
    duels.group_duel_matches(rows, CREATOR, OPPONENT, "zw_lg", RID, None,
                             SINCE, trace=trace)
    assert any("room != bound" in t for t in trace), trace
    trace2 = []
    rows2 = [_ev(CREATOR, "kill", gm="df"), _ev(OPPONENT, "death", gm="df")]
    duels.group_duel_matches(rows2, CREATOR, OPPONENT, "zw_lg", None, None,
                             SINCE, trace=trace2)
    assert any("gm != mode" in t for t in trace2), trace2


def test_trace_reports_missing_co_presence():
    duels = _load_duels()
    trace = []
    # Only the creator has rows — opponent presence never proven.
    duels.group_duel_matches([_ev(CREATOR, "kill", ver=1)], CREATOR, OPPONENT,
                             "zw_lg", None, None, SINCE, trace=trace)
    assert any("incomplete" in t and "opponent" in t for t in trace), trace


def test_wdebug_toggle(monkeypatch):
    duels = _load_duels()
    monkeypatch.delenv("WAGER_DEBUG", raising=False)
    assert duels._wdebug() is False
    monkeypatch.setenv("WAGER_DEBUG", "1")
    assert duels._wdebug() is True
    monkeypatch.setenv("WAGER_DEBUG", "yes")
    assert duels._wdebug() is True
    monkeypatch.setenv("WAGER_DEBUG", "0")
    assert duels._wdebug() is False


def test_wlog_only_prints_when_debug(capsys, monkeypatch):
    duels = _load_duels()
    monkeypatch.delenv("WAGER_DEBUG", raising=False)
    duels._wlog("quiet line")
    assert "quiet line" not in capsys.readouterr().out
    monkeypatch.setenv("WAGER_DEBUG", "1")
    duels._wlog("loud line")
    assert "loud line" in capsys.readouterr().out


def test_wins_denied_detected(capsys):
    # A match with kills but no win row in a wins-race is NOT sealable — the
    # "wins denied" symptom. The seal gate must reject it (and _score_one logs
    # a WIN-DENIED? line, pinned here via the gate itself).
    duels = _load_duels()
    m = {"room_id": RID, "match_version": 0, "kills_c": 3, "kills_o": 1,
         "win_id": None}
    assert duels._match_sealable(m, "wins") is False
    assert duels.score_match(3, 1, None, CREATOR, OPPONENT, race="wins") is None
    # Once the win row exists it seals and the winner is unambiguous.
    m2 = dict(m, win_id=CREATOR)
    assert duels._match_sealable(m2, "wins") is True
    assert duels.score_match(3, 1, CREATOR, CREATOR, OPPONENT,
                             race="wins") == CREATOR
