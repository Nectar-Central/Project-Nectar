"""Conservative fuzzy game-name -> discord_id resolution (pure, no I/O).

Contract:
- A normalized (tags + dc: stripped, casefolded) key that has exactly one
  owner resolves deterministically - that covers casing/tag drift.
- Anything else only resolves through a close, unique, single-owner match:
  below threshold, near-tie, and twin-owner spellings all stay unmapped.
- Money paths (wager settlement) reuse this so a guess can never outrank
  evidence, and an unmapped name stays unmapped rather than becoming a
  plausible-looking wrong player.
"""
from server.lib import names as names_lib


CANDIDATES = {
    "cis6led": "A",
    "chris7": "B",
    "Void Eight": "V",
    "Void Eights": "W",
}


def test_norm_key_strips_tags_dc_prefix_and_case():
    assert names_lib.norm_key("<color=red>cis6led</color>") == "cis6led"
    assert names_lib.norm_key("dc: CIS6LED ") == "cis6led"
    assert names_lib.norm_key("Void   Eight") == "void eight"
    assert names_lib.norm_key(None) == ""
    assert names_lib.norm_key("") == ""


def test_normalized_exact_resolves_case_tag_drift():
    assert names_lib.fuzzy_pick("CIS6LED", CANDIDATES) == "A"
    assert names_lib.fuzzy_pick("<color=red>cis6led</color>", CANDIDATES) == "A"
    assert names_lib.fuzzy_pick("dc: Chris7", CANDIDATES) == "B"


def test_close_typo_resolves_to_single_owner():
    # one substitution (1 -> l) is still the same player
    assert names_lib.fuzzy_pick("cis61ed", CANDIDATES) == "A"
    # trailing character dropped
    assert names_lib.fuzzy_pick("cis6ledd", CANDIDATES) == "A"


def test_near_tie_stays_unmapped():
    # "Void Eight" / "Void Eights" are both close to "Void Eigh" - no winner
    assert names_lib.fuzzy_pick("Void Eigh", CANDIDATES) is None


def test_weak_match_stays_unmapped():
    assert names_lib.fuzzy_pick("Totally Unrelated", CANDIDATES) is None
    assert names_lib.fuzzy_pick("x", CANDIDATES) is None
    assert names_lib.fuzzy_pick("", CANDIDATES) is None
    assert names_lib.fuzzy_pick("cis6led", {}) is None


def test_twin_owners_under_same_spelling_stay_unmapped():
    # same normalized key owned by two accounts: nobody is guessable
    pool = {"Void Eight": "V", "void eight": "W"}
    assert names_lib.fuzzy_pick("VOID EIGHT", pool) is None


def test_candidates_from_users_skips_blank_rows():
    rows = [{"game_username": "cis6led", "discord_id": "A"},
            {"game_username": "", "discord_id": "B"},
            {"game_username": "chris7", "discord_id": None},
            {"game_username": "third", "discord_id": "C"}]
    assert names_lib.candidates_from_users(rows) == [("cis6led", "A"),
                                                     ("third", "C")]
    assert names_lib.candidates_from_users(None) == []


def test_candidates_from_users_keeps_twin_owners():
    # pairs, not a dict: the twin guard needs both owners under one spelling
    rows = [{"game_username": "Void Eight", "discord_id": "V"},
            {"game_username": "void eight", "discord_id": "W"}]
    pool = names_lib.candidates_from_users(rows)
    assert pool == [("Void Eight", "V"), ("void eight", "W")]
    assert names_lib.fuzzy_pick("VOID EIGHT", pool) is None
