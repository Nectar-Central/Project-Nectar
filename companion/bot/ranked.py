"""Ranked v1 — host-authored rooms. Data layer + shared helpers.

Tables (see migration in chat): ranked_rooms, ranked_points, ranked_results,
ranked_bans. All decisions logged with the public R-ID for admin review.
"""
import secrets
from datetime import datetime, timezone

import database as db

RANKED_PING_ROLE_ID = 1545585233569652776  # role pinged on ranked create
CREATE_CHANNEL_ID = 1545584792978981006  # channel where lobby embeds are posted
MATCHES_CHANNEL_ID = 1545584792978981006  # channel where private session threads live (same as lobby)

ALLOWED_GMS = ("zw_lg", "df", "wwzw")

# Milestone entry fees: at/above each tier you burn the fee on every start.
# (Bronze <200 pays nothing.) Tune freely — (threshold, name, fee).
MILESTONE_FEES = [(2000, "Masters", 100), (1000, "Diamond", 50),
                  (500, "Gold", 25), (200, "Silver", 10)]


def milestone_for(points: int):
    try:
        p = int(points or 0)
    except Exception:
        p = 0
    for threshold, name, fee in MILESTONE_FEES:
        if p >= threshold:
            return name, fee
    return "Bronze", 0


def entry_fee(room: dict, player_points: int) -> int:
    """Total burned on start: room fee + milestone fee."""
    _, tier_fee = milestone_for(player_points)
    return int(room.get("fee") or 0) + int(tier_fee)


RULES_TEXT = (
    "1. Roster only — outsiders earn nothing and may get the room reset.\n"
    "2. Host's word is final; host must stay in the room.\n"
    "3. No smurfing, stream-sniping or point boosting — bans apply.\n"
    "4. If you crash, rejoin fast and tell the host."
)


def new_rid() -> str:
    return "R-" + secrets.token_hex(3).upper()


def _sb():
    if not db.USE_SUPABASE or not db.supabase:
        raise RuntimeError("Supabase is not configured")
    return db.supabase


def get_room(rid: str):
    try:
        r = _sb().table("ranked_rooms").select("*").eq("id", rid).limit(1).execute()
        return (r.data or [None])[0]
    except Exception:
        return None


def get_open_room_by_code(code: str):
    try:
        r = _sb().table("ranked_rooms").select("*").eq("short_code", code.upper()).eq("status", "lobby").limit(1).execute()
        rows = r.data or []
        if rows:
            return rows[0]
        r = _sb().table("ranked_rooms").select("*").eq("short_code", code.upper()).eq("status", "live").limit(1).execute()
        return ((r.data or [None])[0])
    except Exception:
        return None


def create_room(*, code, host_discord_id, gm, max_players, max_matches,
                min_points=0, max_points=None, fee=0, starts_at=None,
                lobby_channel_id=None, lobby_message_id=None):
    for _ in range(5):
        rid = new_rid()
        if get_room(rid) is None:
            break
    else:
        raise RuntimeError("R-ID collision, try again")
    row = {
        "id": rid,
        "short_code": code.upper(),
        "host_discord_id": str(host_discord_id),
        "gm": gm,
        "max_players": max_players,
        "max_matches": max_matches,
        "min_points": min_points or 0,
        "max_points": max_points,
        "fee": fee or 0,
        "starts_at": starts_at,
        "status": "lobby",
        "roster": [{"discord_id": str(host_discord_id), "joined_at": _now()}],
        "lobby_channel_id": str(lobby_channel_id) if lobby_channel_id else None,
        "lobby_message_id": str(lobby_message_id) if lobby_message_id else None,
        "thread_id": None,
    }
    _sb().table("ranked_rooms").insert(row).execute()
    return get_room(rid)


def update_room(rid: str, fields: dict):
    _sb().table("ranked_rooms").update(fields).eq("id", rid).execute()


def roster_ids(room: dict) -> list[str]:
    try:
        return [str(x.get("discord_id")) for x in (room.get("roster") or []) if x.get("discord_id")]
    except Exception:
        return []


def roster_add(room: dict, discord_id: str):
    roster = list(room.get("roster") or [])
    if str(discord_id) not in [str(x.get("discord_id")) for x in roster]:
        roster.append({"discord_id": str(discord_id), "joined_at": _now()})
        update_room(room["id"], {"roster": roster})
        return True
    return False


def roster_remove(room: dict, discord_id: str):
    roster = [x for x in (room.get("roster") or []) if str(x.get("discord_id")) != str(discord_id)]
    if len(roster) != len(room.get("roster") or []):
        update_room(room["id"], {"roster": roster})
        return True
    return False


def is_banned(discord_id: str) -> bool:
    try:
        r = _sb().table("ranked_bans").select("discord_id").eq("discord_id", str(discord_id)).limit(1).execute()
        return bool(r.data)
    except Exception:
        return False


def list_due_starts():
    """Lobby rooms whose countdown passed (for the scheduler loop)."""
    try:
        now = _now()
        r = _sb().table("ranked_rooms").select("*").eq("status", "lobby").lte("starts_at", now).limit(10).execute()
        return [x for x in (r.data or []) if x.get("starts_at")]
    except Exception:
        return []


def get_points(discord_id: str) -> int:
    try:
        r = _sb().table("ranked_points").select("points").eq("discord_id", str(discord_id)).limit(1).execute()
        if r.data:
            return int(r.data[0].get("points") or 0)
    except Exception:
        pass
    return 0


def add_points(discord_id: str, delta: int):
    cur = get_points(discord_id)
    _sb().table("ranked_points").upsert({
        "discord_id": str(discord_id),
        "points": cur + int(delta),
        "updated_at": _now(),
    }, on_conflict="discord_id").execute()
    return cur + int(delta)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def lobby_embed(room: dict) -> dict:
    """Spec for the lobby embed (bot.py renders it): title, fields, footer."""
    lock = ""
    if (room.get("min_points") or 0) > 0:
        lock = f"🔒 {room['min_points']}+ points"
        if room.get("max_points"):
            lock += f" (max {room['max_points']})"
    starts = "Manual start"
    if room.get("starts_at"):
        try:
            ts = int(datetime.fromisoformat(str(room["starts_at"]).replace("Z", "+00:00")).timestamp())
            starts = f"<t:{ts}:R>"
        except Exception:
            starts = str(room.get("starts_at"))
    return {
        "title": f"Ranked Lobby {room['id']} — {room.get('gm', '').upper()}",
        "fields": [
            ("Status", "Lobby — code hidden until start", True),
            ("Players", f"{len(roster_ids(room))}/{room.get('max_players')}", True),
            ("Matches", str(room.get("max_matches")), True),
            ("Lock", lock or "Open", True),
            ("Join fee", str(room.get("fee") or 0), True),
            ("Starts", starts, True),
        ],
        "footer": f"{room['id']} · press Join to enter · host sets the roster",
    }


def live_embed(room: dict, thread_id=None) -> dict:
    """Spec for the flipped-live lobby embed."""
    return {
        "title": f"Ranked LIVE {room['id']} — {room.get('gm', '').upper()}",
        "fields": [
            ("Status", "Live — code sent to roster DMs", True),
            ("Players", f"{len(roster_ids(room))}/{room.get('max_players')}", True),
            ("Matches", f"0/{room.get('max_matches')}", True),
        ],
        "footer": f"{room['id']} · thread: {thread_id or '—'}",
    }
