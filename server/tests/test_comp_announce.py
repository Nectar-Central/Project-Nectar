"""Discord announce POINTS section + board avg (pure bot helpers)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "companion" / "bot"))

import bot as bot_mod


def test_ord():
    assert bot_mod._ord(1) == "1st"
    assert bot_mod._ord(2) == "2nd"
    assert bot_mod._ord(3) == "3rd"
    assert bot_mod._ord(4) == "4th"
    assert bot_mod._ord(11) == "11th"
    assert bot_mod._ord(22) == "22nd"


def test_points_table_lines():
    t = {"points_table": {"kill": 1, "win": 5, "placement": [10, 7]}}
    assert bot_mod._points_table_lines(t) == [
        "1st = 10 pts", "2nd = 7 pts", "KILL = 1 pts", "WIN = 5 pts"]
    # JSON string table also works
    import json
    assert bot_mod._points_table_lines(
        {"points_table": json.dumps(t["points_table"])}) == bot_mod._points_table_lines(t)
    # no table -> no section
    assert bot_mod._points_table_lines({}) == []
    assert bot_mod._points_table_lines({"points_table": "junk"}) == []


def test_announce_has_points_section():
    t = {"id": 4, "name": "Testing Cup", "mode": "solo",
         "points_table": {"kill": 1, "win": 3, "placement": [10, 7, 5]}}
    text = bot_mod._comp_announce_text(t)
    assert "**POINTS**:" in text
    assert "1st = 10 pts" in text
    assert "KILL = 1 pts" in text
    assert "WIN = 3 pts" in text
    # no table -> section omitted, template intact
    text2 = bot_mod._comp_announce_text({"id": 4, "name": "X"})
    assert "**POINTS**:" not in text2
    assert "## SOLO X" in text2


def test_announce_hosted_by():
    t = {"id": 4, "name": "Cup", "hosts": ["111", "222"], "created_by": "111"}
    text = bot_mod._comp_announce_text(t)
    # creator merged, deduped, mentioned
    assert "**HOSTED BY**: <@111> <@222>" in text
    text2 = bot_mod._comp_announce_text({"id": 4, "name": "Cup"})
    assert "**HOSTED BY**: —" in text2


def test_board_lines_avg():
    board = [{"name": "cis6led", "points": 106, "kills": 4, "wins": 7, "avg_placement": 1.8},
             {"name": "chris7", "points": 90, "kills": 3, "wins": 2}]
    lines = bot_mod._comp_board_lines(board)
    assert "1.8 avg" in lines[0]
    assert "avg" not in lines[1]
