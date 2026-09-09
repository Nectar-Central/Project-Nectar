"""
presence.py — friend status from live session state (pure, unit-tested).

Computes one presence dict from a cached SessionView dict (same shape as
/api/stats serves) plus room tags:

  {status, detail, gm, code, count, tag}

status:  offline | lobby | match        (coarse bucket)
detail:  offline | lobby | match | scrim | tourney
gm:      raw game_mode of the current room (drawer formats the label)
code:    5-char pID when holding a private room, else None
count:   players_count of the current room (0 when unknown)
tag:     {kind, name} when the room code is tagged, else None

Rules:
- No session, or stale captures (>120s, same window as stats._SESSION_STALE_S)
  -> offline. Kicked/closed/reloaded clients must read offline, never a
  ghost room.
- Current room is the lobby pseudo-room, or state is lobby -> lobby.
- State match + tagged code -> the tag's detail (scrim/tourney) with its name.
- State match + private code + 8 or more players (count or max) -> scrim
  heuristic (custom lobbies don't exist below that size; the tag overrides it).
- Otherwise a plain match.

Stale/offline and lobby never carry gm/code (clients must not render them).
Never raises.
"""

from __future__ import annotations

import time as _time

_STALE_S = 120
_SCRIM_MIN_PLAYERS = 8


def _room_of(live: dict | None) -> dict | None:
    try:
        rooms = (live or {}).get("rooms") or {}
        cur_id = (live or {}).get("current_room_id")
        if cur_id and cur_id in rooms:
            return rooms[cur_id]
        if rooms:
            return next(reversed(list(rooms.values())), None)
    except Exception:
        pass
    return None


def _fresh(live: dict | None, now: float | None = None) -> bool:
    """Mirror of stats._SESSION_STALE_S: recent packets or recent persist."""
    try:
        now = now if now is not None else _time.time()
        room = _room_of(live)
        if isinstance(room, dict):
            try:
                lp = room.get("last_packet_at")
                if lp:
                    return (now - float(lp)) <= _STALE_S
            except Exception:
                pass
        try:
            ca = (live or {}).get("_cached_at")
            if ca:
                return (now - float(ca)) <= _STALE_S
        except Exception:
            pass
        return True  # no timestamps at all (old caches) fails open
    except Exception:
        return True


def _clean_code(room: dict | None) -> str | None:
    try:
        c = str((room or {}).get("short_code") or "").strip().upper()
        return c if len(c) == 5 and c.isalnum() else None
    except Exception:
        return None


def presence_for(live: dict | None, tags: dict | None = None,
                 now: float | None = None) -> dict:
    """Presence for one cached session. Pure. See module docstring."""
    tags = tags or {}
    try:
        if not live or not _fresh(live, now):
            return {"status": "offline", "detail": "offline", "gm": None,
                    "code": None, "count": 0, "tag": None}
        room = _room_of(live)
        if not room or str(room.get("room_id") or "") == "lobby":
            return {"status": "lobby", "detail": "lobby", "gm": None,
                    "code": None, "count": 0, "tag": None}
        state = str(room.get("state") or "").lower()
        if state != "match":
            return {"status": "lobby", "detail": "lobby", "gm": None,
                    "code": None, "count": 0, "tag": None}
        gm = room.get("game_mode")
        code = _clean_code(room)
        try:
            count = int(room.get("players_count") or 0)
        except Exception:
            count = 0
        try:
            maxp = int(room.get("max_players") or 0)
        except Exception:
            maxp = 0
        if code and str(code) in tags:
            t = tags.get(str(code)) or {}
            kind = str(t.get("kind") or "").lower()
            detail = kind if kind in ("scrim", "tourney") else "match"
            tag = {"kind": kind or "scrim", "name": str(t.get("name") or code)}
            return {"status": "match", "detail": detail, "gm": gm,
                    "code": code, "count": count, "tag": tag}
        if code and max(count, maxp) >= _SCRIM_MIN_PLAYERS:
            return {"status": "match", "detail": "scrim", "gm": gm,
                    "code": code, "count": count, "tag": None}
        return {"status": "match", "detail": "match", "gm": gm,
                "code": None, "count": count, "tag": None}
    except Exception:
        return {"status": "offline", "detail": "offline", "gm": None,
                "code": None, "count": 0, "tag": None}
