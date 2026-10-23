"""
insights.py -GET /api/insights?discord_id=... | ?game_username=... | ?game_id=...

Player insights computed from stored game events + user row. Pure compute
(compute_insights) is unit-tested; the endpoint only resolves identity,
fetches events, and returns the computation. No secrets.

Response:
{
  "discord_id": "...",
  "matches_analyzed": N,
  "best_session": {room, match_version, gm, kills, deaths, win, created_at} | null,
  "worst_session": {...} | null,
  "biggest_fear": {"player": str, "deaths": int} | null,      # who kills me most
  "biggest_free_kill": {"player": str, "kills": int} | null,  # who I kill most
  "fav_game_mode": str | null,          # max playtime_per_gm
  "mode_mastery": [{gm, kd, win_rate, matches}],
  "recent_form": [{room, match_version, gm, kills, deaths, win, defeat, created_at}],  # last 5
  "clutch_factor": int,                 # wins with <=1 kill
  "playstyle": str | null,              # Aggressor | Anchor | Support | Wildcard
  "fav_teammate": null,                 # needs team persistence (see below)
  "duo_synergy": null,                  # needs team persistence
  "volume": {"matches": N, "avg_kills": float},
}

Best/worst are kills-first (per locked spec): best = max kills
(tie: win > loss, then fewest deaths); worst = min kills
(tie: loss > win, then most deaths). Fewer than 1 real match ->
best/worst null. Team-based fields return null with needs_team_data
until match teams are persisted server-side.
"""

from collections import Counter

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase
from .discovery import _group_match_events, _resolve_identity

router = APIRouter()

_MIN_MATCHES_FOR_EXTREMES = 1

# Game modes with real teams: only co-presence here means "teammate".
# Everywhere else the lobby is opponents, so teammate detection stays silent.
TEAM_GMS = frozenset({"wwzw", "bf2v2", "tdm_bvr"})


def _kd(kills: int, deaths: int) -> float:
    try:
        return round(kills / deaths, 2) if deaths else float(kills or 0)
    except Exception:
        return 0.0


def _playstyle(kills_per_match: float, kd: float, win_rate: float) -> str | None:
    """Coarse archetype from rates. Thresholds are v1 heuristics."""
    try:
        if kills_per_match >= 3 and kd >= 1.5:
            return "Aggressor"
        if kd >= 1.2 and win_rate >= 0.5:
            return "Anchor"
        if kills_per_match < 1.5 and win_rate >= 0.4:
            return "Support"
        return "Wildcard"
    except Exception:
        return None


def compute_insights(events: list[dict], user_row: dict | None = None) -> dict:
    """Pure insights computation over raw bngc_game_events rows + user row."""
    events = events or []
    user_row = user_row or {}
    matches = _group_match_events(events)

    best = worst = None
    if len(matches) >= _MIN_MATCHES_FOR_EXTREMES:
        # kills-first ordering
        def _best_key(m):
            return (m.get("kills", 0), 1 if m.get("win") else 0, -m.get("deaths", 0))

        def _worst_key(m):
            # min kills; tie: loss first, then most deaths -> invert for min()
            return (m.get("kills", 0), 0 if m.get("defeat") else 1, -m.get("deaths", 0))

        best = max(matches, key=_best_key)
        worst = min(matches, key=_worst_key)

    # fear / farm from kill + death event payloads
    killers: Counter = Counter()
    victims: Counter = Counter()
    for ev in events:
        try:
            gd = (ev or {}).get("event_data") or {}
            et = (ev or {}).get("event_type")
            if et == "death" and gd.get("killer"):
                killers[str(gd["killer"])] += 1
            elif et == "kill" and gd.get("victim"):
                victims[str(gd["victim"])] += 1
        except Exception:
            continue
    fear = killers.most_common(1)[0] if killers else None
    farm = victims.most_common(1)[0] if victims else None

    # fav mode from playtime_per_gm
    fav_mode = None
    try:
        ppg = user_row.get("playtime_per_gm") or {}
        if isinstance(ppg, str):
            import json as _j
            ppg = _j.loads(ppg)
        if isinstance(ppg, dict) and ppg:
            scored = {str(k): int(v or 0) for k, v in ppg.items() if str(k).lower() != "lobby"}
            if scored:
                fav_mode = max(scored.items(), key=lambda kv: kv[1])[0]
    except Exception:
        fav_mode = None

    # mode mastery: per-gm kd + win_rate + match count from grouped matches
    mastery = []
    try:
        by_gm: dict[str, dict] = {}
        for m in matches:
            gm = str(m.get("gm") or "unknown")
            d = by_gm.setdefault(gm, {"gm": gm, "matches": 0, "kills": 0, "deaths": 0, "wins": 0})
            d["matches"] += 1
            d["kills"] += int(m.get("kills") or 0)
            d["deaths"] += int(m.get("deaths") or 0)
            d["wins"] += 1 if m.get("win") else 0
        for gm, d in by_gm.items():
            losses = d["matches"] - d["wins"]
            mastery.append({
                "gm": gm,
                "matches": d["matches"],
                "kd": _kd(d["kills"], d["deaths"]),
                "win_rate": round(d["wins"] / d["matches"], 3) if d["matches"] else 0,
                "kills": d["kills"],
                "wins": d["wins"],
                "defeats": losses,
            })
        mastery.sort(key=lambda x: (x["matches"], x["kd"]), reverse=True)
    except Exception:
        mastery = []

    form = matches[:5]
    clutch = sum(1 for m in matches if m.get("win") and int(m.get("kills") or 0) <= 1)

    total_kills = sum(int(m.get("kills") or 0) for m in matches)
    n = len(matches)
    kpm = round(total_kills / n, 2) if n else 0.0
    kills = int(user_row.get("kills") or total_kills)
    deaths = int(user_row.get("deaths") or sum(int(m.get("deaths") or 0) for m in matches))
    wins = int(user_row.get("wins") or sum(1 for m in matches if m.get("win")))
    defeats = int(user_row.get("defeats") or sum(1 for m in matches if m.get("defeat")))
    wr = round(wins / (wins + defeats), 3) if (wins + defeats) else 0
    style = _playstyle(kpm, _kd(kills, deaths), wr) if n else None

    return {
        "matches_analyzed": n,
        "best_session": best,
        "worst_session": worst,
        "biggest_fear": {"player": fear[0], "deaths": fear[1]} if fear else None,
        "biggest_free_kill": {"player": farm[0], "kills": farm[1]} if farm else None,
        "fav_game_mode": fav_mode,
        "mode_mastery": mastery,
        "recent_form": form,
        "clutch_factor": clutch,
        "playstyle": style,
        "fav_teammate": None,
        "duo_synergy": None,
        "needs_team_data": True,
        "volume": {"matches": n, "avg_kills": kpm},
    }


def _match_room_key(gd: dict) -> tuple | None:
    """(room_id, match_version) for team grouping, or None. Pure."""
    try:
        rid = str(gd.get("room_id") or gd.get("room") or gd.get("code") or gd.get("short_code") or "")
        if not rid:
            return None
        return (rid, gd.get("match_version"))
    except Exception:
        return None


async def best_teammate(me_discord_id: str, my_events: list[dict]) -> dict | None:
    """Most-frequent co-player in team modes + joint wins. Async (2 queries).

    Killfeed/win rows are stored per session, so rows from OTHER sessions in
    the same (room_id, match_version) are co-players. Only team-mode groups
    count (elsewhere the lobby is opponents). Returns
    {discord_id, player, shared, joint_wins, win_rate} or None ("not yet
    found"). Never raises; None on any error or no team-mode history.
    """
    try:
        mine = [e for e in (my_events or [])
                if str(((e or {}).get("event_data") or {}).get("gm") or "").lower() in TEAM_GMS
                and (e or {}).get("event_type") in ("killfeed", "kill", "death", "win", "defeat")]
        if not mine or not supabase:
            return None
        res = supabase.table("bngc_game_events").select(
            "discord_id,event_type,event_data"
        ).in_("event_type", ["killfeed", "win"]).order("created_at", desc=True).limit(1000).execute()
        groups: dict[tuple, dict[str, set]] = {}
        for row in (res.data or []):
            try:
                gd = (row or {}).get("event_data") or {}
                if str(gd.get("gm") or "").lower() not in TEAM_GMS:
                    continue
                key = _match_room_key(gd)
                if key is None:
                    continue
                g = groups.setdefault(key, {"dids": set(), "my_win": False})
                did = str((row or {}).get("discord_id") or "")
                if did:
                    g["dids"].add(did)
                if did == str(me_discord_id) and (row or {}).get("event_type") == "win":
                    g["my_win"] = True
            except Exception:
                continue
        shared: dict[str, int] = {}
        joint: dict[str, int] = {}
        for g in groups.values():
            if str(me_discord_id) not in g["dids"]:
                continue
            for did in g["dids"]:
                if did == str(me_discord_id):
                    continue
                shared[did] = shared.get(did, 0) + 1
                if g["my_win"]:
                    joint[did] = joint.get(did, 0) + 1
        if not shared:
            return None
        top = max(shared.items(), key=lambda kv: (kv[1], joint.get(kv[0], 0)))[0]
        name = top[:6]
        try:
            ures = supabase.table("bngc_users").select(
                "discord_username,game_username").eq("discord_id", top).limit(1).execute()
            if ures.data:
                name = str(ures.data[0].get("game_username") or ures.data[0].get("discord_username") or name)
        except Exception:
            pass
        s = shared[top]
        j = joint.get(top, 0)
        return {"discord_id": top, "player": name, "shared": s,
                "joint_wins": j, "win_rate": round(j / s, 3) if s else 0}
    except Exception:
        return None


@router.get("/api/insights")
async def get_insights(request: Request):
    qp = request.query_params
    ident = _resolve_identity(qp.get("discord_id") or "", qp.get("game_username") or "", qp.get("game_id") or "")
    if not ident:
        if not supabase:
            return JSONResponse({"error": "Server not configured"}, status_code=500)
        return JSONResponse({"error": "user not found"}, status_code=404)
    try:
        ures = supabase.table("bngc_users").select(
            "discord_id,kills,deaths,wins,defeats,playtime_per_gm"
        ).eq("discord_id", ident["discord_id"]).limit(1).execute()
        user_row = (ures.data or [{}])[0]
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    try:
        res = supabase.table("bngc_game_events").select(
            "session_id,event_type,event_data,created_at"
        ).eq("discord_id", ident["discord_id"]).order("created_at", desc=True).limit(500).execute()
        out = compute_insights(res.data or [], user_row)
        out["discord_id"] = ident["discord_id"]
        # best teammate fills fav_teammate/duosynergy when team history exists;
        # otherwise they stay null ("not yet found" in the UI).
        try:
            mate = await best_teammate(ident["discord_id"], res.data or [])
            if mate:
                out["fav_teammate"] = {"player": mate["player"], "shared": mate["shared"],
                                       "joint_wins": mate["joint_wins"]}
                out["duo_synergy"] = mate
                out["needs_team_data"] = False
        except Exception:
            pass
        return JSONResponse(out)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
