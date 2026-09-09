"""Unit tests for insights computation (kills-first best/worst)."""
from server.routes.insights import compute_insights


def _ev(etype, room="abc123", ver=1, gm="zw_lg", ts="2026-09-04T10:00:00", **data):
    ed = {"room_id": room, "match_version": ver, "gm": gm}
    ed.update(data)
    return {"session_id": "s1", "event_type": etype, "event_data": ed, "created_at": ts}


def test_best_worst_kills_first():
    evs = [
        _ev("kill", ver=1, victim="a"), _ev("kill", ver=1, victim="b"),
        _ev("death", ver=1, killer="x"),
        _ev("kill", ver=2, victim="c"),
        _ev("win", ver=2, ts="2026-09-04T11:00:00"),
    ]
    out = compute_insights(evs, {"kills": 3, "deaths": 1, "wins": 1, "defeats": 1,
                                 "playtime_per_gm": {"zw_lg": 100, "bf": 10}})
    assert out["matches_analyzed"] == 2
    assert out["best_session"]["match_version"] == 1  # 2 kills beats 1-kill win
    assert out["best_session"]["kills"] == 2
    assert out["worst_session"]["match_version"] == 2
    assert out["biggest_fear"] == {"player": "x", "deaths": 1}
    assert out["biggest_free_kill"]["kills"] == 1
    assert out["fav_game_mode"] == "zw_lg"
    assert out["recent_form"][0]["match_version"] == 2
    assert out["clutch_factor"] == 1  # the 1-kill win
    assert out["playstyle"] in ("Aggressor", "Anchor", "Support", "Wildcard")
    assert out["fav_teammate"] is None and out["needs_team_data"] is True


def test_empty_events():
    out = compute_insights([], {"playtime_per_gm": {}})
    assert out["matches_analyzed"] == 0
    assert out["best_session"] is None and out["worst_session"] is None
    assert out["playstyle"] is None


def test_lobby_noise_ignored():
    evs = [{"session_id": "s", "event_type": "join",
            "event_data": {"room_id": "lobby"}, "created_at": "2026-09-04T10:00:00"}]
    out = compute_insights(evs, {})
    assert out["matches_analyzed"] == 0
