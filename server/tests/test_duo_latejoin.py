"""Regression: BF2V2 duo late-join (3 players in room, 4th = me).

Symptom 1: my elim recorded immediate defeat with teammate alive — the
late joiner and pre-existing players lacked _pt (targeted broadcasts
predated capture), so no team formed and the deferral silently degraded
to solo rules. Fix: harvest _pt from the ActorList roster (_apply_roster_pt).

Symptom 2: teammate's death recorded as MY defeat — nameless alv sender
(often the room master) shadowed TargetActorNr. Fix: target-first
resolution (_resolve_alv_subject).
"""
from server.lib.photon_engine import engine as eng_mod
from server.lib.photon_engine.engine import _apply_roster_pt, _resolve_alv_subject
from server.lib.photon_engine.models import Player, RoomView, SessionView, Team
from server.scripts.processor import _team_alive_for_me


def _room():
    r = RoomView(room_id="r1")
    v = SessionView(session_id="s")
    return r, v


# --- _apply_roster_pt ---

def test_harvest_fills_by_actor_key():
    r, _ = _room()
    r.players[2] = Player(actor_nr=2, username="mate")
    assert _apply_roster_pt(r, 2, "mate", "2") is True
    assert r.players[2].pt == "2"


def test_harvest_fills_by_username_when_key_differs():
    r, _ = _room()
    # killfeed-known player keyed by hash, no actor/pt
    r.players[456] = Player(actor_nr=-1, username="mate")
    assert _apply_roster_pt(r, 2, "mate", {"value": 2}) is True
    assert r.players[456].pt == "2"


def test_harvest_never_overwrites_live_pt():
    r, _ = _room()
    r.players[2] = Player(actor_nr=2, username="mate", pt="1")
    assert _apply_roster_pt(r, 2, "mate", "2") is False
    assert r.players[2].pt == "1"


def test_harvest_stashes_pending_for_unknown_actor():
    r, _ = _room()
    assert _apply_roster_pt(r, 7, None, "1") is False
    assert r.pending_pt[7] == "1"


def test_harvest_ignores_empty():
    r, _ = _room()
    assert _apply_roster_pt(r, 2, "mate", None) is False
    assert _apply_roster_pt(r, 2, "mate", "  ") is False


# --- _resolve_alv_subject ---

def _gpb(sender, target):
    return {"params": {"ActorNr": sender, "TargetActorNr": target}}


def test_target_wins_over_sender():
    r, _ = _room()
    r.actor_map = {1: "master", 2: "mate"}
    name, actor = _resolve_alv_subject(r, None, _gpb(1, 2), 1)
    assert (name, actor) == ("mate", 2)


def test_sender_fallback_when_target_unknown():
    r, _ = _room()
    r.actor_map = {1: "master"}
    name, actor = _resolve_alv_subject(r, None, _gpb(1, 9), 1)
    assert (name, actor) == ("master", 1)


def test_named_packet_untouched():
    r, _ = _room()
    name, actor = _resolve_alv_subject(r, "me", _gpb(1, 2), 1)
    assert (name, actor) == ("me", 1)


def test_no_info_stays_unknown():
    r, _ = _room()
    name, actor = _resolve_alv_subject(r, None, {"params": {}}, 5)
    assert (name, actor) == (None, 5)


# --- late-join deferral shape ---

def test_harvested_late_join_team_defers():
    # me late-joins with 3 in room; roster harvest fills both pts -> team
    # forms -> teammate alive -> deferral holds (True, not None/False)
    r, v = _room()
    me = Player(actor_nr=4, username="me", is_self=True)
    r.players[10] = me
    r.players[11] = Player(actor_nr=-1, username="mate")
    v.me = me
    assert _apply_roster_pt(r, 4, "me", "2") is True
    assert _apply_roster_pt(r, 2, "mate", "2") is True
    r.teams["2"] = Team(team_id="2", members=["me", "mate"])
    assert _team_alive_for_me(r, v, "me") is True


def test_engine_module_imports():
    assert callable(eng_mod._apply_roster_pt)
