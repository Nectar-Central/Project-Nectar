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
