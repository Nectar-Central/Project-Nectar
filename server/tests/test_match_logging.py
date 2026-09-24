"""Match lifecycle / outcome lines must be real, greppable log lines.

RANK lines (`RANK [ranked] capture sid=... room=... code=... ver=7`) are the
model: a dedicated colored tag plus key=value fields. MATCH START / MATCH END
/ WIN / DEFEAT were either folded into a generic SYS line or missing entirely
(defeats are wins-only by design, so nothing ever said "you lost that one"),
which made a lost round impossible to read straight from the server console.

Pinned here: the match stream's shape, the engine's START/END field set, the
processor's WIN line, and the DEFEAT gate (real elimination only — a leave or
an empty room is not a defeat).
"""
from server.lib import logger as slog
from server.lib.photon_engine.models import KillInfo, Player, RoomView
from server.scripts import processor as proc


class _FakeLogger:
    def __init__(self):
        self.lines = []

    def info(self, line):
        self.lines.append(line)


# --- the stream itself ------------------------------------------------------

def test_log_match_prints_tagged_line_and_writes_file(monkeypatch, capsys):
    fake = _FakeLogger()
    monkeypatch.setattr(slog, "_match_logger", fake)
    slog.log_match("START", event="match", session="sess_1a0ce360",
                   room="a1998c71", gm="zw_lg", code="VMNCH", version=0)
    out = capsys.readouterr().out
    assert "MATCH" in out
    assert "[match] START room=a1998c71 gm=zw_lg code=VMNCH version=0" in out
    assert "sess_1a0ce360" in out
    assert len(fake.lines) == 1
    assert "[match] START room=a1998c71" in fake.lines[0]


def test_log_match_json_event_goes_to_the_json_stream(monkeypatch):
    fake = _FakeLogger()
    monkeypatch.setattr(slog, "_struct_logger", fake)
    slog.log_match("END", event="match", session="sess_1a0ce360",
                   room="a1998c71", version=1)
    assert len(fake.lines) == 1
    assert '"event":"match"' in fake.lines[0]
    # The verb is the action in the JSON stream.
    assert '"action":"END"' in fake.lines[0]
    assert '"session":"sess_1a0ce360"' in fake.lines[0]


def test_log_match_tag_argument_selects_the_tag(monkeypatch, capsys):
    monkeypatch.setattr(slog, "_match_logger", _FakeLogger())
    slog.log_match("DEFEAT", tag="DEFEAT", event="defeat",
                   me="cis6led", room="a1998c71")
    out = capsys.readouterr().out
    assert "DEFEAT" in out
    assert "[match] DEFEAT me=cis6led room=a1998c71" in out


def test_log_match_never_raises_on_bad_input(monkeypatch):
    monkeypatch.setattr(slog, "_match_logger", _FakeLogger())
    slog.log_match("START", room=None, gm=None)  # None fields are dropped
    slog.log_match("WIN", tag="NOPE", event="win", v=0)


# --- engine: START / END field set -----------------------------------------

def test_engine_match_log_fields_carry_room_code_mode_version():
    from server.lib.photon_engine import engine as eng
    room = RoomView(room_id="a1998c71-deadbeef", short_code="VMNCH")
    room.match_version = 3
    f = eng._match_log_fields(room, kills=2)
    assert f["room"] == "a1998c71"
    assert f["code"] == "VMNCH"
    assert f["version"] == 3
    assert f["kills"] == 2
    # The verb is the action (log_match turns it into the JSON `action`).
    assert "action" not in f
    assert eng._match_log_fields(room, players=2)["players"] == 2
    assert eng._match_log_fields(None)["room"] is None


# --- processor: WIN line ----------------------------------------------------

def test_write_win_logs_a_win_line(monkeypatch, capsys):
    monkeypatch.setattr(proc, "_result_event_exists",
                        lambda sid, et, rid, mv, buf=None: False)
    monkeypatch.setattr(proc, "_precision", type(
        "P", (), {"schedule": staticmethod(lambda *a, **k: None)})())
    room = RoomView(room_id="a1998c71-dead", short_code="VMNCH")
    room.match_version = 3
    proc._write_win("129547868", "sess_1a0ce360", {"wins": 1}, "zw_lg",
                    "a1998c71-dead", "VMNCH", 3, "cis6led", room,
                    buf=[], dirty=set())
    out = capsys.readouterr().out
    assert "WIN" in out
    assert "winner=cis6led" in out
    assert "room=a1998c71" in out
    assert "code=VMNCH" in out
    assert "version=3" in out


# --- processor: DEFEAT line (wins-only, log only) ---------------------------

def _duel_room(me_alive: bool):
    room = RoomView(room_id="a1998c71-dead", short_code="VMNCH")
    room.match_version = 1
    room.game_mode = None
    me = Player(actor_nr=1, username="cis6led", game_id="G1")
    me.is_alive = me_alive
    foe = Player(actor_nr=2, username="chris7", game_id="G2")
    foe.is_alive = True
    room.players = {1: me, 2: foe}
    room.recent_kills = [KillInfo(instigator="chris7", instigated="cis6led",
                                 event_type="killed")]
    return room


def test_defeat_fields_name_the_killer_on_a_real_elimination():
    room = _duel_room(me_alive=False)
    f = proc._defeat_fields(room, None, "cis6led", "zw_lg")
    assert f["me"] == "cis6led"
    assert f["killer"] == "chris7"
    assert f["room"] == "a1998c71"
    assert f["code"] == "VMNCH"
    assert f["version"] == 1


def test_defeat_fields_absent_when_the_player_survived():
    room = _duel_room(me_alive=True)
    assert proc._defeat_fields(room, None, "cis6led", "zw_lg") is None


def test_defeat_fields_absent_without_death_evidence():
    # Empty room, no killfeed, no dead roster row: unknown, not a defeat.
    room = RoomView(room_id="a1998c71-dead", short_code="VMNCH")
    room.match_version = 1
    assert proc._defeat_fields(room, None, "cis6led", "zw_lg") is None
    assert proc._defeat_fields(None, None, "cis6led", "zw_lg") is None


def test_defeat_fields_survive_a_malformed_room():
    class _Boom:
        def __getattr__(self, n):
            raise RuntimeError("boom")
    try:
        assert proc._defeat_fields(_Boom(), None, "cis6led", "zw_lg") is None
    except RuntimeError:
        raise AssertionError("_defeat_fields must never raise")
