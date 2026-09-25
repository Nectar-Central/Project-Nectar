"""Duo team slots: colours and per-team Discord roles (pure, no I/O).

A team is identified by its ORDER in the tourney roster - the same order the
intake assigns it - and there are six slots, so colour and role cycle
together past six teams.

TEAM_COLORS and DEFAULT_TEAM_ROLE_IDS are in lockstep by index and MUST stay
that way: slot N's colour is the colour of the role it grants, so reordering
one without the other hands a team a role that disagrees with the board.

Role ids default to the six roles that exist on the Nectar server, so a fresh
deployment grants the right roles with no extra setup. COMP_TEAM_ROLE_1..6
override them per slot, and a variable that is set but EMPTY disables that
slot (distinct from not being set, which means "use the default"). An unset
slot grants nothing and is never stripped.

Kept in its own module so it can be imported (and unit-tested) without
dragging in the bot's command tree.
"""
import os

# blue, red, green, yellow, orange, purple
TEAM_COLORS = ["🔵", "🔴", "🟢", "🟡", "🟠", "🟣"]

TEAM_SLOTS = len(TEAM_COLORS)

DEFAULT_TEAM_ROLE_IDS = [
    "1553079575452450977",  # 1 blue
    "1553079824124223690",  # 2 red
    "1553079938599358605",  # 3 green
    "1553080070019481750",  # 4 yellow
    "1553080214240624750",  # 5 orange
    "1553080641502056458",  # 6 purple
]


def _configured_role_id(slot: int) -> str:
    """Env override for a slot, else the shipped default, else "".

    Absent means "use the default"; present-but-empty means "no role here".
    """
    raw = os.getenv(f"COMP_TEAM_ROLE_{slot}")
    if raw is None:
        return DEFAULT_TEAM_ROLE_IDS[slot - 1]
    return str(raw or "").strip()


TEAM_ROLE_IDS = [_configured_role_id(i) for i in range(1, TEAM_SLOTS + 1)]


def team_color(index) -> str:
    """Colour for a team slot, cycling every six. Never raises."""
    try:
        return TEAM_COLORS[int(index or 0) % TEAM_SLOTS]
    except Exception:
        return TEAM_COLORS[0]


def team_role_id(index) -> str:
    """Discord role id for a team slot, or "" when that slot is unset.

    An unparsable index grants nothing rather than raising into a command.
    """
    try:
        return str(TEAM_ROLE_IDS[int(index or 0) % len(TEAM_ROLE_IDS)] or "").strip()
    except Exception:
        return ""


def configured_team_role_ids() -> set:
    """Every configured team role id - the ones to strip from a member who
    is no longer on that team. Never raises."""
    try:
        return {r for r in TEAM_ROLE_IDS if r}
    except Exception:
        return set()
