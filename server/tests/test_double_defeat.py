"""Regression: elim-before-stG=3 then staying to phase end must not double-defeat.

Shape: defeat #1 written live at version N (killfeed elim). stG=3 bumps to
N+1 and arms; a later stG=1 resets the per-life flags while the arm lingers.
The drain then re-marks (killfeed death evidence persists) and, checking only
version N+1, misses defeat #1 and writes defeat #2 for the same match.

Fix: the drain heals via _defeat_recorded (matches V or pre-bump V-1) and
consumes the arm on every skip path, so a stale arm can never re-fire.
"""
import server.scripts.processor as proc


class _FakeDB:
    def __init__(self):
        self.rows = set()  # (room_id, match_version)

    def exists(self, session_id, event_type, room_id, match_ver):
        return (str(room_id), match_ver) in self.rows


def _patched(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(proc, "_result_event_exists",
                        lambda sid, et, rid, ver, buf=None: fake.exists(sid, et, rid, ver))
    return fake


def test_pre_bump_defeat_blocks_post_bump_rewrite(monkeypatch):
    fake = _patched(monkeypatch)
    fake.rows.add(("room1", 5))  # defeat #1 written live at version 5
    assert proc._defeat_recorded("s", "room1", 6) is True  # stale arm at 6 heals


def test_exact_version_still_blocks(monkeypatch):
    fake = _patched(monkeypatch)
    fake.rows.add(("room1", 6))
    assert proc._defeat_recorded("s", "room1", 6) is True


def test_neighbour_match_unaffected(monkeypatch):
    fake = _patched(monkeypatch)
    fake.rows.add(("room1", 5))
    # version 7 is a whole later match — V-1 tolerance must not reach back to 5
    assert proc._defeat_recorded("s", "room1", 7) is False


def test_other_room_unaffected(monkeypatch):
    fake = _patched(monkeypatch)
    fake.rows.add(("room1", 5))
    assert proc._defeat_recorded("s", "room2", 6) is False


def test_no_record_allows_write(monkeypatch):
    _patched(monkeypatch)
    assert proc._defeat_recorded("s", "room1", 6) is False


def test_bad_version_falls_back(monkeypatch):
    fake = _patched(monkeypatch)
    fake.rows.add(("room1", "n/a"))
    assert proc._defeat_recorded("s", "room1", "n/a") is True
    assert proc._defeat_recorded("s", "room1", "other") is False
