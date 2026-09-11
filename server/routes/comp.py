"""
comp.py — comp/tourney read API (pure compute + thin endpoints).

  GET /api/comp/tourneys?status=scheduled|running|ended
      -> [{id, code, name, status, series_len, sealed, players, leader, ...}]
  GET /api/comp/tourney?id=
      -> {tourney, registrations, matches, standings}

Statuses are DERIVED (never stored beyond scheduled/open/closed):
  closed -> ended; scheduled -> scheduled; open + >=1 sealed match -> running;
  open + 0 matches -> scheduled.

Standings math (shared by bot, dashboard, Events tab — one implementation):
  per sealed match: kills per discord (kill events, mapped only) +
  win bonus (manual winner_discord_id, else last alive from alv order) +
  placement table by reverse alv-death order (last alive = 1st).
  Aggregate = sum across sealed matches. Points table from the tourney row,
  default kill 1 / win 3 / placement [10,7,5,3,1].
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase, db_io, is_transient_error
from ..lib.logger import log_err

router = APIRouter()


def _db_err(e: Exception, what: str):
    """Transient pool blips degrade (503, retryable); real errors stay 500."""
    try:
        msg = str(e)[:160]
    except Exception:
        msg = "db error"
    try:
        log_err(f"COMP {what} DB error ({msg[:80]})")
    except Exception:
        pass
    if is_transient_error(e):
        return JSONResponse({"error": "temporarily unavailable", "retry": True}, status_code=503)
    return JSONResponse({"error": msg}, status_code=500)

DEFAULT_TABLE = {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}


def derive_status(tourney: dict, sealed_count: int) -> str:
    """created | scheduled | running | ended. Pure, unit-tested.

    Stored lifecycle: created -> scheduled -> started -> ended.
    Display mirrors storage 1:1 (a fresh row reads Created, not Scheduled);
    callers that only know the old three buckets treat created as scheduled.
    """
    try:
        s = str((tourney or {}).get("status") or "created").lower()
    except Exception:
        s = "created"
    if s == "ended":
        return "ended"
    if s == "started":
        return "running"
    if s == "scheduled":
        return "scheduled"
    return "created"


def _table_of(tourney: dict | None) -> dict:
    try:
        t = ((tourney or {}).get("points_table")) or {}
        if isinstance(t, str):
            import json as _j
            t = _j.loads(t)
        kill = int(t.get("kill", DEFAULT_TABLE["kill"]))
        win = int(t.get("win", DEFAULT_TABLE["win"]))
        place = list(t.get("placement", DEFAULT_TABLE["placement"]))
        return {"kill": kill, "win": win, "placement": [int(x) for x in place]}
    except Exception:
        return dict(DEFAULT_TABLE)


def compute_match_points(events: list[dict], table: dict | None = None,
                         winner_override: str | None = None,
                         placements: dict | None = None) -> list[dict]:
    """Points for ONE sealed match. Pure, unit-tested.

    events: comp event rows {etype, discord_id, actor_name, alive}.
    Kills count per mapped discord_id only (twins stay null upstream).
    Placement: manual map first ({did: place}), else reverse alv-death order
    (last alive first). Winner: override, else manual P1, else last alive,
    else None (kills-only scoring).
    Returns [{discord_id, name, kills, placement, won, points}] sorted desc.
    """
    table = table or dict(DEFAULT_TABLE)
    try:
        place_pts = list(table.get("placement") or [])
        kill_pts = int(table.get("kill", 1))
        win_pts = int(table.get("win", 3))
    except Exception:
        place_pts, kill_pts, win_pts = list(DEFAULT_TABLE["placement"]), 1, 3
    kills: dict[str, int] = {}
    names: dict[str, str] = {}
    death_order: list[str] = []  # first out -> last out
    seen_dead: set[str] = set()
    for e in (events or []):
        try:
            et = (e or {}).get("etype")
            did = (e or {}).get("discord_id")
            if et == "kill" and did:
                kills[did] = kills.get(did, 0) + 1
                if (e or {}).get("actor_name") and did not in names:
                    names[did] = str(e["actor_name"])
            elif et == "alv" and did and (e or {}).get("alive") is False:
                if did not in seen_dead:
                    seen_dead.add(did)
                    death_order.append(did)
                if (e or {}).get("actor_name") and did not in names:
                    names[did] = str(e["actor_name"])
            elif did and (e or {}).get("actor_name") and did not in names:
                names[did] = str(e["actor_name"])
        except Exception:
            continue
    order = list(reversed(death_order))  # last alive first
    winner = None
    try:
        if winner_override and str(winner_override) in set(list(kills) + order):
            winner = str(winner_override)
        elif order:
            winner = order[0]
    except Exception:
        winner = None
    placement_of = {did: i + 1 for i, did in enumerate(order)}
    # Manual placement override (dashboard): exact places beat alv order;
    # winner falls back to manual P1 when no override is set.
    try:
        man = {str(k): int(v) for k, v in (placements or {}).items()
               if str(k or "").strip()}
    except Exception:
        man = {}
    if man:
        try:
            known = set(list(kills) + order + list(man))
            placement_of = {did: man[did] for did in sorted(man, key=lambda d: man[d]) if did in known}
            if not winner:
                first = sorted(man.items(), key=lambda kv: kv[1])
                if first and first[0][0] in known:
                    winner = first[0][0]
        except Exception:
            pass
    rows = []
    for did in sorted(set(list(kills) + order + list(man))):
        k = kills.get(did, 0)
        p = placement_of.get(did)
        pts = k * kill_pts
        if winner and did == winner:
            pts += win_pts
        if p and p <= len(place_pts):
            pts += place_pts[p - 1]
        rows.append({"discord_id": did, "name": names.get(did) or did[:6],
                     "kills": k, "placement": p, "won": bool(winner and did == winner),
                     "points": pts})
    rows.sort(key=lambda r: (r["points"], r["kills"]), reverse=True)
    return rows


def group_standings_by_team(board: list[dict], regs: list[dict] | None,
                            names: dict | None = None) -> list[dict]:
    """Fold a per-player series board into team rows. Pure, unit-tested.

    Teams come from accepted regs ({team_label, member_ids}); each team acts
    as one player sharing summed points/kills/wins. Display name is the
    label when set, else @a & @b over member display names. Unregistered
    players (played but never registered) stand alone under their own name.
    Rows carry member_ids so the dashboard can still resolve individuals.
    """
    try:
        regs = regs or []
        names = names or {}
        team_of: dict[str, int] = {}
        teams: list[dict] = []
        for r in regs:
            try:
                members = [str(x) for x in ((r or {}).get("member_ids") or []) if x]
            except Exception:
                members = []
            if not members:
                continue
            label = str((r or {}).get("team_label") or "").strip() or None
            teams.append({"label": label, "member_ids": members,
                          "kills": 0, "wins": 0, "points": 0, "matches": 0,
                          "avg_w": 0.0, "avg_n": 0})
            idx = len(teams) - 1
            for m in members:
                team_of.setdefault(m, idx)
        solos: dict[str, dict] = {}
        for row in (board or []):
            try:
                did = str((row or {}).get("discord_id") or "")
                if not did:
                    continue
                k = int((row or {}).get("kills") or 0)
                w = int((row or {}).get("wins") or 0)
                pts = int((row or {}).get("points") or 0)
                mt = int((row or {}).get("matches") or 0)
                nm = (row or {}).get("name") or names.get(did) or did[:6]
                try:
                    av = (row or {}).get("avg_placement")
                    av = float(av) if av is not None else None
                except Exception:
                    av = None
                if did in team_of:
                    t = teams[team_of[did]]
                    t["kills"] += k
                    t["wins"] += w
                    t["points"] += pts
                    t["matches"] = max(t["matches"], mt)
                    if av is not None and mt > 0:
                        t["avg_w"] += av * mt
                        t["avg_n"] += mt
                else:
                    s = solos.setdefault(did, {"member_ids": [did], "kills": 0,
                                               "wins": 0, "points": 0, "matches": 0,
                                               "name": nm, "avg_w": 0.0, "avg_n": 0})
                    s["kills"] += k
                    s["wins"] += w
                    s["points"] += pts
                    s["matches"] = max(s["matches"], mt)
                    if av is not None and mt > 0:
                        s["avg_w"] += av * mt
                        s["avg_n"] += mt
            except Exception:
                continue

        def _avg_of(t) -> float | None:
            try:
                n = int(t.get("avg_n") or 0)
                return round(float(t.get("avg_w") or 0.0) / n, 1) if n > 0 else None
            except Exception:
                return None

        out = []
        for t in teams:
            try:
                disp = [str(names.get(m) or m[:6]) for m in t["member_ids"]]
            except Exception:
                disp = [str(m)[:6] for m in t["member_ids"]]
            name = t["label"] or ("@" + " & @".join(disp) if disp else "?")
            out.append({"team": True, "team_label": t["label"],
                        "name": name, "member_ids": t["member_ids"],
                        "kills": t["kills"], "wins": t["wins"],
                        "points": t["points"], "matches": t["matches"],
                        "avg_placement": _avg_of(t)})
        for did, s in solos.items():
            out.append({"team": False, "discord_id": did, "name": s["name"],
                        "member_ids": s["member_ids"], "kills": s["kills"],
                        "wins": s["wins"], "points": s["points"],
                        "matches": s["matches"], "avg_placement": _avg_of(s)})
        out.sort(key=lambda r: (r["points"], r["kills"]), reverse=True)
        return out
    except Exception:
        return list(board or [])


def aggregate_standings(per_match: list[list[dict]]) -> list[dict]:
    """Sum match rows into a series board. Pure, unit-tested.

    Adds avg_placement (mean of per-match placements, 1 decimal; None when
    the player has no placed match, e.g. kills-only scoring).
    """
    tot: dict[str, dict] = {}
    try:
        for rows in (per_match or []):
            for r in (rows or []):
                did = str((r or {}).get("discord_id") or "")
                if not did:
                    continue
                d = tot.setdefault(did, {"discord_id": did, "name": (r or {}).get("name") or did[:6],
                                         "kills": 0, "wins": 0, "points": 0, "matches": 0,
                                         "placements": []})
                d["kills"] += int((r or {}).get("kills") or 0)
                d["wins"] += 1 if (r or {}).get("won") else 0
                d["points"] += int((r or {}).get("points") or 0)
                d["matches"] += 1
                try:
                    p = (r or {}).get("placement")
                    if p is not None:
                        d["placements"].append(int(p))
                except Exception:
                    pass
    except Exception:
        pass
    out = []
    for d in tot.values():
        try:
            pl = d.pop("placements", [])
            d["avg_placement"] = round(sum(pl) / len(pl), 1) if pl else None
        except Exception:
            d["avg_placement"] = None
        out.append(d)
    out.sort(key=lambda r: (r["points"], r["kills"]), reverse=True)
    return out


def standings_for_matches(tourney: dict | None, sealed: list[dict], events_by_match: dict):
    """(per_match, board) for sealed matches. Pure core, unit-tested.

    sealed: match rows {id, winner_discord_id}; events_by_match: match_id ->
    event rows. Points table from the tourney row.
    """
    try:
        table = _table_of(tourney)
    except Exception:
        table = dict(DEFAULT_TABLE)
    per_match = []
    try:
        for m in (sealed or []):
            try:
                per_match.append(compute_match_points(
                    (events_by_match or {}).get((m or {}).get("id"), []),
                    table, (m or {}).get("winner_discord_id"),
                    (m or {}).get("placements")))
            except Exception:
                per_match.append([])
    except Exception:
        per_match = []
    try:
        board = aggregate_standings(per_match)
    except Exception:
        board = []
    return per_match, board


def code_tourney_points(code: str | None, discord_id: str | None) -> dict | None:
    """My live points for the tourney tagged to this room code. For the
    passthrough HUD. Returns {tourney_id, name, points, kills, wins, rank,
    players} or None (no tourney / no rows / DB down). Never raises."""
    try:
        code = str(code or "").strip().upper()
        did = str(discord_id or "").strip()
        if not code or not did or not supabase:
            return None
        tres = supabase.table("bngc_comp_tourneys").select(
            "id,name,points_table").eq("code", code).neq(
            "status", "ended").limit(2).execute()
        rows = tres.data or []
        if len(rows) != 1:
            return None
        t = rows[0]
        mres = supabase.table("bngc_comp_matches").select(
            "id,winner_discord_id,placements").eq("tourney_id", t["id"]).in_(
            "status", ["sealed", "manual"]).order("version").limit(200).execute()
        sealed = mres.data or []
        if not sealed:
            return {"tourney_id": t["id"], "name": t.get("name"), "points": 0,
                    "kills": 0, "wins": 0, "rank": None, "players": 0}
        table = _table_of(t)
        mids = [m["id"] for m in sealed]
        eres = supabase.table("bngc_comp_events").select(
            "match_id,etype,discord_id,actor_name,alive").in_(
            "match_id", mids[:200]).limit(5000).execute()
        by_mid: dict = {}
        for e in (eres.data or []):
            by_mid.setdefault((e or {}).get("match_id"), []).append(e)
        per_match, board = standings_for_matches(t, sealed, by_mid)
        me = next((r for r in board if str(r.get("discord_id")) == did), None)
        rank = next((i + 1 for i, r in enumerate(board) if str(r.get("discord_id")) == did), None)
        if me is None:
            return {"tourney_id": t["id"], "name": t.get("name"), "points": 0,
                    "kills": 0, "wins": 0, "avg_placement": None,
                    "rank": None, "players": len(board)}
        return {"tourney_id": t["id"], "name": t.get("name"),
                "points": int(me.get("points") or 0), "kills": int(me.get("kills") or 0),
                "wins": int(me.get("wins") or 0), "avg_placement": me.get("avg_placement"),
                "rank": rank, "players": len(board)}
    except Exception:
        return None


def open_tourney_points(discord_ids: list[str]):
    """Comp points per user across OPEN tourneys. For drawer/leaderboard display.

    Returns {did: {points, kills, wins, tourney_id, tourney_name}}. Empty dict
    (never raises) when no open tourneys, no sealed matches, or DB down.
    Bounded: 20 tourneys, 200 matches, 5000 events.
    """
    try:
        if not supabase or not discord_ids:
            return {}
        want = sorted({str(d) for d in discord_ids if str(d or "").strip()})[:200]
        if not want:
            return {}
        tres = supabase.table("bngc_comp_tourneys").select(
            "id,name,points_table").eq("status", "started").limit(20).execute()
        trous = tres.data or []
        if not trous:
            return {}
        tids = [t["id"] for t in trous]
        mres = supabase.table("bngc_comp_matches").select(
            "id,tourney_id,winner_discord_id,placements").in_("tourney_id", tids).in_(
            "status", ["sealed", "manual"]).limit(200).execute()
        sealed = mres.data or []
        if not sealed:
            return {}
        mids = [m["id"] for m in sealed]
        # Full event scope (NOT filtered by user): placement order and the
        # win bonus depend on every player's alv rows. Filtering per user
        # made everyone the "winner" of their own view (9W instead of 7W).
        # Row selection by `want` happens after scoring, below.
        eres = supabase.table("bngc_comp_events").select(
            "match_id,etype,discord_id,actor_name,alive").in_(
            "match_id", mids[:200]).limit(5000).execute()
        by_mid: dict = {}
        for e in (eres.data or []):
            by_mid.setdefault((e or {}).get("match_id"), []).append(e)
        by_tid: dict = {}
        for m in sealed:
            by_tid.setdefault((m or {}).get("tourney_id"), []).append(m)
        out: dict[str, dict] = {}
        for t in trous:
            tmap = {m["id"]: m for m in by_tid.get(t["id"], [])}
            evs = {mid: by_mid.get(mid, []) for mid in tmap}
            per_match, board = standings_for_matches(t, list(tmap.values()), evs)
            _ = per_match
            for r in board:
                did = str(r.get("discord_id"))
                if did in want:
                    cur = out.get(did)
                    if cur is None or r.get("points", 0) > cur.get("points", 0):
                        out[did] = {"points": r.get("points", 0), "kills": r.get("kills", 0),
                                    "wins": r.get("wins", 0),
                                    "avg_placement": r.get("avg_placement"),
                                    "tourney_id": t["id"],
                                    "tourney_name": t.get("name")}
        return out
    except Exception:
        return {}


def _need():
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    return None


@router.get("/api/comp/tourneys")
async def list_tourneys(request: Request):
    err = _need()
    if err:
        return err
    want = str(request.query_params.get("status") or "all").strip().lower()
    if want not in ("all", "created", "scheduled", "running", "ended"):
        return JSONResponse({"error": "status must be all|created|scheduled|running|ended"}, status_code=400)
    try:
        tres = supabase.table("bngc_comp_tourneys").select(
            "id,code,name,status,series_len,scheduled_at,started_at,ended_at,created_at"
        ).order("created_at", desc=False).limit(100).execute()
        rows = tres.data or []
        tids = [r["id"] for r in rows]
        sealed_by: dict = {}
        players_by: dict = {}
        if tids:
            try:
                mres = supabase.table("bngc_comp_matches").select(
                    "tourney_id,status").in_("tourney_id", tids).limit(2000).execute()
                for m in (mres.data or []):
                    if (m or {}).get("status") in ("sealed", "manual"):
                        sealed_by[m["tourney_id"]] = sealed_by.get(m["tourney_id"], 0) + 1
            except Exception:
                pass
            try:
                rres = supabase.table("bngc_comp_registrations").select(
                    "tourney_id").eq("status", "accepted").in_("tourney_id", tids).limit(2000).execute()
                for r in (rres.data or []):
                    players_by[r["tourney_id"]] = players_by.get(r["tourney_id"], 0) + 1
            except Exception:
                pass
        out = []
        for t in rows:
            st = derive_status(t, sealed_by.get(t["id"], 0))
            if want != "all" and st != want:
                continue
            out.append({"id": t["id"], "code": t.get("code"), "name": t.get("name"),
                        "status": st, "series_len": t.get("series_len") or 0,
                        "sealed": sealed_by.get(t["id"], 0),
                        "players": players_by.get(t["id"], 0),
                        "scheduled_at": t.get("scheduled_at"),
                        "started_at": t.get("started_at"),
                        "ended_at": t.get("ended_at")})
        return JSONResponse({"count": len(out), "tourneys": out})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/comp/tourney")
async def tourney_detail(request: Request):
    err = _need()
    if err:
        return err
    try:
        tid = int(request.query_params.get("id"))
    except Exception:
        return JSONResponse({"error": "id required (numeric, from 0)"}, status_code=400)
    try:
        tres = supabase.table("bngc_comp_tourneys").select("*").eq("id", tid).limit(1).execute()
        if not tres.data:
            return JSONResponse({"error": "tourney not found"}, status_code=404)
        t = tres.data[0]
        try:
            mres = supabase.table("bngc_comp_matches").select(
                "id,version,room_id,status,winner_discord_id,placements,sealed_at"
            ).eq("tourney_id", tid).order("version").limit(200).execute()
            matches = mres.data or []
        except Exception:
            matches = []
        sealed = [m for m in matches if (m or {}).get("status") in ("sealed", "manual")]
        table = _table_of(t)
        per_match, board = [], []
        try:
            mids = [m["id"] for m in sealed]
            by_mid: dict = {}
            if mids:
                eres = supabase.table("bngc_comp_events").select(
                    "match_id,etype,discord_id,actor_name,alive"
                ).in_("match_id", mids[:200]).limit(5000).execute()
                for e in (eres.data or []):
                    by_mid.setdefault((e or {}).get("match_id"), []).append(e)
            per_match, board = standings_for_matches(t, sealed, by_mid)
        except Exception:
            per_match, board = [], []
        # winner names for the match list (sealed only; open matches resolve
        # live in the match endpoint below).
        winners: dict = {}
        try:
            for i, m in enumerate(sealed):
                rows = per_match[i] if i < len(per_match) else []
                w = next((rw.get("name") for rw in rows if rw.get("won")), None)
                if w:
                    winners[m.get("id")] = w
        except Exception:
            pass
        try:
            rres = supabase.table("bngc_comp_registrations").select(
                "team_label,member_ids,captain_id,status"
            ).eq("tourney_id", tid).eq("status", "accepted").limit(500).execute()
            regs = rres.data or []
        except Exception:
            regs = []
        names: dict[str, str] = {}
        try:
            dids = sorted({str(x) for r in regs for x in (r.get("member_ids") or []) if x})[:200]
            if dids:
                ures = supabase.table("bngc_users").select(
                    "discord_id,game_username,discord_username").in_("discord_id", dids).execute()
                for u in (ures.data or []):
                    names[str(u.get("discord_id"))] = str(
                        u.get("game_username") or u.get("discord_username") or u.get("discord_id"))
        except Exception:
            pass
        # Duo mode: the series board shows teams (@a & @b sharing summed
        # points/kills/wins) instead of individuals. Per-player source rows
        # stay untouched (calculate/results, match boards, bot lines read the
        # same shapes as before).
        try:
            if str(t.get("mode") or "solo").lower() == "duo":
                board = group_standings_by_team(board, regs, names)
        except Exception:
            pass
        return JSONResponse({
            "tourney": {"id": t["id"], "code": t.get("code"), "name": t.get("name"),
                        "status": derive_status(t, len(sealed)),
                        "mode": str(t.get("mode") or "solo").lower(),
                        "series_len": t.get("series_len") or 0, "sealed": len(sealed),
                        "points_table": table, "scheduled_at": t.get("scheduled_at"),
                        "started_at": t.get("started_at"), "ended_at": t.get("ended_at")},
            "players": [{"discord_id": str(x), "name": names.get(str(x), str(x)[:6])}
                        for r in regs for x in (r.get("member_ids") or []) if x],
            "matches": [{"id": m.get("id"), "version": m.get("version"), "status": m.get("status"),
                          "room_id": (str(m.get("room_id") or "")[:8] or None),
                          "winner": winners.get(m.get("id"))}
                         for m in matches],
            "match_boards": [{"version": m.get("version"),
                              "rows": (per_match[i] if i < len(per_match) else [])[:10]}
                             for i, m in enumerate(sealed)],
            "standings": board,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def _json(request: Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


@router.get("/api/comp/match")
async def comp_match_detail(request: Request):
    """One match for the dashboard match view: live board + killfeed.

    Works for open matches too (board computed from events so far).
    Returns {match, tourney, board, killfeed}. Never raises."""
    err = _need()
    if err:
        return err
    try:
        mid = int(request.query_params.get("id"))
    except Exception:
        return JSONResponse({"error": "id required (match id)"}, status_code=400)
    try:
        mres = supabase.table("bngc_comp_matches").select(
            "id,tourney_id,version,room_id,status,winner_discord_id,placements,started_at,sealed_at"
        ).eq("id", mid).limit(1).execute()
        mrows = mres.data or []
        if not mrows:
            return JSONResponse({"error": "match not found"}, status_code=404)
        m = mrows[0]
        try:
            tres = supabase.table("bngc_comp_tourneys").select(
                "id,name,code,status,points_table").eq(
                "id", m.get("tourney_id")).limit(1).execute()
            t = (tres.data or [{}])[0]
        except Exception:
            t = {}
        table = _table_of(t)
        try:
            eres = supabase.table("bngc_comp_events").select(
                "id,match_id,etype,actor_nr,actor_name,target_name,discord_id,alive,created_at"
            ).eq("match_id", mid).order("id").limit(1000).execute()
            events = eres.data or []
        except Exception:
            events = []
        board = compute_match_points(events, table, m.get("winner_discord_id"), m.get("placements"))
        # display names: registered game_username wins over raw actor names.
        try:
            dids = sorted({str(r.get("discord_id")) for r in board if r.get("discord_id")})[:200]
            if dids:
                ures = supabase.table("bngc_users").select(
                    "discord_id,game_username,discord_username").in_("discord_id", dids).execute()
                nmap = {str(u.get("discord_id")): str(
                    u.get("game_username") or u.get("discord_username") or u.get("discord_id"))
                    for u in (ures.data or []) if u.get("discord_id")}
                for r in board:
                    if nmap.get(str(r.get("discord_id"))):
                        r["name"] = nmap[str(r.get("discord_id"))]
        except Exception:
            pass
        feed = [{"id": e.get("id"), "etype": e.get("etype"), "actor_nr": e.get("actor_nr"),
                 "actor_name": e.get("actor_name"), "target_name": e.get("target_name"),
                 "discord_id": e.get("discord_id"), "alive": e.get("alive"),
                 "created_at": e.get("created_at")} for e in events]
        return JSONResponse({
            "match": {"id": m.get("id"), "tourney_id": m.get("tourney_id"),
                      "version": m.get("version"), "status": m.get("status"),
                      "room_id": m.get("room_id"),
                      "winner_discord_id": m.get("winner_discord_id"),
                      "placements": m.get("placements") or {},
                      "started_at": m.get("started_at"), "sealed_at": m.get("sealed_at")},
            "tourney": {"id": t.get("id"), "name": t.get("name"), "code": t.get("code"),
                        "status": t.get("status"), "points_table": table},
            "board": board,
            "killfeed": feed,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _next_version(tourney_id: int) -> int:
    try:
        res = supabase.table("bngc_comp_matches").select("version").eq(
            "tourney_id", int(tourney_id)).order("version", desc=True).limit(1).execute()
        rows = res.data or []
        return int(rows[0].get("version", -1)) + 1 if rows else 0
    except Exception:
        return 0


@router.post("/api/comp/match/open")
async def comp_match_open(request: Request):
    """Manual blank match (dashboard). Never raises beyond JSON errors."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        tid = int(b.get("tourney_id"))
    except Exception:
        return JSONResponse({"error": "tourney_id required"}, status_code=400)
    try:
        ver = _next_version(tid)
        res = supabase.table("bngc_comp_matches").insert({
            "tourney_id": tid, "version": ver,
            "room_id": (str(b.get("room_id") or "").strip() or None),
            "status": "manual",
        }).execute()
        row = (res.data or [{}])[0]
        return JSONResponse({"ok": True, "match_id": row.get("id"), "version": ver})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/match/seal")
async def comp_match_seal(request: Request):
    """Seal an open/manual match (dashboard end-freeze)."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        mid = int(b.get("match_id"))
    except Exception:
        return JSONResponse({"error": "match_id required"}, status_code=400)
    try:
        from datetime import datetime, timezone
        res = await db_io(lambda: supabase.table("bngc_comp_matches").update(
            {"status": "sealed", "sealed_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", mid).in_("status", ["open", "manual"]).execute())
        if not res.data:
            return JSONResponse({"error": "match not found or already sealed"}, status_code=404)
        try:
            log_comp(f"sealed match={mid} via dashboard")
        except Exception:
            pass
        return JSONResponse({"ok": True, "match_id": mid})
    except Exception as e:
        return _db_err(e, f"seal match={mid}")


@router.post("/api/comp/match/winner")
async def comp_match_winner(request: Request):
    """Set/clear the manual winner override (dashboard)."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        mid = int(b.get("match_id"))
    except Exception:
        return JSONResponse({"error": "match_id required"}, status_code=400)
    try:
        who = b.get("winner_discord_id")
        who = str(who).strip() if who else None
        res = supabase.table("bngc_comp_matches").update(
            {"winner_discord_id": who}).eq("id", mid).execute()
        if not res.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        return JSONResponse({"ok": True, "match_id": mid, "winner_discord_id": who})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _valid_table(t) -> dict | None:
    """Validated points table or None. Pure."""
    try:
        if not isinstance(t, dict):
            return None
        kill = int(t.get("kill"))
        win = int(t.get("win"))
        place = [int(x) for x in (t.get("placement") or [])]
        if kill < 0 or win < 0 or not place or len(place) > 10 or any(p < 0 for p in place):
            return None
        return {"kill": kill, "win": win, "placement": place}
    except Exception:
        return None


@router.post("/api/comp/tourney/table")
async def comp_tourney_table(request: Request):
    """Replace a tourney's points table (dashboard editor)."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        tid = int(b.get("tourney_id"))
    except Exception:
        return JSONResponse(
            {"error": "tourney_id required (pick a tourney in the dashboard list first)"},
            status_code=400)
    table = _valid_table(b.get("points_table"))
    if table is None:
        return JSONResponse(
            {"error": "points_table needs {kill>=0, win>=0, placement:[1..10 non-neg ints]}"},
            status_code=400)
    try:
        res = supabase.table("bngc_comp_tourneys").update(
            {"points_table": table}).eq("id", tid).execute()
        if not res.data:
            return JSONResponse({"error": "tourney not found"}, status_code=404)
        return JSONResponse({"ok": True, "tourney_id": tid, "points_table": table})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/tourney/host-playing")
async def comp_tourney_host_playing(request: Request):
    """Flip whether the host plays (dashboard toggle + bot host-status).

    When false the bridge ignores the host's own kills/alv rows."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        tid = int(b.get("tourney_id"))
    except Exception:
        return JSONResponse(
            {"error": "tourney_id required (pick a tourney in the dashboard list first)"},
            status_code=400)
    if b.get("host_playing") is None:
        return JSONResponse({"error": "host_playing required (true/false)"}, status_code=400)
    hp = bool(b.get("host_playing"))
    try:
        res = supabase.table("bngc_comp_tourneys").update(
            {"host_playing": hp}).eq("id", tid).execute()
        if not res.data:
            return JSONResponse({"error": "tourney not found"}, status_code=404)
        return JSONResponse({"ok": True, "tourney_id": tid, "host_playing": hp})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/tourney/hosts")
async def comp_tourney_hosts(request: Request):
    """Add/remove a per-tourney host (dashboard hosts UI + bot).

    Body: {tourney_id, add: discord_id} or {tourney_id, remove: discord_id}
    (exactly one). Returns the resulting hosts list."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        tid = int(b.get("tourney_id"))
    except Exception:
        return JSONResponse(
            {"error": "tourney_id required (pick a tourney in the dashboard list first)"},
            status_code=400)
    add = b.get("add")
    remove = b.get("remove")
    add = str(add).strip() if add is not None else ""
    remove = str(remove).strip() if remove is not None else ""
    if bool(add) == bool(remove):
        return JSONResponse(
            {"error": "pass exactly one of add/remove (discord id)"}, status_code=400)
    try:
        rres = supabase.table("bngc_comp_tourneys").select("hosts").eq(
            "id", tid).limit(1).execute()
        if not rres.data:
            return JSONResponse({"error": "tourney not found"}, status_code=404)
        hosts = [str(x) for x in ((rres.data[0] or {}).get("hosts") or [])
                 if str(x or "").strip()]
        if add:
            if add not in hosts:
                hosts.append(add)
        else:
            hosts = [h for h in hosts if h != remove]
        ures = supabase.table("bngc_comp_tourneys").update(
            {"hosts": hosts}).eq("id", tid).execute()
        if not ures.data:
            return JSONResponse({"error": "tourney not found"}, status_code=404)
        return JSONResponse({"ok": True, "tourney_id": tid, "hosts": hosts})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _calculate_sync(trous: dict, matches: list, by_mid: dict,
                    computed_by: str = "dashboard") -> dict:
    """Shared calculate core: per-match points + aggregate boards + snapshot
    rows into bngc_comp_results. Sync (direct supabase calls, for request and
    bridge threads alike). Never raises; returns {str(tid): board}."""
    boards: dict = {}
    try:
        from datetime import datetime, timezone
        by_tid: dict = {}
        for m in matches or []:
            by_tid.setdefault((m or {}).get("tourney_id"), []).append(m)
        for tid, ms in by_tid.items():
            trow = (trous or {}).get(tid) or {}
            table = _table_of(trow)
            per: dict = {}
            for m in ms:
                try:
                    per[m["id"]] = compute_match_points(
                        (by_mid or {}).get(m["id"], []), table, (m or {}).get("winner_discord_id"),
                        (m or {}).get("placements"))
                except Exception:
                    per[m["id"]] = []
            _pm = [per[m["id"]] for m in ms]
            boards[str(tid)] = aggregate_standings(_pm)
            for m in ms:
                try:
                    supabase.table("bngc_comp_results").upsert({
                        "match_id": m["id"], "standings": per[m["id"]],
                        "computed_at": datetime.now(timezone.utc).isoformat(),
                        "computed_by": str(computed_by or "dashboard"),
                    }, on_conflict="match_id").execute()
                except Exception:
                    pass
    except Exception:
        pass
    return boards


def calculate_tourney_sync(tid: int, computed_by: str = "bridge") -> list:
    """Tourney-branch calculate for non-request callers (bridge auto-calc
    after every seal, so stored placements never go stale). Sync. Never
    raises; returns the board list."""
    try:
        if not supabase:
            return []
        trows = supabase.table("bngc_comp_tourneys").select(
            "id,points_table").eq("id", int(tid)).limit(1).execute()
        if not trows.data:
            return []
        mres = supabase.table("bngc_comp_matches").select(
            "id,tourney_id,version,room_id,status,winner_discord_id,placements").eq(
            "tourney_id", int(tid)).in_("status", ["sealed", "manual"]).order(
            "version").limit(200).execute()
        matches = mres.data or []
        if not matches:
            return []
        mids = [m["id"] for m in matches]
        eres = supabase.table("bngc_comp_events").select(
            "match_id,etype,discord_id,actor_name,alive").in_(
            "match_id", mids[:200]).limit(5000).execute()
        by_mid: dict = {}
        for e in (eres.data or []):
            by_mid.setdefault((e or {}).get("match_id"), []).append(e)
        boards = _calculate_sync({int(tid): trows.data[0]}, matches, by_mid, computed_by)
        return boards.get(str(int(tid)), [])
    except Exception:
        return []


@router.post("/api/comp/calculate")
async def comp_calculate(request: Request):
    """Compute standings with the shared math and snapshot per-match rows
    into bngc_comp_results (dashboard Calculate button). Accepts
    {tourney_id} (all sealed) or {match_id} (one). Returns the board."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        from datetime import datetime, timezone
        tids: list[int] = []
        mids: list[int] | None = None
        if b.get("match_id") is not None:
            try:
                mids = [int(b.get("match_id"))]
            except Exception:
                return JSONResponse({"error": "match_id must be a number"}, status_code=400)
            mres = supabase.table("bngc_comp_matches").select(
                "id,tourney_id,version,room_id,status,winner_discord_id,placements").in_("id", mids).execute()
            matches = [m for m in (mres.data or []) if (m or {}).get("status") in ("sealed", "manual")]
            if not matches:
                return JSONResponse({"error": "match not found or not sealed"}, status_code=404)
            tmap = {m["id"]: m for m in matches}
            trows = supabase.table("bngc_comp_tourneys").select(
                "id,points_table").in_("id", list({m["tourney_id"] for m in matches})).execute()
            trous = {t["id"]: t for t in (trows.data or [])}
        else:
            try:
                tid = int(b.get("tourney_id"))
            except Exception:
                return JSONResponse(
                    {"error": "tourney_id required (pick a tourney in the dashboard list first)"},
                    status_code=400)
            tids = [tid]
            trows = supabase.table("bngc_comp_tourneys").select(
                "id,points_table").eq("id", tid).limit(1).execute()
            if not trows.data:
                return JSONResponse({"error": "tourney not found"}, status_code=404)
            trous = {tid: trows.data[0]}
            mres = supabase.table("bngc_comp_matches").select(
                "id,tourney_id,version,room_id,status,winner_discord_id,placements").eq(
                "tourney_id", tid).in_("status", ["sealed", "manual"]).order("version").limit(200).execute()
            matches = mres.data or []
            mids = [m["id"] for m in matches]
            tmap = {m["id"]: m for m in matches}
        by_mid: dict = {}
        if mids:
            eres = supabase.table("bngc_comp_events").select(
                "match_id,etype,discord_id,actor_name,alive").in_(
                "match_id", mids[:200]).limit(5000).execute()
            for e in (eres.data or []):
                by_mid.setdefault((e or {}).get("match_id"), []).append(e)
        boards = _calculate_sync(trous, matches, by_mid,
                                 str(b.get("computed_by") or "dashboard"))
        first_tid = matches[0]["tourney_id"] if matches else None
        first = boards.get(str(first_tid), [])
        return JSONResponse({"ok": True, "standings": first, "tourneys": list(boards)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/comp/regs")
async def comp_regs(request: Request):
    """Registration queue for a tourney (dashboard moderation view)."""
    err = _need()
    if err:
        return err
    try:
        tid = int(request.query_params.get("tourney_id"))
    except Exception:
        return JSONResponse({"error": "tourney_id required"}, status_code=400)
    want = str(request.query_params.get("status") or "").lower() or None
    if want and want not in ("pending", "accepted", "rejected", "banned"):
        return JSONResponse({"error": "bad status"}, status_code=400)
    try:
        q = supabase.table("bngc_comp_registrations").select(
            "id,team_label,member_ids,captain_id,status,created_at").eq("tourney_id", tid)
        if want:
            q = q.eq("status", want)
        res = q.order("created_at").limit(500).execute()
        return JSONResponse({"count": len(res.data or []), "regs": res.data or []})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/comp/names")
async def comp_names(request: Request):
    """Batch discord_id -> display name (dashboard player lists)."""
    err = _need()
    if err:
        return err
    try:
        raw = str(request.query_params.get("ids") or "")
        dids = sorted({x.strip() for x in raw.split(",") if x.strip()})[:100]
        if not dids:
            return JSONResponse({"names": {}})
        res = supabase.table("bngc_users").select(
            "discord_id,game_username,discord_username").in_("discord_id", dids).execute()
        out = {}
        for u in (res.data or []):
            out[str(u.get("discord_id"))] = str(
                u.get("game_username") or u.get("discord_username") or u.get("discord_id"))
        return JSONResponse({"names": out})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/reg/status")
async def comp_reg_status(request: Request):
    """Dashboard moderation mirror (accept/reject/ban a registration)."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        rid = int(b.get("reg_id"))
    except Exception:
        return JSONResponse({"error": "reg_id required"}, status_code=400)
    st = str(b.get("status") or "").lower()
    if st not in ("accepted", "rejected", "banned", "pending"):
        return JSONResponse({"error": "status must be accepted|rejected|banned|pending"}, status_code=400)
    try:
        res = supabase.table("bngc_comp_registrations").update(
            {"status": st}).eq("id", rid).execute()
        if not res.data:
            return JSONResponse({"error": "registration not found"}, status_code=404)
        return JSONResponse({"ok": True, "reg_id": rid, "status": st})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _comp_display_name(discord_id: str) -> str:
    try:
        if not supabase or not discord_id:
            return str(discord_id or "")[:6]
        res = supabase.table("bngc_users").select(
            "game_username,discord_username").eq("discord_id", str(discord_id)).limit(1).execute()
        rows = res.data or []
        if rows:
            return str(rows[0].get("game_username") or rows[0].get("discord_username")
                       or discord_id)[:32]
    except Exception:
        pass
    return str(discord_id or "")[:6]


@router.post("/api/comp/match/kills")
async def comp_match_kills(request: Request):
    """Hand-adjust a player's kills in a match (dashboard +kill button).

    Positive delta inserts manual kill rows; negative removes that many of
    the player's manual rows (newest first). Tracked (non-manual) rows are
    never touched. Returns the player's new kill total plus how many manual
    rows were actually removed (requested vs removed tells the dashboard when
    tracked kills were kept).
    """
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        mid = int(b.get("match_id"))
        did = str(b.get("discord_id") or "").strip()
        delta = int(b.get("delta") or 0)
    except Exception:
        return JSONResponse({"error": "match_id, discord_id and delta required"}, status_code=400)
    if not did or delta == 0:
        return JSONResponse({"error": "discord_id and non-zero delta required"}, status_code=400)
    if abs(delta) > 50:
        return JSONResponse({"error": "delta capped at ±50"}, status_code=400)
    try:
        mres = supabase.table("bngc_comp_matches").select("id").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        if delta > 0:
            name = _comp_display_name(did)
            supabase.table("bngc_comp_events").insert([{
                "match_id": mid, "etype": "kill", "discord_id": did,
                "actor_name": name, "target_name": "manual", "manual": True,
            }] * delta).execute()
        else:
            eres = supabase.table("bngc_comp_events").select("id").eq(
                "match_id", mid).eq("discord_id", did).eq("etype", "kill").eq(
                "manual", True).order("id", desc=True).limit(abs(delta)).execute()
            removed = 0
            for r in (eres.data or []):
                try:
                    supabase.table("bngc_comp_events").delete().eq("id", r["id"]).execute()
                    removed += 1
                except Exception:
                    pass
        cres = supabase.table("bngc_comp_events").select("id,manual").eq(
            "match_id", mid).eq("discord_id", did).eq("etype", "kill").execute()
        rows = cres.data or []
        tracked = sum(1 for r in rows if not (r or {}).get("manual"))
        return JSONResponse({"ok": True, "match_id": mid, "discord_id": did,
                             "kills": len(rows), "tracked_kept": tracked,
                             "requested": abs(delta) if delta < 0 else delta,
                             "removed_manual": removed if delta < 0 else delta})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/match/place")
async def comp_match_place(request: Request):
    """Set/clear a manual placement for a player in a match (dashboard
    1st–5th buttons). Stored on the match placements map; beats alv order."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        mid = int(b.get("match_id"))
        did = str(b.get("discord_id") or "").strip()
    except Exception:
        return JSONResponse({"error": "match_id and discord_id required"}, status_code=400)
    if not did:
        return JSONResponse({"error": "discord_id required"}, status_code=400)
    try:
        place = b.get("place")
        place = int(place) if place is not None else None
        if place is not None and (place < 1 or place > 30):
            return JSONResponse({"error": "place must be 1..30 or null"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "place must be 1..30 or null"}, status_code=400)
    try:
        mres = supabase.table("bngc_comp_matches").select(
            "id,placements").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        cur = dict((mres.data[0] or {}).get("placements") or {})
        if place is None:
            cur.pop(did, None)
        else:
            # one player per place: evict whoever held it
            for k in [k for k, v in cur.items() if int(v) == place and k != did]:
                del cur[k]
            cur[did] = place
        supabase.table("bngc_comp_matches").update({"placements": cur}).eq("id", mid).execute()
        return JSONResponse({"ok": True, "match_id": mid, "placements": cur})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/comp/match/unmapped")
async def comp_match_unmapped(request: Request):
    """Actor names in a match with no discord link (dashboard link UI).

    Returns [{name, kills, deaths}] so the host can attach each to a player.
    """
    err = _need()
    if err:
        return err
    try:
        mid = int(request.query_params.get("match_id"))
    except Exception:
        return JSONResponse({"error": "match_id required"}, status_code=400)
    try:
        eres = supabase.table("bngc_comp_events").select(
            "etype,discord_id,actor_name,target_name,alive").eq("match_id", mid).limit(5000).execute()
        agg: dict[str, dict] = {}
        for e in (eres.data or []):
            try:
                if (e or {}).get("discord_id"):
                    continue
                for nm_raw in ((e or {}).get("actor_name"), (e or {}).get("target_name")):
                    nm = str(nm_raw or "").strip()
                    if not nm:
                        continue
                    d = agg.setdefault(nm, {"name": nm, "kills": 0, "deaths": 0})
                    if (e or {}).get("etype") == "kill":
                        d["kills"] += 1
                    elif (e or {}).get("etype") in ("death", "knock"):
                        d["deaths"] += 1
                    elif (e or {}).get("etype") == "alv" and (e or {}).get("alive") is False:
                        d["deaths"] += 1
            except Exception:
                continue
        return JSONResponse({"match_id": mid,
                             "unmapped": sorted(agg.values(), key=lambda r: -r["kills"])})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/match/link")
async def comp_match_link(request: Request):
    """Link (or unlink) an actor name to a player for a match (dashboard).

    Writes discord_id directly onto the null rows, so boards, HUD and stored
    results all agree with zero special cases. {match_id, actor_name,
    discord_id|null, apply_to_all?}. Unlink passes null. apply_to_all backfills
    the same actor_name across all matches of the tourney.
    """
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        mid = int(b.get("match_id"))
        name = str(b.get("actor_name") or "").strip()
    except Exception:
        return JSONResponse({"error": "match_id and actor_name required"}, status_code=400)
    if not name:
        return JSONResponse({"error": "actor_name required"}, status_code=400)
    try:
        did = b.get("discord_id")
        did = str(did).strip() if did else None
        apply_all = bool(b.get("apply_to_all"))
        q = supabase.table("bngc_comp_events").update(
            {"discord_id": did}).eq("match_id", mid).is_("discord_id", "null").eq(
            "actor_name", name)
        res = q.execute()
        n = len(res.data or [])
        n_all = 0
        if apply_all and did:
            try:
                mrow = supabase.table("bngc_comp_matches").select(
                    "tourney_id").eq("id", mid).limit(1).execute()
                tid = ((mrow.data or [{}])[0] or {}).get("tourney_id")
                if tid is not None:
                    mids = supabase.table("bngc_comp_matches").select(
                        "id").eq("tourney_id", tid).limit(200).execute()
                    ids = [m.get("id") for m in (mids.data or []) if m.get("id") != mid]
                    for oid in ids[:200]:
                        try:
                            r2 = supabase.table("bngc_comp_events").update(
                                {"discord_id": did}).eq("match_id", oid).is_(
                                "discord_id", "null").eq("actor_name", name).execute()
                            n_all += len(r2.data or [])
                        except Exception:
                            continue
            except Exception:
                pass
        return JSONResponse({"ok": True, "match_id": mid, "actor_name": name,
                             "discord_id": did, "linked": n, "linked_all": n_all})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/comp/tourney/by-code")
async def comp_tourney_by_code(request: Request):
    """Tourney carrying this room code (userscript join detection + bridge).

    Returns the single non-ended tourney for the code, else {tourney: null}.
    Ambiguous (2+ open rows, same code) resolves to null — never guess.
    """
    err = _need()
    if err:
        return err
    try:
        code = str(request.query_params.get("code") or "").strip().upper()
    except Exception:
        code = ""
    if len(code) != 5 or not code.isalnum():
        return JSONResponse({"tourney": None})
    try:
        res = await db_io(lambda: supabase.table("bngc_comp_tourneys").select(
            "id,code,name,status,mode,series_len").eq("code", code).neq(
            "status", "ended").limit(2).execute())
        rows = res.data or []
        if len(rows) != 1:
            return JSONResponse({"tourney": None})
        t = rows[0]
        return JSONResponse({"tourney": {
            "id": t.get("id"), "code": t.get("code"), "name": t.get("name"),
            "status": t.get("status"), "mode": t.get("mode"),
            "series_len": t.get("series_len") or 0}})
    except Exception as e:
        # Join-detection polls this every few seconds — a pool blip must
        # resolve to null (retry on next poll), never a 500 that the
        # userscript treats as fatal.
        if is_transient_error(e):
            try:
                log_err(f"COMP by-code={code} transient ({str(e)[:60]}) → null")
            except Exception:
                pass
            return JSONResponse({"tourney": None})
        return _db_err(e, f"by-code={code}")
