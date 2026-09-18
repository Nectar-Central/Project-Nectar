"""Ranked v1 — host-authored rooms. Data layer + shared helpers.

Tables (see migration in chat): ranked_rooms, ranked_points, ranked_results,
ranked_bans. All decisions logged with the public R-ID for admin review.
"""
import os
import secrets
from datetime import datetime, timezone

import database as db

RANKED_PING_ROLE_ID = 1545585233569652776  # role pinged on ranked create
CREATE_CHANNEL_ID = 1545584792978981006  # channel where lobby embeds are posted
MATCHES_CHANNEL_ID = 1545584792978981006  # channel where private session threads live (same as lobby)

# Leaderboard channel: env RANKED_LB_CHANNEL_ID wins, else the lobby channel.
# Set the env to a dedicated #ranked-leaderboard channel to use it.
def _lb_channel_id() -> int:
    try:
        v = int(os.getenv("RANKED_LB_CHANNEL_ID", "") or 0)
        return v or CREATE_CHANNEL_ID
    except Exception:
        return CREATE_CHANNEL_ID


LB_CHANNEL_ID = _lb_channel_id()

# 3-day scheduled board cadence (seconds).
LB_POST_EVERY_S = 3 * 24 * 3600

# Tier icons + colors (mirrors the userscript RANKED_TIERS, including the
# upstream platinium.png filename typo — do NOT "fix" the spelling).
_MILESTONE_ASSETS = "https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/assets"
_MILESTONE_COLORS = {
    "Unranked": "#6b7280", "Bronze": "#CD7F32", "Silver": "#C0C0C0",
    "Gold": "#FFD700", "Platinum": "#E5E7EB", "Diamond": "#B9F2FF",
    "Emerald": "#10b981", "Champion": "#f59e0b", "Master": "#9333EA",
    "Grandmaster": "#ef4444",
}


def milestone_icon(name: str) -> str:
    """Public icon URL for a tier name. Never raises."""
    try:
        slug = "platinium" if str(name) == "Platinum" else str(name or "").lower()
        return f"{_MILESTONE_ASSETS}/{slug}.png" if slug else ""
    except Exception:
        return ""


def milestone_color(name: str) -> str:
    """Hex color for a tier name. Never raises."""
    try:
        return _MILESTONE_COLORS.get(str(name)) or "#6b7280"
    except Exception:
        return "#6b7280"


# Tier roles: one Discord role id per milestone, via env
# RANKED_ROLE_UNRANKED/BRONZE/SILVER/GOLD/PLATINUM/DIAMOND/EMERALD/
# CHAMPION/MASTER/GRANDMASTER. Unset (or unparsable) tiers sync as a
# no-op with a log line — never blocks seals or commands.
def _tier_role_env(name: str):
    try:
        v = int(os.getenv(f"RANKED_ROLE_{str(name).upper()}", "") or 0)
        return v or None
    except Exception:
        return None


MILESTONE_ROLE_IDS = {
    n: _tier_role_env(n)
    for n in ("Unranked", "Bronze", "Silver", "Gold", "Platinum",
              "Diamond", "Emerald", "Champion", "Master", "Grandmaster")
}


def tier_role_id(milestone: str):
    """Discord role id for a tier name, or None when unconfigured."""
    try:
        return MILESTONE_ROLE_IDS.get(str(milestone))
    except Exception:
        return None

ALLOWED_GMS = ("zw_lg", "df", "wwzw")

# Human-readable mode names for embeds (never show raw ids like ZW_LG).
GM_NAMES = {
    "zw_lg": "Late Game Zonewars",
    "zw": "Zone Wars",
    "wwzw": "Duo Wild West",
    "ww": "Solo Wild West",
    "bf": "Boxfights",
    "bf2v2": "Boxfights 2v2",
    "bf_2v2": "Boxfights 2v2",
    "va": "V-Arena",
    "tdm_bvr": "Red Vs Blue",
    "rvb": "Red Vs Blue",
    "r_1v1": "Realistic 1v1",
    "df": "Free Mode",
    "lobby": "Lobby",
}


def gm_name(gm: str) -> str:
    try:
        return GM_NAMES.get(str(gm or "").lower(), str(gm or "").upper())
    except Exception:
        return "Unknown"

# Milestone entry fees: at/above each tier you burn the fee on every start.
# (Unranked <500 pays nothing.) Tune freely — (threshold, name, fee).
MILESTONE_FEES = [(25000, "Grandmaster", 1250), (18000, "Master", 900),
                  (13000, "Champion", 650), (9000, "Emerald", 450),
                  (6000, "Diamond", 300), (4000, "Platinum", 200),
                  (2500, "Gold", 125), (1200, "Silver", 60),
                  (500, "Bronze", 25)]


def milestone_for(points: int):
    try:
        p = int(points or 0)
    except Exception:
        p = 0
    for threshold, name, fee in MILESTONE_FEES:
        if p >= threshold:
            return name, fee
    return "Unranked", 0


def entry_fee(room: dict, player_points: int) -> int:
    """Total burned on start: room fee + milestone fee."""
    _, tier_fee = milestone_for(player_points)
    return int(room.get("fee") or 0) + int(tier_fee)


RULES_TEXT = (
    "1. Roster only — outsiders earn nothing and may get the room reset.\n"
    "2. Host's word is final; host must stay in the room.\n"
    "3. The timer is always 4:30 — no shooting before it hits zero. "
    "Shot early? Open a ticket with proof (clip or screenshot). "
    "Caught shooting early? Blacklist + ban from ranked.\n"
    "4. No smurfing, stream-sniping, teaming in solo, or point boosting — bans apply.\n"
    "5. No exploiting bugs or glitches — report them instead, bans apply.\n"
    "6. If you crash, rejoin fast and tell the host."
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


def get_room_by_thread_id(thread_id: str):
    """Find a ranked room by its Discord thread ID. Returns the open room or None."""
    try:
        tid = str(thread_id or "").strip()
        if not tid:
            return None
        for status in ("live", "lobby"):
            r = _sb().table("ranked_rooms").select("*").eq("thread_id", tid).eq("status", status).limit(1).execute()
            if r.data:
                return r.data[0]
        return None
    except Exception:
        return None


def get_user_active_room(discord_id: str):
    """Find the user's active ranked room (live preferred, then lobby). Returns room dict or None."""
    try:
        did = str(discord_id or "").strip()
        if not did:
            return None
        r = _sb().table("ranked_rooms").select("*").in_("status", ["live", "lobby"]).order("created_at", desc=True).limit(50).execute()
        for status in ("live", "lobby"):
            for row in (r.data or []):
                if row.get("status") == status and did in roster_ids(row):
                    return row
        return None
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
        "host_status": "is_host_playing",
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


def banned_many(discord_ids) -> set:
    """Banned subset of the given ids (single query). Empty set on error."""
    try:
        ids = [str(x) for x in (discord_ids or []) if x]
        if not ids:
            return set()
        r = _sb().table("ranked_bans").select("discord_id").in_("discord_id", ids[:64]).execute()
        return {str(x.get("discord_id")) for x in (r.data or []) if x.get("discord_id")}
    except Exception:
        return set()


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


def get_points_many(discord_ids) -> dict:
    """Points for many players in one query (keeps the event loop alive during
    starts — per-player reads starve interactions past the 3s defer window).
    Missing ids default to 0. Never raises."""
    try:
        ids = [str(x) for x in (discord_ids or []) if x]
        if not ids:
            return {}
        r = _sb().table("ranked_points").select("discord_id,points").in_("discord_id", ids[:64]).execute()
        out = {}
        for x in (r.data or []):
            try:
                out[str(x.get("discord_id"))] = int(x.get("points") or 0)
            except Exception:
                continue
        for i in ids:
            out.setdefault(i, 0)
        return out
    except Exception:
        return {str(x): 0 for x in (discord_ids or []) if x}


def add_points(discord_id: str, delta: int):
    cur = get_points(discord_id)
    _sb().table("ranked_points").upsert({
        "discord_id": str(discord_id),
        "points": cur + int(delta),
        "updated_at": _now(),
    }, on_conflict="discord_id").execute()
    return cur + int(delta)


def sealed_count(rid: str) -> int:
    """How many matches sealed for this room. 0 on error."""
    try:
        r = _sb().table("ranked_results").select("match_version").eq("room_id", rid).execute()
        return len(r.data or [])
    except Exception:
        return 0


def list_live_rooms():
    """Live rooms (for the seal-watch loop). Empty on error."""
    try:
        r = _sb().table("ranked_rooms").select("*").eq("status", "live").limit(50).execute()
        return r.data or []
    except Exception:
        return []


def room_seals(rid: str):
    """Sealed matches for a room, newest first (full rows incl placements).
    Empty on error."""
    try:
        r = _sb().table("ranked_results").select(
            "match_version,gm,winner_discord_id,kills,points,placements"
        ).eq("room_id", rid).order("match_version", desc=True).limit(50).execute()
        return r.data or []
    except Exception:
        return []


def session_standings(rid: str):
    """Per-player session totals across sealed matches.

    Returns ([(did, gained, wins, played)] sorted by gained desc, wins desc,
    matches_sealed). Players with zero sealed rows are absent. Never raises.
    """
    try:
        tot: dict = {}
        n = 0
        for row in room_seals(rid):
            n += 1
            pts = row.get("points") or {}
            if not isinstance(pts, dict):
                continue
            w = str(row.get("winner_discord_id") or "")
            for did, d in pts.items():
                try:
                    e = tot.setdefault(str(did), [0, 0, 0])  # gained, wins, played
                    e[0] += int(d or 0)
                    e[2] += 1
                    if w and str(did) == w:
                        e[1] += 1
                except Exception:
                    continue
        board = sorted(tot.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))
        return board, n
    except Exception:
        return [], 0


def top_points(limit: int = 10):
    """Top arena-point holders: [(discord_id, points)]. Empty on error."""
    try:
        r = _sb().table("ranked_points").select("discord_id,points").order("points", desc=True).limit(limit).execute()
        return [(str(x.get("discord_id")), int(x.get("points") or 0)) for x in (r.data or []) if x.get("discord_id")]
    except Exception:
        return []


def reset_points_all() -> int:
    """Delete every ranked_points row (season reset). Returns rows removed.
    PostgREST needs a filter for deletes: discord_ids are never empty."""
    try:
        r = _sb().table("ranked_points").delete().neq("discord_id", "").execute()
        return len(r.data or [])
    except Exception as e:
        print(f"[ranked] reset_points_all failed: {e}")
        return 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def lobby_embed(room: dict) -> dict:
    """Spec for the lobby embed (bot.py renders it): title, fields, footer."""
    lock = "Open"
    if (room.get("min_points") or 0) > 0:
        tier, _fee = milestone_for(room.get("min_points") or 0)
        lock = f"🔒 {tier} ({room['min_points']}+ points)"
        if room.get("max_points"):
            lock += f" (max {room['max_points']})"
    starts = "Manual start"
    if room.get("starts_at"):
        try:
            ts = int(datetime.fromisoformat(str(room["starts_at"]).replace("Z", "+00:00")).timestamp())
            starts = f"<t:{ts}:R>"
        except Exception:
            starts = str(room.get("starts_at"))
    try:
        rids = roster_ids(room)
        if rids:
            pts_map = get_points_many(rids) or {}
            vals = [int(pts_map.get(d, 0)) for d in rids]
            avg = round(sum(vals) / len(vals), 1)
            avg_ms, _fee = milestone_for(avg)
            avg_line = f"{avg} pts ({avg_ms})"
        else:
            avg_line = "—"
    except Exception:
        avg_line = "—"
    return {
        "title": f"Ranked Lobby {room['id']} — {gm_name(room.get('gm'))}",
        "fields": [
            ("Status", "Lobby — code hidden until start", True),
            ("Players", f"{len(roster_ids(room))}/{room.get('max_players')}", True),
            ("Avg", avg_line, True),
            ("Matches", str(room.get("max_matches")), True),
            ("Lock", lock, True),
            ("Join fee", str(room.get("fee") or 0), True),
            ("Starts", starts, True),
        ],
        "footer": f"{room['id']} · press Join to enter",
    }


def live_embed(room: dict, thread_id=None, host_status: str = "") -> dict:
    """Spec for the flipped-live lobby embed."""
    return {
        "title": f"Ranked LIVE {room['id']} — {gm_name(room.get('gm'))}",
        "fields": [
            ("Status", "Live — code sent to roster DMs", True),
            ("Players", f"{len(roster_ids(room))}/{room.get('max_players')}", True),
            ("Matches", f"{sealed_count(room['id'])}/{room.get('max_matches')}", True),
        ],
        "footer": f"{room['id']} · thread: {thread_id or '—'}",
    }


def ended_embed(room: dict, thread_id=None) -> dict:
    """Spec for the ended lobby embed."""
    return {
        "title": f"Ranked ENDED {room['id']} — {gm_name(room.get('gm'))}",
        "fields": [
            ("Status", "Ended", True),
            ("Players", f"{len(roster_ids(room))}/{room.get('max_players')}", True),
            ("Matches", f"{sealed_count(room['id'])}/{room.get('max_matches')}", True),
        ],
        "footer": f"{room['id']} · session over · thread: {thread_id or room.get('thread_id') or '—'}",
    }


def ban_player(discord_id: str, reason: str = "", banned_by: str = "") -> bool:
    try:
        _sb().table("ranked_bans").upsert({
            "discord_id": str(discord_id),
            "reason": reason or "",
            "banned_by": str(banned_by or ""),
            "created_at": _now(),
        }, on_conflict="discord_id").execute()
        return True
    except Exception:
        return False


def unban_player(discord_id: str) -> bool:
    try:
        _sb().table("ranked_bans").delete().eq("discord_id", str(discord_id)).execute()
        return True
    except Exception:
        return False
