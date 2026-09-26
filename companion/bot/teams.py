"""Duo team slots: colours only (pure, no I/O).

A team is identified by its ORDER in the tourney roster - the same order the
intake assigns it - and there are six slots, so the colour cycles every six
teams. The colour is FROZEN on the registration row, because it is the
reaction the players see on their signup message, which means it drifts
whenever a team is added or removed. /tourney-refresh-teams re-derives them.

There are deliberately NO Discord team roles. They were tried and removed:
one role per slot meant a member had to be wearing a role that agreed with a
colour the roster could reassign at any time, and the two kept disagreeing.

Kept in its own module so it can be imported (and unit-tested) without
dragging in the bot's command tree.
"""
# Written as escapes so the palette cannot be corrupted by an editor's
# encoding, and so the order is reviewable at a glance.
# blue, red, green, yellow, orange, purple
TEAM_COLORS = ["\U0001f535", "\U0001f534", "\U0001f7e2", "\U0001f7e1",
               "\U0001f7e0", "\U0001f7e3"]

TEAM_SLOTS = len(TEAM_COLORS)


def team_color(index) -> str:
    """Colour for a team slot, cycling every six. Never raises."""
    try:
        return TEAM_COLORS[int(index or 0) % TEAM_SLOTS]
    except Exception:
        return TEAM_COLORS[0]


def team_slots(rows) -> list:
    """(row, slot) for every non-banned row, in the order given.

    The slot is the row's POSITION among live teams, so a team's colour and
    its number can never disagree.

    Callers MUST pass rows in a stable order - registration id, which is a
    monotonic identity. An unstable order is what let a team change colour
    between syncs, because Postgres guarantees no row order without ORDER BY.
    Never raises.
    """
    out: list = []
    try:
        for r in (rows or []):
            if str((r or {}).get("status") or "") == "banned":
                continue
            out.append((r, len(out)))
    except Exception:
        pass
    return out
