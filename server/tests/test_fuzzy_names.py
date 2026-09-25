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


# --- leftover pairing (elimination, roster-scoped) ------------------------

ROSTER = {"A": "cis6led", "B": "klepper"}


def test_pair_leftover_maps_unknown_name_to_unclaimed_roster_player():
    # "dom" matched nobody, and klepper (did B) never showed up under his
    # registered name - one name and one player left, so they are each other
    out = names_lib.pair_leftover(ROSTER, ["cis6led", "dom"],
                                  {"cis6led": "A"})
    assert out == {"dom": "B"}


def test_pair_leftover_ignores_names_already_resolved():
    # nothing unmapped -> nothing to pair
    assert names_lib.pair_leftover(ROSTER, ["cis6led"], {"cis6led": "A"}) == {}
    # every roster player already claimed -> an unknown name is a stranger
    assert names_lib.pair_leftover(ROSTER, ["cis6led", "stranger"],
                                   {"cis6led": "A", "klepper": "B"}) == {}


def test_pair_leftover_refuses_ambiguous_leftovers():
    # two unknown names, one player: no pairing (we cannot tell which)
    assert names_lib.pair_leftover({"A": "cis6led"}, ["dom", "stranger"], {}) == {}
    # one unknown name, two unclaimed players: no pairing
    assert names_lib.pair_leftover({"A": "cis6led", "B": "klepper",
                                    "C": "chris7"},
                                   ["dom"], {"cis6led": "A"}) == {}


def test_pair_leftover_dedupes_case_and_tag_drift():
    # the same name seen twice (raw + tagged) is still one leftover
    out = names_lib.pair_leftover(ROSTER, ["dom", "<color=red>DOM</color>"],
                                  {"cis6led": "A"})
    assert out == {"dom": "B"}


def test_pair_leftover_ignores_blank_roster_names():
    # a registrant with no registered name cannot be paired
    assert names_lib.pair_leftover({"A": "cis6led", "B": ""}, ["dom"],
                                   {"cis6led": "A"}) == {}
    assert names_lib.pair_leftover({}, ["dom"], {}) == {}
    assert names_lib.pair_leftover(None, None, None) == {}
