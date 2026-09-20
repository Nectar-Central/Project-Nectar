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

# scoring brain lives in lib/scoring.py (one implementation shared by
# comp and ranked); re-exported here so existing callers and tests
# that import from this module keep working unchanged
from ..lib.scoring import (DEFAULT_TABLE, compute_match_points,
                           group_standings_by_team, aggregate_standings,
                           compute_tourney_pr)


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


def _tourney_adjustments(tid) -> list[dict]:
    """Ledger rows for a tourney: [{discord_id, delta_points}]. Never raises;
    [] when the table is missing/empty/DB down (pre-migration safe)."""
    try:
        if not supabase or tid is None:
            return []
        res = supabase.table("bngc_comp_adjustments").select(
            "discord_id,delta_points").eq("tourney_id", int(tid)).limit(1000).execute()
        out = []
        for r in (res.data or []):
            try:
                did = str((r or {}).get("discord_id") or "").strip()
                if did:
                    out.append({"discord_id": did, "delta_points": int((r or {}).get("delta_points") or 0)})
            except Exception:
                continue
        return out
    except Exception:
        return []


def apply_adjustments(board: list[dict], adjustments: list[dict] | None) -> list[dict]:
    """Fold the adjustments ledger into an aggregate board (global tourney
    standings level, NOT per-match). Pure: sums deltas per player, adds a row
    for adjusted players with no sealed rows, re-sorts. Never raises."""
    try:
        adj: dict[str, int] = {}
        for a in (adjustments or []):
            try:
                did = str((a or {}).get("discord_id") or "").strip()
                if did:
                    adj[did] = adj.get(did, 0) + int((a or {}).get("delta_points") or 0)
            except Exception:
                continue
        if not adj:
            return board
        by_did = {str((r or {}).get("discord_id")): r for r in (board or [])}
        for did, d in adj.items():
            if not d:
                continue
            row = by_did.get(did)
            if row is None:
                row = {"discord_id": did, "name": did[:6], "kills": 0, "wins": 0,
                       "points": max(0, d), "matches": 0, "avg_placement": None, "adjusted": d}
                (board or []).append(row)
                by_did[did] = row
            else:
                row["points"] = max(0, int(row.get("points") or 0) + d)
                row["adjusted"] = int(row.get("adjusted") or 0) + d
        try:
            (board or []).sort(key=lambda r: ((r or {}).get("points", 0), (r or {}).get("kills", 0)), reverse=True)
        except Exception:
            pass
        return board
    except Exception:
        return board


def standings_for_matches(tourney: dict | None, sealed: list[dict], events_by_match: dict,
                          adjustments: list[dict] | None = None):
    """(per_match, board) for sealed matches. Pure core, unit-tested.

    sealed: match rows {id, winner_discord_id}; events_by_match: match_id ->
    event rows. Points table from the tourney row. adjustments: ledger rows
    folded into the aggregate board (global level; per-match rows stay raw).
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
                    (m or {}).get("placements"),
                    (m or {}).get("removed_discord_ids"),
                    (m or {}).get("point_awards")))
            except Exception:
                per_match.append([])
    except Exception:
        per_match = []
    try:
        board = aggregate_standings(per_match)
    except Exception:
        board = []
    try:
        board = apply_adjustments(board, adjustments)
    except Exception:
        pass
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
            "id,winner_discord_id,placements,removed_discord_ids,point_awards").eq("tourney_id", t["id"]).in_(
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
        per_match, board = standings_for_matches(t, sealed, by_mid, _tourney_adjustments(t.get("id")))
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
            "id,tourney_id,winner_discord_id,placements,removed_discord_ids,point_awards").in_("tourney_id", tids).in_(
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
            per_match, board = standings_for_matches(
                t, list(tmap.values()), evs, _tourney_adjustments(t.get("id")))
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
            "id,code,name,status,mode,series_len,scheduled_at,started_at,ended_at,created_at"
        ).order("created_at", desc=False).limit(100).execute()
        rows = tres.data or []
        tids = [r["id"] for r in rows]
        sealed_by: dict = {}
        players_by: dict = {}
        teams_by: dict = {}
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
                    "tourney_id,member_ids").eq("status", "accepted").in_("tourney_id", tids).limit(2000).execute()
                for r in (rres.data or []):
                    try:
                        tid = (r or {}).get("tourney_id")
                        members = ((r or {}).get("member_ids") or [])
                        players_by[tid] = players_by.get(tid, 0) + len(members)
                        teams_by[tid] = teams_by.get(tid, 0) + 1
                    except Exception:
                        continue
            except Exception:
                pass
        out = []
        for t in rows:
            st = derive_status(t, sealed_by.get(t["id"], 0))
            if want != "all" and st != want:
                continue
            out.append({"id": t["id"], "code": t.get("code"), "name": t.get("name"),
                        "status": st, "mode": str(t.get("mode") or "solo").lower(),
                        "series_len": t.get("series_len") or 0,
                        "sealed": sealed_by.get(t["id"], 0),
                        "players": players_by.get(t["id"], 0),
                        "teams": teams_by.get(t["id"], 0),
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
                "id,version,room_id,status,winner_discord_id,placements,removed_discord_ids,point_awards,sealed_at"
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
            per_match, board = standings_for_matches(
                t, sealed, by_mid, _tourney_adjustments(t.get("id")))
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
            try:
                rres = supabase.table("bngc_comp_registrations").select(
                    "team_label,member_ids,member_names,captain_id,status"
                ).eq("tourney_id", tid).eq("status", "accepted").limit(500).execute()
            except Exception:
                rres = supabase.table("bngc_comp_registrations").select(
                    "team_label,member_ids,captain_id,status"
                ).eq("tourney_id", tid).eq("status", "accepted").limit(500).execute()
            regs = rres.data or []
        except Exception:
            regs = []
        # Registration display names first (captured at @mention intake, no
        # raw IDs in lists); bngc_users game names override when present.
        reg_names: dict[str, str] = {}
        try:
            for r in regs:
                for did, nm in (((r or {}).get("member_names")) or {}).items():
                    try:
                        label = str((nm or {}).get("display_name") or (nm or {}).get("username") or "").strip()
                        if did and label and str(did) not in reg_names:
                            reg_names[str(did)] = label[:32]
                    except Exception:
                        continue
        except Exception:
            pass
        names: dict[str, str] = dict(reg_names)
        try:
            dids = sorted({str(x) for r in regs for x in (r.get("member_ids") or []) if x})[:200]
            if dids:
                ures = supabase.table("bngc_users").select(
                    "discord_id,game_username,discord_username").in_("discord_id", dids).execute()
                for u in (ures.data or []):
                    label = str(u.get("game_username") or u.get("discord_username") or u.get("discord_id"))
                    if label:
                        names[str(u.get("discord_id"))] = label
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
            "reg_names": reg_names,
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
        try:
            mres = supabase.table("bngc_comp_matches").select(
                "id,tourney_id,version,room_id,status,winner_discord_id,placements,removed_discord_ids,point_awards,started_at,sealed_at"
            ).eq("id", mid).limit(1).execute()
        except Exception:
            mres = supabase.table("bngc_comp_matches").select(
                "id,tourney_id,version,room_id,status,winner_discord_id,placements,started_at,sealed_at"
            ).eq("id", mid).limit(1).execute()
        mrows = mres.data or []
        if not mrows:
            return JSONResponse({"error": "match not found"}, status_code=404)
        m = mrows[0]
        try:
            removed = [str(x) for x in ((m or {}).get("removed_discord_ids") or []) if str(x or "").strip()]
        except Exception:
            removed = []
        try:
            tres = supabase.table("bngc_comp_tourneys").select(
                "id,name,code,status,points_table").eq(
                "id", m.get("tourney_id")).limit(1).execute()
            t = (tres.data or [{}])[0]
        except Exception:
            t = {}
        table = _table_of(t)
        try:
            try:
                eres = supabase.table("bngc_comp_events").select(
                    "id,match_id,etype,actor_nr,actor_name,target_name,discord_id,alive,manual,created_at"
                ).eq("match_id", mid).order("id").limit(1000).execute()
            except Exception:
                # pre-migration (no manual column): classic shape, manual unknown
                eres = supabase.table("bngc_comp_events").select(
                    "id,match_id,etype,actor_nr,actor_name,target_name,discord_id,alive,created_at"
                ).eq("match_id", mid).order("id").limit(1000).execute()
            events = eres.data or []
        except Exception:
            events = []
        board = compute_match_points(events, table, m.get("winner_discord_id"), m.get("placements"), removed, m.get("point_awards"))
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
                 "manual": bool((e or {}).get("manual")),
                 "created_at": e.get("created_at")} for e in events]
        return JSONResponse({
            "match": {"id": m.get("id"), "tourney_id": m.get("tourney_id"),
                      "version": m.get("version"), "status": m.get("status"),
                      "room_id": m.get("room_id"),
                      "winner_discord_id": m.get("winner_discord_id"),
                      "placements": m.get("placements") or {},
                      "removed_discord_ids": removed,
                      "point_awards": (m.get("point_awards") or {}),
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
    """Manual blank match (dashboard). Never raises beyond JSON errors.

    Manual matches start with started_at set (the dashboard Match Started
    bookend) and status manual — the bridge never auto-seals or writes to
    manual rows, so hosts own them end to end."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        tid = int(b.get("tourney_id"))
    except Exception:
        return JSONResponse({"error": "tourney_id required"}, status_code=400)
    try:
        from datetime import datetime, timezone
        ver = _next_version(tid)
        res = supabase.table("bngc_comp_matches").insert({
            "tourney_id": tid, "version": ver,
            "room_id": (str(b.get("room_id") or "").strip() or None),
            "status": "manual",
            "started_at": datetime.now(timezone.utc).isoformat(),
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


@router.post("/api/comp/match/start")
async def comp_match_start(request: Request):
    """Stamp Match Started on an open/manual match (dashboard).

    Idempotent: a match that already started returns ok with its existing
    timestamp. Sealed matches are rejected (unseal first). Never raises
    beyond JSON errors."""
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
        mres = supabase.table("bngc_comp_matches").select(
            "id,status,started_at").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        m = mres.data[0] or {}
        if (m.get("status") or "") == "sealed":
            return JSONResponse({"error": "match already sealed (unseal first)"}, status_code=409)
        if m.get("started_at"):
            return JSONResponse({"ok": True, "match_id": mid,
                                 "started_at": m.get("started_at")})
        now = datetime.now(timezone.utc).isoformat()
        supabase.table("bngc_comp_matches").update(
            {"started_at": now}).eq("id", mid).execute()
        return JSONResponse({"ok": True, "match_id": mid, "started_at": now})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/match/unseal")
async def comp_match_unseal(request: Request):
    """Reopen a sealed match for corrections (dashboard).

    Sealed -> manual (NOT open: the bridge auto-seals open rows, manual rows
    stay dashboard-owned) with sealed_at cleared. Already-live matches
    return ok unchanged. Never raises beyond JSON errors."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        mid = int(b.get("match_id"))
    except Exception:
        return JSONResponse({"error": "match_id required"}, status_code=400)
    try:
        mres = supabase.table("bngc_comp_matches").select(
            "id,status").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        if (mres.data[0] or {}).get("status") != "sealed":
            return JSONResponse({"ok": True, "match_id": mid,
                                 "status": (mres.data[0] or {}).get("status")})
        res = supabase.table("bngc_comp_matches").update(
            {"status": "manual", "sealed_at": None}).eq("id", mid).execute()
        if not res.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        return JSONResponse({"ok": True, "match_id": mid, "status": "manual"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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
        mres = supabase.table("bngc_comp_matches").select(
            "id,placements").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        patch: dict = {"winner_discord_id": who}
        if who:
            # keep crown + board in agreement: no manual P1 yet -> winner is P1
            try:
                cur = dict(((mres.data[0] or {}).get("placements")) or {})
                if not any(int(v) == 1 for v in cur.values()):
                    cur.pop(who, None)
                    cur[who] = 1
                    patch["placements"] = cur
            except Exception:
                pass
        res = supabase.table("bngc_comp_matches").update(patch).eq("id", mid).execute()
        if not res.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        return JSONResponse({"ok": True, "match_id": mid, "winner_discord_id": who,
                             "placements": patch.get("placements")})
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
                        (m or {}).get("placements"), (m or {}).get("removed_discord_ids"),
                        (m or {}).get("point_awards"))
                except Exception:
                    per[m["id"]] = []
            _pm = [per[m["id"]] for m in ms]
            try:
                boards[str(tid)] = apply_adjustments(
                    aggregate_standings(_pm), _tourney_adjustments(tid))
            except Exception:
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
            "id,tourney_id,version,room_id,status,winner_discord_id,placements,removed_discord_ids,point_awards").eq(
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
                "id,tourney_id,version,room_id,status,winner_discord_id,placements,removed_discord_ids,point_awards").in_("id", mids).execute()
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
                "id,tourney_id,version,room_id,status,winner_discord_id,placements,removed_discord_ids,point_awards").eq(
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


def compute_and_store_tourney_pr_sync(tid: int) -> list[dict]:
    """Compute Tourney PR once at tourney end + store. Idempotent: when
    bngc_comp_pr_history rows exist for the tourney they are returned as-is
    (no recompute). Writes bngc_users.tourney_pr per player. Sync. Never
    raises; returns the PR board (possibly empty)."""
    try:
        if not supabase:
            return []
        try:
            try:
                existing = supabase.table("bngc_comp_pr_history").select(
                    "tourney_id,discord_id,pr,kills,wins,matches,avg_placement,players,manual"
                ).eq("tourney_id", int(tid)).limit(500).execute()
            except Exception:
                existing = supabase.table("bngc_comp_pr_history").select(
                    "tourney_id,discord_id,pr,kills,wins,matches,avg_placement,players"
                ).eq("tourney_id", int(tid)).limit(500).execute()
            if existing.data:
                rows = sorted((existing.data or []),
                              key=lambda r: float((r or {}).get("pr") or 0), reverse=True)
                return [{"discord_id": str(r.get("discord_id")), "pr": float(r.get("pr") or 0),
                         "kills": int(r.get("kills") or 0), "wins": int(r.get("wins") or 0),
                         "matches": int(r.get("matches") or 0),
                         "avg_placement": r.get("avg_placement"),
                         "players": float(r.get("players") or 0)} for r in rows]
        except Exception:
            pass
        trows = supabase.table("bngc_comp_tourneys").select(
            "id,points_table").eq("id", int(tid)).limit(1).execute()
        if not trows.data:
            return []
        trow = trows.data[0]
        mres = supabase.table("bngc_comp_matches").select(
            "id,tourney_id,version,status,winner_discord_id,placements,removed_discord_ids,point_awards").eq(
            "tourney_id", int(tid)).in_("status", ["sealed", "manual"]).order(
            "version").limit(200).execute()
        sealed = [m for m in (mres.data or []) if (m or {}).get("status") in ("sealed", "manual")]
        if not sealed:
            return []
        mids = [m["id"] for m in sealed]
        by_mid: dict = {}
        try:
            if mids:
                eres = supabase.table("bngc_comp_events").select(
                    "match_id,etype,discord_id,actor_name,alive").in_(
                    "match_id", mids[:200]).limit(5000).execute()
                for e in (eres.data or []):
                    by_mid.setdefault((e or {}).get("match_id"), []).append(e)
        except Exception:
            pass
        per_match, board = standings_for_matches(trow, sealed, by_mid)
        if not board:
            return []
        pr_board = compute_tourney_pr(board, per_match)
        for r in pr_board:
            try:
                supabase.table("bngc_comp_pr_history").upsert({
                    "tourney_id": int(tid), "discord_id": str(r["discord_id"]),
                    "pr": float(r["pr"]), "kills": int(r["kills"]), "wins": int(r["wins"]),
                    "matches": int(r["matches"]),
                    "avg_placement": r["avg_placement"], "players": float(r["players"]),
                }, on_conflict="tourney_id,discord_id").execute()
            except Exception:
                pass
            try:
                supabase.table("bngc_users").update(
                    {"tourney_pr": float(r["pr"])}).eq("discord_id", str(r["discord_id"])).execute()
            except Exception:
                pass
        return pr_board
    except Exception:
        return []


@router.post("/api/comp/tourney/pr")
async def comp_tourney_pr(request: Request):
    """Compute + store Tourney PR for an ended tourney (idempotent). Body:
    {tourney_id}. Returns {ok, pr_board}."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        tid = int(b.get("tourney_id"))
    except Exception:
        return JSONResponse({"error": "tourney_id required"}, status_code=400)
    try:
        board = await db_io(compute_and_store_tourney_pr_sync, tid)
        return JSONResponse({"ok": True, "tourney_id": tid, "pr_board": board})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/tourney/pr-override")
async def comp_pr_override(request: Request):
    """Host manual PR: {tourney_id, discord_id, pr|null}.

    Sets a manual PR (writes history flagged manual + bngc_users.tourney_pr;
    recompute preserves manual rows). null clears the manual flag/value.
    pr must be 0..999."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        tid = int(b.get("tourney_id"))
        did = str(b.get("discord_id") or "").strip()
    except Exception:
        return JSONResponse({"error": "tourney_id and discord_id required"}, status_code=400)
    if not did:
        return JSONResponse({"error": "discord_id required"}, status_code=400)
    try:
        pr = b.get("pr")
        pr = float(pr) if pr is not None else None
        if pr is not None and not (0 <= pr <= 999):
            return JSONResponse({"error": "pr must be 0..999 or null"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "pr must be 0..999 or null"}, status_code=400)
    try:
        try:
            hres = supabase.table("bngc_comp_pr_history").select(
                "tourney_id,discord_id,pr").eq("tourney_id", int(tid)).eq(
                "discord_id", did).limit(1).execute()
        except Exception:
            return JSONResponse({"error": "pr history unavailable"}, status_code=500)
        base = dict(((hres.data or [{}])[0] or {}))
        # manual column may predate migration: try flagged write, fall back plain
        row = {"tourney_id": int(tid), "discord_id": did,
               "pr": pr if pr is not None else float(base.get("pr") or 0),
               "kills": int(base.get("kills") or 0), "wins": int(base.get("wins") or 0),
               "matches": int(base.get("matches") or 0),
               "avg_placement": base.get("avg_placement"),
               "players": float(base.get("players") or 0), "manual": pr is not None}
        if pr is None:
            row["manual"] = False
        try:
            supabase.table("bngc_comp_pr_history").upsert(
                row, on_conflict="tourney_id,discord_id").execute()
        except Exception:
            row.pop("manual", None)
            supabase.table("bngc_comp_pr_history").upsert(
                row, on_conflict="tourney_id,discord_id").execute()
        if pr is not None:
            try:
                supabase.table("bngc_users").update(
                    {"tourney_pr": float(pr)}).eq("discord_id", did).execute()
            except Exception:
                pass
        return JSONResponse({"ok": True, "tourney_id": tid, "discord_id": did, "pr": pr})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/tourney/pr-reset")
async def comp_pr_reset(request: Request):
    """Delete a tourney PR history ({tourney_id}) so the next stop/PR
    call recomputes fresh. bngc_users.tourney_pr left as-is."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        tid = int(b.get("tourney_id"))
    except Exception:
        return JSONResponse({"error": "tourney_id required"}, status_code=400)
    try:
        res = supabase.table("bngc_comp_pr_history").delete().eq(
            "tourney_id", int(tid)).execute()
        return JSONResponse({"ok": True, "tourney_id": tid, "deleted": len(res.data or [])})
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
        try:
            q = supabase.table("bngc_comp_registrations").select(
                "id,team_label,member_ids,member_names,captain_id,status,created_at").eq("tourney_id", tid)
            if want:
                q = q.eq("status", want)
            res = q.order("created_at").limit(500).execute()
        except Exception:
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


@router.post("/api/comp/match/elim")
async def comp_match_elim(request: Request):
    """Hand-adjust a player's eliminations in a match (dashboard elim buttons).

    Positive delta inserts manual alv (alive=False) rows — they join the
    reverse death order for placement like bridge-banked rows. Negative
    removes that many of the player's manual alv rows (newest first).
    Tracked (non-manual) rows are never touched. On bridge-owned open
    matches prefer the seal-time snapshot (it still banks the full roster);
    manual elims are meant for manual matches and post-seal corrections.
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
                "match_id": mid, "etype": "alv", "discord_id": did,
                "actor_name": name, "target_name": None,
                "alive": False, "manual": True,
            }] * delta).execute()
        else:
            eres = supabase.table("bngc_comp_events").select("id").eq(
                "match_id", mid).eq("discord_id", did).eq("etype", "alv").eq(
                "manual", True).order("id", desc=True).limit(abs(delta)).execute()
            removed = 0
            for r in (eres.data or []):
                try:
                    supabase.table("bngc_comp_events").delete().eq("id", r["id"]).execute()
                    removed += 1
                except Exception:
                    pass
        cres = supabase.table("bngc_comp_events").select("id,manual,alive").eq(
            "match_id", mid).eq("discord_id", did).eq("etype", "alv").execute()
        # death = explicit alive False (same rule as compute_match_points);
        # the add-player presence row (alive True) is not an elimination.
        elims = sum(1 for r in (cres.data or []) if (r or {}).get("alive") is False)
        tracked = sum(1 for r in (cres.data or [])
                      if (r or {}).get("alive") is False and not (r or {}).get("manual"))
        return JSONResponse({"ok": True, "match_id": mid, "discord_id": did,
                             "elims": elims, "tracked_kept": tracked,
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


def _removed_list(m: dict) -> list:
    try:
        return [str(x) for x in ((m or {}).get("removed_discord_ids") or []) if str(x or "").strip()]
    except Exception:
        return []


@router.post("/api/comp/match/remove")
async def comp_match_remove(request: Request):
    """Remove a player from a match roster (left mid-match).

    Body: {match_id, discord_id?, actor_name?}. discord_id preferred; when
    only actor_name is given the server resolves it via regs/game_username
    (best effort — the 🚪 leave line still shows regardless). Idempotent:
    appends to removed_discord_ids. Removed players are ignored in placement
    and calculations. Never raises beyond JSON errors."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        mid = int(b.get("match_id"))
    except Exception:
        return JSONResponse({"error": "match_id required"}, status_code=400)
    did = str(b.get("discord_id") or "").strip() or None
    aname = str(b.get("actor_name") or "").strip() or None
    try:
        try:
            mres = supabase.table("bngc_comp_matches").select(
                "id,tourney_id,removed_discord_ids").eq("id", mid).limit(1).execute()
        except Exception:
            return JSONResponse({"error": "removed_discord_ids column missing — apply add_comp_v237.sql"}, status_code=500)
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        m = mres.data[0] or {}
        if not did and aname:
            # best-effort resolve: tourney regs + game_username match
            try:
                rres = supabase.table("bngc_comp_registrations").select(
                    "member_ids").eq("tourney_id", m.get("tourney_id")).neq(
                    "status", "banned").limit(500).execute()
                dids = sorted({str(x) for r in (rres.data or [])
                               for x in ((r or {}).get("member_ids") or []) if x})[:200]
                if dids:
                    ures = supabase.table("bngc_users").select(
                        "discord_id,game_username").in_("discord_id", dids).execute()
                    import re as _re
                    want = _re.sub(r"<[^>]*>", "", aname).strip().lower()
                    if want.startswith("dc:"):
                        want = want[3:].strip()
                    for u in (ures.data or []):
                        try:
                            g = _re.sub(r"<[^>]*>", "", str((u or {}).get("game_username") or "")).strip().lower()
                            if g.startswith("dc:"):
                                g = g[3:].strip()
                            if g and g == want:
                                did = str((u or {}).get("discord_id"))
                                break
                        except Exception:
                            continue
            except Exception:
                pass
        if not did:
            return JSONResponse({"error": "discord_id required (actor_name did not resolve)"}, status_code=400)
        cur = _removed_list(m)
        if did not in cur:
            cur.append(did)
            supabase.table("bngc_comp_matches").update(
                {"removed_discord_ids": cur}).eq("id", mid).execute()
        return JSONResponse({"ok": True, "match_id": mid, "removed_discord_ids": cur})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/match/add-back")
async def comp_match_add_back(request: Request):
    """Add a removed player back to a match roster (/tourney add-back).

    Body: {match_id, discord_id}. Removes from removed_discord_ids so the
    player scores again. Never raises beyond JSON errors."""
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
        try:
            mres = supabase.table("bngc_comp_matches").select(
                "id,removed_discord_ids").eq("id", mid).limit(1).execute()
        except Exception:
            return JSONResponse({"error": "removed_discord_ids column missing — apply add_comp_v237.sql"}, status_code=500)
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        cur = [x for x in _removed_list(mres.data[0]) if x != did]
        supabase.table("bngc_comp_matches").update(
            {"removed_discord_ids": cur}).eq("id", mid).execute()
        return JSONResponse({"ok": True, "match_id": mid, "removed_discord_ids": cur})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/match/add-player")
async def comp_match_add_player(request: Request):
    """Add a player (e.g. a sub off the tourney roster) to a match with zero
    stats (dashboard Add player box).

    Body: {match_id, discord_id}. Inserts one manual presence row
    (alv alive=True: not a death, no feed line); scoring boards every mapped
    participant even at 0 kills, so the player appears at 0 pts and the host
    can then +kill/+elim/place them. Also clears the did from
    removed_discord_ids when present. Idempotent: a did with any event row
    already returns ok/present. Never raises beyond JSON errors."""
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
        try:
            mres = supabase.table("bngc_comp_matches").select(
                "id,removed_discord_ids").eq("id", mid).limit(1).execute()
        except Exception:
            mres = supabase.table("bngc_comp_matches").select(
                "id").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        eres = supabase.table("bngc_comp_events").select("id").eq(
            "match_id", mid).eq("discord_id", did).limit(1).execute()
        if (eres.data or []):
            return JSONResponse({"ok": True, "match_id": mid,
                                 "discord_id": did, "present": True})
        name = _comp_display_name(did)
        supabase.table("bngc_comp_events").insert({
            "match_id": mid, "etype": "alv", "discord_id": did,
            "actor_name": name, "target_name": None,
            "alive": True, "manual": True,
        }).execute()
        try:
            cur = [x for x in _removed_list((mres.data[0] or {})) if x != did]
            if len(cur) != len(_removed_list((mres.data[0] or {}))):
                supabase.table("bngc_comp_matches").update(
                    {"removed_discord_ids": cur}).eq("id", mid).execute()
        except Exception:
            pass
        return JSONResponse({"ok": True, "match_id": mid,
                             "discord_id": did, "present": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/match/points")
async def comp_match_points_award(request: Request):
    """Manual +/-X points for a player in a match (dashboard +pts).

    Body: {match_id, discord_id, delta}. Stored in point_awards (additive,
    capped at +/-500 per call); row totals floored at 0 in scoring.
    Returns the match awards map."""
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
    if abs(delta) > 500:
        return JSONResponse({"error": "delta capped at +/-500"}, status_code=400)
    try:
        try:
            mres = supabase.table("bngc_comp_matches").select(
                "id,point_awards").eq("id", mid).limit(1).execute()
        except Exception:
            return JSONResponse({"error": "point_awards column missing - apply add_comp_v239.sql"}, status_code=500)
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        cur = dict(((mres.data[0] or {}).get("point_awards")) or {})
        try:
            cur[did] = int(cur.get(did) or 0) + delta
        except Exception:
            cur[did] = delta
        supabase.table("bngc_comp_matches").update({"point_awards": cur}).eq("id", mid).execute()
        return JSONResponse({"ok": True, "match_id": mid, "point_awards": cur})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/match/reset")
async def comp_match_reset(request: Request):
    """Reset a player (or whole match) to 0 in a match.

    Body: {match_id, discord_id?, hard?}. Clears placements entry + point
    awards; deletes manual kill rows (hard=true also deletes tracked rows,
    giving a true zero). Omit discord_id to reset the whole match.
    Removed/excluded lists are untouched."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        mid = int(b.get("match_id"))
    except Exception:
        return JSONResponse({"error": "match_id required"}, status_code=400)
    did = str(b.get("discord_id") or "").strip() or None
    hard = bool(b.get("hard"))
    try:
        mres = supabase.table("bngc_comp_matches").select(
            "id,placements,point_awards").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        m = mres.data[0] or {}
        cur_p = dict(m.get("placements") or {})
        cur_a = dict(m.get("point_awards") or {})
        if did:
            cur_p.pop(did, None)
            cur_a.pop(did, None)
            q = supabase.table("bngc_comp_events").delete().eq("match_id", mid).eq("discord_id", did)
            if not hard:
                q = q.eq("manual", True)
            try:
                q.execute()
            except Exception:
                # pre-migration manual column: delete scoped rows only when hard
                if hard:
                    supabase.table("bngc_comp_events").delete().eq(
                        "match_id", mid).eq("discord_id", did).execute()
        else:
            cur_p, cur_a = {}, {}
            try:
                if hard:
                    supabase.table("bngc_comp_events").delete().eq("match_id", mid).execute()
                else:
                    supabase.table("bngc_comp_events").delete().eq(
                        "match_id", mid).eq("manual", True).execute()
            except Exception:
                pass
        supabase.table("bngc_comp_matches").update(
            {"placements": cur_p, "point_awards": cur_a}).eq("id", mid).execute()
        return JSONResponse({"ok": True, "match_id": mid, "placements": cur_p, "point_awards": cur_a})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/reset")
async def comp_tourney_reset(request: Request):
    """Reset tourney-ledger points: {tourney_id, discord_id?}.

    Deletes adjustment rows for one player (or all when omitted).
    Sealed-match points are untouched."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        tid = int(b.get("tourney_id"))
    except Exception:
        return JSONResponse({"error": "tourney_id required"}, status_code=400)
    did = str(b.get("discord_id") or "").strip() or None
    try:
        q = supabase.table("bngc_comp_adjustments").delete().eq("tourney_id", int(tid))
        if did:
            q = q.eq("discord_id", did)
        res = q.execute()
        return JSONResponse({"ok": True, "tourney_id": tid, "deleted": len(res.data or [])})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/match/delete")
async def comp_match_delete(request: Request):
    """Delete a match + its events + results snapshot. Body: {match_id}."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        mid = int(b.get("match_id"))
    except Exception:
        return JSONResponse({"error": "match_id required"}, status_code=400)
    try:
        mres = supabase.table("bngc_comp_matches").select(
            "id,tourney_id,version").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        m = mres.data[0] or {}
        try:
            supabase.table("bngc_comp_events").delete().eq("match_id", mid).execute()
        except Exception:
            pass
        try:
            supabase.table("bngc_comp_results").delete().eq("match_id", mid).execute()
        except Exception:
            pass
        supabase.table("bngc_comp_matches").delete().eq("id", mid).execute()
        return JSONResponse({"ok": True, "match_id": mid, "tourney_id": m.get("tourney_id"),
                             "version": m.get("version")})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/comp/adjust")
async def comp_adjust(request: Request):
    """Ledger point adjustment at GLOBAL tourney level (dashboard -pts/+pts).

    Body: {tourney_id, discord_id, delta_points, match_id?, reason?}.
    Inserts one ledger row; standings fold deltas on read so calculate/
    reconcile never snaps back. delta 0 rejected, capped at ±500.
    Returns the player's adjusted tourney total."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        tid = int(b.get("tourney_id"))
        did = str(b.get("discord_id") or "").strip()
        delta = int(b.get("delta_points", b.get("delta", 0)))
    except Exception:
        return JSONResponse({"error": "tourney_id, discord_id and delta_points required"}, status_code=400)
    if not did or delta == 0:
        return JSONResponse({"error": "discord_id and non-zero delta_points required"}, status_code=400)
    if abs(delta) > 500:
        return JSONResponse({"error": "delta capped at ±500"}, status_code=400)
    try:
        mid = b.get("match_id")
        mid = int(mid) if mid is not None else None
    except Exception:
        return JSONResponse({"error": "match_id must be a number"}, status_code=400)
    try:
        reason = str(b.get("reason") or "")[:160]
        by = str(b.get("created_by") or "")[:64]
        tres = supabase.table("bngc_comp_tourneys").select("id").eq("id", tid).limit(1).execute()
        if not tres.data:
            return JSONResponse({"error": "tourney not found"}, status_code=404)
        row = {"tourney_id": tid, "discord_id": did, "delta_points": delta,
               "reason": reason, "created_by": by}
        if mid is not None:
            row["match_id"] = mid
        supabase.table("bngc_comp_adjustments").insert(row).execute()
        total = None
        try:
            board = calculate_tourney_sync(tid, "adjust")
            me = next((r for r in (board or []) if str(r.get("discord_id")) == did), None)
            total = int(me.get("points")) if me and me.get("points") is not None else None
        except Exception:
            pass
        return JSONResponse({"ok": True, "tourney_id": tid, "discord_id": did,
                             "delta_points": delta, "points": total})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/comp/adjustments")
async def comp_adjustments(request: Request):
    """Ledger rows for a tourney (dashboard audit). ?tourney_id="""
    err = _need()
    if err:
        return err
    try:
        tid = int(request.query_params.get("tourney_id"))
    except Exception:
        return JSONResponse({"error": "tourney_id required"}, status_code=400)
    try:
        res = supabase.table("bngc_comp_adjustments").select(
            "id,match_id,discord_id,delta_points,reason,created_by,created_at").eq(
            "tourney_id", tid).order("created_at", desc=True).limit(200).execute()
        return JSONResponse({"tourney_id": tid, "adjustments": res.data or []})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/match/tie")
async def comp_match_tie(request: Request):
    """Share P1 across 2+ players (dashboard Tie button). Stored on the match
    placements map (additive: listed dids all become place 1; other P1 holders
    outside the list are evicted). Scoring gives every P1 holder the full WIN
    bonus. Body: {match_id, discord_ids[]} (2..10). Also points winner at the
    first listed did when the current winner is outside the tied set."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        mid = int(b.get("match_id"))
    except Exception:
        return JSONResponse({"error": "match_id required"}, status_code=400)
    try:
        dids = sorted({str(x).strip() for x in (b.get("discord_ids") or []) if str(x or "").strip()})
    except Exception:
        dids = []
    if len(dids) < 2 or len(dids) > 10:
        return JSONResponse({"error": "discord_ids must list 2..10 players"}, status_code=400)
    try:
        mres = supabase.table("bngc_comp_matches").select(
            "id,placements,winner_discord_id").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        mrow = mres.data[0] or {}
        cur = dict(mrow.get("placements") or {})
        for k in [k for k, v in cur.items() if int(v) == 1 and k not in dids]:
            del cur[k]
        for did in dids:
            cur[did] = 1
        patch: dict = {"placements": cur}
        w = str(mrow.get("winner_discord_id") or "")
        if w not in dids:
            patch["winner_discord_id"] = dids[0]
        supabase.table("bngc_comp_matches").update(patch).eq("id", mid).execute()
        return JSONResponse({"ok": True, "match_id": mid, "placements": cur,
                             "tied": dids, "winner_discord_id": patch.get("winner_discord_id", w)})
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
