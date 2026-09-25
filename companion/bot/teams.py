"""Duo team slots: colours and per-team Discord roles (pure, no I/O).

A team is identified by its ORDER in the tourney roster - the same order the
intake assigns it - and there are six slots, so colour and role cycle
together past six teams. Roles are configuration (COMP_TEAM_ROLE_1..6) so
the ids live in the environment and never in code; an unset slot grants
nothing and is never stripped.

Kept in its own module so it can be imported (and unit-tested) without
dragging in the bot's command tree.
"""
import os

TEAM_COLORS = ["🔵", "🔴", "🟠", "🟡", "🟢", "🟣"]

TEAM_SLOTS = len(TEAM_COLORS)

TEAM_ROLE_IDS = [os.getenv(f"COMP_TEAM_ROLE_{i}", "").strip()
                 for i in range(1, TEAM_SLOTS + 1)]


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
