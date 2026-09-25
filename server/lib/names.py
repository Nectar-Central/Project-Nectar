"""
names.py -conservative game-name -> discord_id resolution (pure, no I/O).

One resolver shared by every surface that has to turn a killfeed/ALV game
name into a player: the comp/ranked event bank (`scripts/processor.py`) and
the wager evidence reader (`routes/bets.py`). Sharing it is the point -if the
tournament board and the money path disagree about who a name is, wagers
settle against a player the board never showed.

Resolution order (first hit wins, every tier conservative):

  1. exact byte match on the stored game_username (callers do this in SQL);
  2. normalized match - color tags, `dc:` prefix, case and whitespace drift
     folded away. One owner per spelling, or nothing (twin guard);
  3. fuzzy match - a close spelling drift, one substitution or a dropped /
     added character, which is what a player who re-set their username badly
     looks like. Only a unique, confidently-better match resolves.

Everything weaker stays unmapped by design. A missed name costs a player a
row on a board (the dashboard /link flow can still fix it); a wrong name pays
real coins to the wrong account, and a twin who guessed wrong takes another
player's placement. Unmapped is the cheap failure, so that is what we pick.

Pure module: no I/O, no Supabase, no server-state imports.
"""

from __future__ import annotations

import difflib

from .gpbinary import _strip_color_tags

# Fuzzy tier gates. A single substitution in a 7-character name scores ~0.86
# and an appended character ~0.93, so 0.84 admits real username drift while
# rejecting unrelated names (which score under ~0.5). The margin keeps a
# second close owner - a genuine near-twin - from being out-voted by noise.
FUZZY_THRESHOLD = 0.84
FUZZY_MARGIN = 0.10

# Cap on the candidate pool a caller may hand in, so a fuzz never becomes a
# full-table scan (callers read this many registered users at most).
MAX_CANDIDATES = 2000


def norm_key(name) -> str:
    """Comparison key for a game name: tags + `dc:` stripped, casefolded,
    inner whitespace collapsed. Never raises."""
    try:
        s = _strip_color_tags(str(name or "")).strip()
        if s.lower().startswith("dc:"):
            s = s[3:].strip()
        return " ".join(s.lower().split())
    except Exception:
        return ""


def similarity(a: str, b: str) -> float:
    """0.0-1.0 spelling closeness. Never raises."""
    try:
        return float(difflib.SequenceMatcher(None, str(a or ""), str(b or "")).ratio())
    except Exception:
        return 0.0


def _owner_pool(candidates) -> dict[str, set[str]]:
    """normalized name -> every discord_id registered under it."""
    pool: dict[str, set[str]] = {}
    try:
        items = (candidates or {}).items() if isinstance(candidates, dict) else (candidates or [])
        for item in items:
            try:
                cname, did = item
            except Exception:
                continue
            key = norm_key(cname)
            did = str(did or "").strip()
            if not key or not did:
                continue
            pool.setdefault(key, set()).add(did)
            if len(pool) > MAX_CANDIDATES:
                break
    except Exception:
        return pool
    return pool


def fuzzy_pick(name, candidates, threshold: float = FUZZY_THRESHOLD,
               margin: float = FUZZY_MARGIN):
    """The one discord_id `name` can mean, or None.

    candidates: a `{name: discord_id}` dict, or an iterable of
    `(name, discord_id)` pairs (use `candidates_from_users` for that). Repeat
    owners under one spelling stay ambiguous and resolve to nothing.

    None when the name is blank, nothing is close enough, the best match is
    within `margin` of the runner-up, or the winning spelling is owned by
    more than one account. Never raises.
    """
    key = norm_key(name)
    if not key:
        return None
    pool = _owner_pool(candidates)
    if not pool:
        return None
    exact = pool.get(key)
    if exact is not None:
        return next(iter(exact)) if len(exact) == 1 else None
    scored = sorted(((similarity(key, ck), ck, dids)
                     for ck, dids in pool.items() if dids),
                    key=lambda t: -t[0])
    if not scored:
        return None
    best = scored[0]
    if best[0] < threshold or len(best[2]) != 1:
        return None
    if len(scored) > 1 and (best[0] - scored[1][0]) < margin:
        return None
    return next(iter(best[2]))


def candidates_from_users(rows) -> list[tuple[str, str]]:
    """`bngc_users`-shaped rows -> (game_username, discord_id) pairs.

    Pairs, not a dict: two accounts can share one game name and the twin
    guard needs to see both owners. Never raises.
    """
    out: list[tuple[str, str]] = []
    try:
        for r in (rows or []):
            try:
                name = str((r or {}).get("game_username") or "").strip()
                did = str((r or {}).get("discord_id") or "").strip()
                if name and did:
                    out.append((name, did))
            except Exception:
                continue
    except Exception:
        return out
    return out[:MAX_CANDIDATES]


def pair_leftover(roster, observed_names, resolved, claimed_dids=None) -> dict:
    """Elimination tier: the one unknown name is the one unclaimed player.

    The case fuzzy matching cannot reach: a player's registered name is stale
    and no amount of spelling similarity bridges it. "klepper" in the database
    and "dom" in game share nothing, so the only way to learn they are the
    same person is to count. Inside a match with a trusted roster
    (accepted registrations, room roster) the wager creator's own companion
    feed names itself correctly, so every other name already has an owner -
    and one name is left over, and one roster player never showed up under
    their registered name. Those two are each other.

    roster: {discord_id: registered game name} for the players the match
    expects. observed_names: names seen in the match. resolved: {name:
    discord_id} already assigned. claimed_dids: extra accounts to treat as
    claimed (a spectating host, a manual row).

    Returns {} unless exactly one name is unmapped AND exactly one roster
    player is unclaimed. That single leftover pair is what a match with a
    closed roster actually looks like when a registered name is stale. Two
    leftovers are a coin flip, and refusing those is what keeps a stranger
    from being handed another player's placement and their coins. The
    residual risk - one stranger plus one absent registrant - is genuinely
    indistinguishable from the event stream alone; the host can always
    override on the dashboard. Callers must NOT cache the result: it is true
    of this roster, not of that name. Never raises.
    """
    try:
        observed: list = []
        seen_keys: set = set()
        for n in (observed_names or []):
            k = norm_key(n)
            if not k or k in seen_keys:
                continue
            seen_keys.add(k)
            observed.append(n)
        if not observed:
            return {}
        unmapped = [n for n in observed
                    if not str((resolved or {}).get(n) or "").strip()]
        if len(unmapped) != 1:
            return {}
        claimed = {str(d) for d in (resolved or {}).values()
                   if str(d or "").strip()}
        claimed |= {str(d) for d in (claimed_dids or []) if str(d or "").strip()}
        left: list = []
        for did, name in (roster or {}).items():
            d = str(did or "").strip()
            if not d or d in claimed or not norm_key(name):
                continue
            left.append(d)
            if len(left) > 1:
                return {}          # two absent players: no way to tell
        if len(left) != 1:
            return {}
        return {unmapped[0]: left[0]}
    except Exception:
        return {}

