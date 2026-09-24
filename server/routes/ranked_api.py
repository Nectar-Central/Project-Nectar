"""
ranked_api.py — ranked live-client endpoints (userscript + bot).

Kept routes (all have live callers outside any dashboard):
  GET  /api/ranked/table                      userscript room-detect economics
  GET  /api/ranked/match                      userscript live board overlay
  POST /api/ranked/match/winner               userscript host controls
  POST /api/ranked/match/recalc               userscript host controls
  POST /api/ranked/room/backout | /room/end   userscript host controls

Removed (dashboard-only, no callers after the ranked dashboard was
deleted): rooms, room, match/placements, match/removed, match/kills,
match/elim, match/points, match/add-player, match/start, link/*.
Comp-dashboard equivalents live under /api/comp/*, mirrored for the
/ranked page under /api/ranked/* in server/routes/ranked_mirror.py.

NOTE: ranked_match_detail / ranked_match_winner below are plain functions
on purpose — their paths are registered once, as dispatchers, in
ranked_tourney.py (query/body decides ranked-room vs comp-tourney shape).
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase, db_io, is_transient_error
from ..lib.logger import log_err
from ..lib import ranked as ranked_mod

router = APIRouter()


def _banked_events(rid: str, mver: int) -> list:
    """Banked rows for one seal, id-ordered. [] on any failure."""
    try:
        if supabase is None:
            return []
        try:
            r = supabase.table("bngc_ranked_events").select(
                "id,etype,actor_nr,actor_name,target_name,discord_id,alive,manual").eq(
                "room_id", str(rid)).eq("match_version", int(mver)).order(
                "id").limit(1000).execute()
        except Exception:
            # pre-migration (no manual column): classic shape
            r = supabase.table("bngc_ranked_events").select(
                "id,etype,actor_nr,actor_name,target_name,discord_id,alive").eq(
                "room_id", str(rid)).eq("match_version", int(mver)).order(
                "id").limit(1000).execute()
        return list(r.data or [])
    except Exception:
        return []


def _sealed_row(rid: str, mver: int) -> dict | None:
    """ranked_results row for one seal, or None (open / missing)."""
    try:
        if supabase is None:
            return None
        try:
            r = supabase.table("ranked_results").select(
                "winner_discord_id,placements,kills,points,points_table").eq(
                "room_id", str(rid)).eq("match_version", int(mver)).limit(1).execute()
        except Exception as exc:
            # pre-migration (no points_table column): read classic shape
            if "points_table" not in str(exc) and "42703" not in str(exc):
                raise
            r = supabase.table("ranked_results").select(
                "winner_discord_id,placements,kills,points").eq(
                "room_id", str(rid)).eq("match_version", int(mver)).limit(1).execute()
        rows = r.data or []
        return dict(rows[0]) if rows else None
    except Exception:
        return None


def _match_meta(rid: str, mver: int) -> dict:
    """Optional v245 entity metadata; old deployments simply return {}."""
    try:
        if supabase is None:
            return {}
        r = supabase.table("bngc_ranked_matches").select(
            "removed_discord_ids,point_awards,status,started_at,sealed_at,room_code").eq("room_id", str(rid)).eq(
            "match_version", int(mver)).limit(1).execute()
        return dict((r.data or [{}])[0] or {})
    except Exception:
        return {}


def _lobby_avg_for(rid: str, *, fresh: bool = False) -> float:
    """Current lobby average for open-board previews (best effort)."""
    try:
        if supabase is None:
            return 0.0
        r = supabase.table("ranked_rooms").select("roster").eq(
            "id", str(rid)).limit(1).execute()
        rows = r.data or []
        if not rows:
            return 0.0
        dids = [str(x.get("discord_id")) for x in ((rows[0] or {}).get("roster") or [])
                if (x or {}).get("discord_id")]
        return ranked_mod.lobby_avg_points_sync(dids, fresh=fresh)
    except Exception:
        return 0.0


def _norm_player_name(s: str) -> str:
    """Killfeed name normalizer (client mirrors it): strip tags, dc: prefix,
    trim, lowercase. Never raises."""
    try:
        import re as _re
        nm = _re.sub(r"<[^>]*>", "", str(s or "")).strip().lower()
        return nm[3:].strip() if nm.startswith("dc:") else nm
    except Exception:
        return str(s or "").lower()


def _price_kill_sync(code: str, did: str, killer_name: str,
                     victim_name: str) -> dict:
    """Price one optimistic kill at seal-time economics. Sync. Never raises.

    Resolves both names against the live roster (room roster or session
    regs + game names), verifies the killer IS the caller, and returns the
    exact per-kill award the seal would pay for a first kill (no taper
    state mid-match — later kills reconcile at seal). {ok, award, mode}.
    Unresolvable/ambiguous → {ok: False}. No writes.
    """
    try:
        code = str(code or "").strip().upper()
        did = str(did or "").strip()
        kn, vn = _norm_player_name(killer_name), _norm_player_name(victim_name)
        if len(code) != 5 or not code.isalnum() or not did or not kn:
            return {"ok": False}
        try:
            if ranked_mod.ranked_sleeping():
                return {"ok": False}
        except Exception:
            pass
        if supabase is None:
            return {"ok": False}
        # Ranked room first (kill 1..8 upset economy).
        try:
            rdata = ranked_mod.get_ranked_room(code)
        except Exception:
            rdata = None
        if rdata:
            try:
                roster = ranked_mod.room_roster_ids(rdata)
                if did not in roster:
                    return {"ok": False}
                namemap = ranked_mod._game_names_by_discord_sync(roster) or {}
                lookup = {_norm_player_name(k): v for k, v in namemap.items()}
                if lookup.get(kn) != did:
                    return {"ok": False}
                vdid = lookup.get(vn)
                want = [did] + ([vdid] if vdid and vdid != did else [])
                pts = ranked_mod._get_points_many(want, fresh=True)
                kpts = int(pts.get(did, 0))
                vpts = int(pts.get(vdid, kpts)) if vdid else kpts
                try:
                    avg = float(ranked_mod.lobby_avg_points_sync(roster, fresh=True))
                except Exception:
                    avg = 0.0
                return {"ok": True, "mode": "room",
                        "award": max(0, int(ranked_mod.kill_delta(kpts, vpts, avg))),
                        "killer": did, "victim": vdid}
            except Exception:
                return {"ok": False}
        # Ranked session (kill 4..20 tourney economy).
        try:
            from . import ranked_tourney as _rt
        except Exception:
            return {"ok": False}
        try:
            tres = supabase.table("bngc_ranked_tourneys").select(
                "id").eq("code", code).neq("status", "ended").limit(2).execute()
            rows = tres.data or []
            if len(rows) != 1:
                return {"ok": False}
            tid = (rows[0] or {}).get("id")
            rres = supabase.table("bngc_ranked_tourney_registrations").select(
                "member_ids,member_names").eq("tourney_id", int(tid)).neq(
                "status", "banned").limit(500).execute()
            parts: set[str] = set()
            disp: dict[str, str] = {}
            for r in (rres.data or []):
                try:
                    for m in ((r or {}).get("member_ids") or []):
                        if str(m or "").strip():
                            parts.add(str(m))
                    for _d, _nm in (((r or {}).get("member_names")) or {}).items():
                        label = str((_nm or {}).get("display_name")
                                    or (_nm or {}).get("username") or "").strip()
                        if _d and label:
                            disp.setdefault(str(_d), label)
                except Exception:
                    continue
            if did not in parts:
                return {"ok": False}
            try:
                ures = supabase.table("bngc_users").select(
                    "discord_id,game_username").in_(
                    "discord_id", sorted(parts)[:200]).execute()
                for u in (ures.data or []):
                    try:
                        g = str((u or {}).get("game_username") or "").strip()
                        _d = str((u or {}).get("discord_id") or "")
                        if g and _d:
                            disp.setdefault(_d, g)
                    except Exception:
                        continue
            except Exception:
                pass
            lookup = {_norm_player_name(v): k for k, v in disp.items()}
            if lookup.get(kn) != did:
                return {"ok": False}
            vdid = lookup.get(vn)
            snap = _rt._tourney_ladder_snapshot(parts | {did} | ({vdid} if vdid else set()))
            kpts = int(snap.get(did, 0))
            vpts = int(snap.get(vdid, kpts)) if vdid else kpts
            n = max(1, len(parts))
            try:
                avg = round(sum(int(snap.get(d, 0)) for d in parts) / len(parts), 1)
            except Exception:
                avg = 0.0
            return {"ok": True, "mode": "session",
                    "award": max(0, int(_rt.tourney_kill_delta(kpts, vpts, avg, n))),
                    "killer": did, "victim": vdid}
        except Exception:
            return {"ok": False}
    except Exception:
        return {"ok": False}


@router.post("/api/ranked/kill-price")
async def ranked_kill_price(request: Request):
    """Price one optimistic kill for the live counter. Body
    {code, discord_id, killer_name, victim_name} → {ok, award, mode}.

    Display-only helper: the seal stays the single source of truth (the
    client shows server_total + priced kills and discards the layer at
    seal). Best-effort — unresolvable names resolve to ok:false, never 500.
    """
    try:
        b = await request.json()
        b = b if isinstance(b, dict) else {}
    except Exception:
        b = {}
    try:
        out = await db_io(_price_kill_sync,
                          b.get("code"), b.get("discord_id"),
                          b.get("killer_name"), b.get("victim_name"))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:80]}, status_code=500)
    return JSONResponse(out if isinstance(out, dict) else {"ok": False})


@router.get("/api/ranked/room/by-code")
async def ranked_room_by_code(request: Request):
    """Room carrying this room code (userscript join detection).

    Returns the single non-ended room for the code, else {room: null}.
    Ambiguous (2+ open rows, same code) resolves to null — never guess.
    Same contract as GET /api/comp/tourney/by-code.
    """
    if supabase is None:
        return JSONResponse({"room": None})
    try:
        code = str(request.query_params.get("code") or "").strip().upper()
    except Exception:
        code = ""
    if len(code) != 5 or not code.isalnum():
        return JSONResponse({"room": None})
    try:
        res = await db_io(lambda: supabase.table("ranked_rooms").select(
            "id,short_code,status").eq("short_code", code).neq(
            "status", "ended").limit(2).execute())
        rows = res.data or []
        if len(rows) != 1:
            return JSONResponse({"room": None})
        r = rows[0]
        return JSONResponse({"room": {
            "id": r.get("id"), "short_code": r.get("short_code"),
            "status": r.get("status")}})
    except Exception as e:
        # Join-detection polls this every few seconds — a pool blip must
        # resolve to null (retry on next poll), never a 500 that the
        # userscript treats as fatal.
        if is_transient_error(e):
            try:
                log_err(f"RANKED by-code={code} transient ({str(e)[:60]}) → null")
            except Exception:
                pass
            return JSONResponse({"room": None})
        try:
            msg = str(e)[:160]
        except Exception:
            msg = "db error"
        return JSONResponse({"error": msg}, status_code=500)


@router.get("/api/ranked/table")
async def ranked_table(request: Request):
    """Static table for a room's current lobby (optimistic client values).

    ?room_id= (R-XXXXXX) -> {room_id, short_code, avg_points, table, loss}.
    The userscript fetches this on room detect and uses kill/win for
    toasts + optimistic deltas instead of hardcoded 8/50 (loss stays -5).
    404 when the room is missing. Never raises beyond JSON errors.
    """
    if supabase is None:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    try:
        rid = str(request.query_params.get("room_id") or "").strip()
    except Exception:
        rid = ""
    if not rid:
        return JSONResponse({"error": "room_id required"}, status_code=400)
    try:
        r = supabase.table("ranked_rooms").select(
            "id,short_code,roster").eq("id", rid).limit(1).execute()
        rows = r.data or []
        if not rows:
            return JSONResponse({"error": "room not found"}, status_code=404)
        room = dict(rows[0])
        try:
            dids = [str(x.get("discord_id")) for x in (room.get("roster") or [])
                    if (x or {}).get("discord_id")]
        except Exception:
            dids = []
        avg = ranked_mod.lobby_avg_points_sync(dids, fresh=ranked_mod.RANKED_DYNAMIC)
        return JSONResponse({
            "room_id": str(room.get("id") or rid),
            "short_code": room.get("short_code"),
            "avg_points": avg,
            "table": (ranked_mod.dynamic_table_for(avg, len(dids))
                      if ranked_mod.RANKED_DYNAMIC else ranked_mod.scaled_table_for(avg)),
            "loss": ranked_mod.LOSS_POINTS,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _display_names(dids: list[str]) -> dict:
    """discord_id -> game username (best effort)."""
    try:
        if supabase is None or not dids:
            return {}
        r = supabase.table("bngc_users").select(
            "discord_id,game_username,discord_username").in_(
            "discord_id", dids[:200]).execute()
        out = {}
        for u in (r.data or []):
            try:
                label = str(u.get("game_username") or u.get("discord_username") or u.get("discord_id"))
                if u.get("discord_id") and label:
                    out[str(u.get("discord_id"))] = label
            except Exception:
                continue
        return out
    except Exception:
        return {}


async def ranked_match_detail(request: Request):
    """Board + killfeed for one ranked seal (open or sealed). Never raises.

    Plain function (no route): GET /api/ranked/match is registered once in
    ranked_mirror.py, which dispatches here for ranked-room queries
    (?room_id=&match_version=) and to the comp handler for ?id=.
    """
    if supabase is None:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    try:
        rid = str(request.query_params.get("room_id") or "").strip()
        mver = int(request.query_params.get("match_version"))
    except Exception:
        return JSONResponse({"error": "room_id and match_version required"}, status_code=400)
    if not rid:
        return JSONResponse({"error": "room_id and match_version required"}, status_code=400)
    try:
        sealed = _sealed_row(rid, mver)
        # Meta always (not just when sealed): live versions need started_at
        # for the dashboard bookends + Start button state.
        try:
            meta = _match_meta(rid, mver)
        except Exception:
            meta = {}
        events = _banked_events(rid, mver)
        winner = (sealed or {}).get("winner_discord_id") if sealed else None
        pmap = dict((sealed or {}).get("placements") or {}) if sealed else None
        try:
            if sealed:
                table = ranked_mod.table_of_result(sealed)
            elif ranked_mod.RANKED_DYNAMIC:
                try:
                    rr = supabase.table("ranked_rooms").select("roster").eq(
                        "id", rid).limit(1).execute()
                    roster = [str(x.get("discord_id")) for x in
                              ((rr.data or [{}])[0].get("roster") or []) if x.get("discord_id")]
                except Exception:
                    roster = []
                table = ranked_mod.dynamic_table_for(_lobby_avg_for(rid, fresh=True), len(roster))
            else:
                # open board previews the coming seal economics (lobby avg)
                table = ranked_mod.scaled_table_for(_lobby_avg_for(rid))
        except Exception:
            table = {"kill": ranked_mod.KILL_POINTS, "win": ranked_mod.WIN_POINTS,
                     "placement": []}
        try:
            if table.get("mode") == "dynamic-v1":
                awards = table.get("kill_awards") or []
                if not sealed:
                    pairs = ranked_mod._event_kill_pairs(
                        events, ranked_mod._game_names_by_discord_sync(roster))
                    awards = ranked_mod._kill_awards_for(
                        pairs, ranked_mod._get_points_many(roster, fresh=True),
                        table.get("avg_points"))
                rows = ranked_mod._ranked_rows(events, table, winner, pmap, awards,
                                                meta.get("removed_discord_ids"),
                                                meta.get("point_awards"))
            else:
                from server.lib.scoring import compute_match_points as _cmp
                rows = _cmp(events, table, winner, pmap,
                            meta.get("removed_discord_ids"),
                            meta.get("point_awards"))
        except Exception:
            rows = []
        board = []
        try:
            _frozen_loss = {}
            try:
                if sealed:
                    _frozen_loss = {str(k): int(v) for k, v in
                                    ((table or {}).get("loss_awards") or {}).items()}
            except Exception:
                _frozen_loss = {}
            for r in rows:
                try:
                    did = str(r.get("discord_id"))
                    pts = int(r.get("points") or 0)
                    if sealed and not r.get("won"):
                        pts += int(_frozen_loss.get(did, ranked_mod.LOSS_POINTS))
                    board.append({"discord_id": did, "name": r.get("name") or did[:6],
                                  "kills": int(r.get("kills") or 0),
                                  "placement": r.get("placement"),
                                  "won": bool(r.get("won")), "points": pts})
                except Exception:
                    continue
        except Exception:
            board = []
        try:
            nmap = _display_names(sorted({b["discord_id"] for b in board}))
            for b in board:
                if nmap.get(b["discord_id"]):
                    b["name"] = nmap[b["discord_id"]]
        except Exception:
            pass
        feed = [{"etype": e.get("etype"), "actor_nr": e.get("actor_nr"),
                 "actor_name": e.get("actor_name"), "target_name": e.get("target_name"),
                 "discord_id": e.get("discord_id"), "alive": e.get("alive"),
                 "manual": bool((e or {}).get("manual"))}
                for e in events]
        return JSONResponse({
            "room_id": rid, "match_version": mver, "sealed": sealed is not None,
            "winner_discord_id": winner,
            "placements": dict((sealed or {}).get("placements") or {}),
            "removed_discord_ids": meta.get("removed_discord_ids") or [],
            "point_awards": dict(meta.get("point_awards") or {}),
            "started_at": meta.get("started_at"),
            "sealed_at": meta.get("sealed_at"),
            "points_table": table if sealed else None,
            "board": board, "killfeed": feed,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def _json(request: Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


def _host_room(rid: str) -> dict | None:
    """Live lobby/live ranked room row, or None."""
    try:
        if supabase is None or not rid:
            return None
        r = supabase.table("ranked_rooms").select(
            "id,short_code,status,host_discord_id").eq("id", str(rid)).limit(1).execute()
        rows = r.data or []
        return dict(rows[0]) if rows else None
    except Exception:
        return None


def _require_host(room: dict | None, discord_id: str):
    """(error_response | None). Host-only gate: accidents, not auth —
    same trust level as the comp dashboard endpoints."""
    if room is None:
        return JSONResponse({"error": "room not found"}, status_code=404)
    try:
        did = str(discord_id or "").strip()
    except Exception:
        did = ""
    if not did or did != str(room.get("host_discord_id") or ""):
        return JSONResponse({"error": "host only"}, status_code=403)
    return None


@router.post("/api/ranked/room/backout")
async def ranked_room_backout(request: Request):
    """Host: live -> lobby (sealed matches stand; sealing pauses until
    the bot restarts the room). Body {room_id, discord_id}."""
    if supabase is None:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    b = await _json(request)
    try:
        rid = str(b.get("room_id") or "").strip()
    except Exception:
        rid = ""
    if not rid:
        return JSONResponse({"error": "room_id required"}, status_code=400)
    room = _host_room(rid)
    err = _require_host(room, b.get("discord_id"))
    if err:
        return err
    if str(room.get("status") or "") != "live":
        return JSONResponse({"error": "room is not live"}, status_code=400)
    try:
        supabase.table("ranked_rooms").update({"status": "lobby"}).eq("id", rid).execute()
        return JSONResponse({"ok": True, "room_id": rid, "status": "lobby"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/room/end")
async def ranked_room_end(request: Request):
    """Host: lobby|live -> ended (bot loop finalizes threads/embeds).
    Body {room_id, discord_id}."""
    if supabase is None:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    b = await _json(request)
    try:
        rid = str(b.get("room_id") or "").strip()
    except Exception:
        rid = ""
    if not rid:
        return JSONResponse({"error": "room_id required"}, status_code=400)
    room = _host_room(rid)
    err = _require_host(room, b.get("discord_id"))
    if err:
        return err
    if str(room.get("status") or "") not in ("lobby", "live"):
        return JSONResponse({"error": "room already ended"}, status_code=400)
    try:
        supabase.table("ranked_rooms").update({"status": "ended"}).eq("id", rid).execute()
        return JSONResponse({"ok": True, "room_id": rid, "status": "ended"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _recalc_sync(rid: str, mver: int) -> dict | None:
    """Recompute one sealed match from banked rows + stored winner/placements.

    Returns {points, kills, diffs, totals} or None (no results row / DB down).
    Ledger fix is differential (new - old per did), so a recalc never
    double-pays: re-running it diffs to zero. Sync, for db_io and direct
    tests. Never raises."""
    try:
        if supabase is None or not rid or int(mver or 0) <= 0:
            return None
        r = supabase.table("ranked_results").select(
            "winner_discord_id,placements,kills,points,points_table").eq(
            "room_id", str(rid)).eq("match_version", int(mver)).limit(1).execute()
        rows = r.data or []
        if not rows:
            return None
        old = dict(rows[0])
        meta = _match_meta(rid, mver)
        removed = meta.get("removed_discord_ids") or []
        # Host manual points (bot seal awards): folded into every row before
        # the loss is applied, so the differential ledger below pays them
        # exactly once (re-running diffs to zero).
        try:
            awards = {str(k): int(v) for k, v in
                      ((meta.get("point_awards") or {}).items())
                      if str(k or "").strip() and int(v or 0)}
        except Exception:
            awards = {}
        winner = (old.get("winner_discord_id") or None)
        winner = str(winner) if winner else None
        try:
            pmap = {str(k): int(v) for k, v in ((old.get("placements") or {}).items())}
        except Exception:
            pmap = {}
        try:
            old_kills = {str(k): max(0, int(v or 0)) for k, v in ((old.get("kills") or {}).items())}
        except Exception:
            old_kills = {}
        try:
            old_points = {str(k): int(v or 0) for k, v in ((old.get("points") or {}).items())}
        except Exception:
            old_points = {}
        # Frozen losses default to flat -5; the seal-time map fills them
        # when the stored table carries loss_awards (dynamic seals).
        _frozen_loss: dict = {}
        _loss_get = lambda did: int(_frozen_loss.get(str(did),
                                                     ranked_mod.LOSS_POINTS))
        try:
            from server.lib.scoring import compute_match_points as _cmp
            events = _banked_events(str(rid), int(mver))
            if not events:
                events = ([{"etype": "kill", "discord_id": did, "actor_name": None, "alive": None}
                           for did, n in old_kills.items() for _ in range(max(0, int(n or 0)))])
            # Frozen seal-time economics: dynamic rows carry their exact
            # per-kill awards, so a later Elo change cannot rewrite history.
            # Same for losses: loss_awards freezes each did's tier-scaled
            # seal-time loss; rows sealed before it fall back to flat -5.
            table = ranked_mod.table_of_result(old)
            try:
                _frozen_loss = {str(k): int(v) for k, v in
                                ((table or {}).get("loss_awards") or {}).items()}
            except Exception:
                _frozen_loss = {}
            if table.get("mode") == "dynamic-v1":
                comp_rows = ranked_mod._ranked_rows(
                    events, table, winner, pmap or None,
                    table.get("kill_awards") or [], removed, awards)
            else:
                comp_rows = _cmp(events, table, winner, pmap or None,
                                 removed, awards)
            comp_by = {str(x.get("discord_id")): int(x.get("points") or 0) for x in comp_rows}
            comp_kills = {str(x.get("discord_id")): int(x.get("kills") or 0) for x in comp_rows}
        except Exception:
            comp_by, comp_kills = {}, {}
        involved = set(old_kills) | set(pmap) | set(old_points) | set(comp_by)
        if winner:
            involved.add(winner)
        new_points: dict = {}
        for did in involved:
            try:
                if did in comp_by:
                    pts = comp_by[did] + (0 if did == winner else _loss_get(did))
                else:
                    pts = old_kills.get(did, 0) * ranked_mod.KILL_POINTS + (
                        ranked_mod.WIN_POINTS if did == winner else _loss_get(did))
                new_points[did] = pts
            except Exception:
                continue
        new_kills = dict(old_kills)
        try:
            for did, n in comp_kills.items():
                new_kills[did] = n
        except Exception:
            pass
        diffs = {did: new_points.get(did, 0) - old_points.get(did, 0) for did in new_points}
        diffs = {did: d for did, d in diffs.items() if d}
        totals = ranked_mod._bump_points_many(diffs) if diffs else {}
        try:
            supabase.table("ranked_results").update(
                {"kills": new_kills, "points": new_points}).eq(
                "room_id", str(rid)).eq("match_version", int(mver)).execute()
            # NOTE: point_awards on bngc_ranked_matches is the bot's MANUAL
            # map, never the final totals — recalc must not mirror
            # new_points back into it or the next recalc double-pays.
        except Exception as exc:
            ranked_mod.log_ranked(f"[ranked] recalc write failed {rid} v{mver}: {exc}")
            return None
        return {"points": new_points, "kills": new_kills, "diffs": diffs, "totals": totals}
    except Exception:
        return None


@router.post("/api/ranked/match/recalc")
async def ranked_match_recalc(request: Request):
    """Host: recompute one sealed match (differential ledger fix).
    Body {room_id, match_version, discord_id}."""
    if supabase is None:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    b = await _json(request)
    try:
        rid = str(b.get("room_id") or "").strip()
        mver = int(b.get("match_version"))
    except Exception:
        return JSONResponse({"error": "room_id and match_version required"}, status_code=400)
    if not rid:
        return JSONResponse({"error": "room_id and match_version required"}, status_code=400)
    room = _host_room(rid)
    err = _require_host(room, b.get("discord_id"))
    if err:
        return err
    try:
        out = await db_io(_recalc_sync, rid, mver)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    if not out:
        return JSONResponse({"error": "match not found or not sealed"}, status_code=404)
    out.update({"ok": True, "room_id": rid, "match_version": mver})
    return JSONResponse(out)


async def ranked_match_winner(request: Request):
    """Host: set/clear the winner override on a sealed match, then recalc.
    Body {room_id, match_version, discord_id, winner_discord_id|null}.

    Plain function (no route): POST /api/ranked/match/winner is registered
    once in ranked_mirror.py, which dispatches here for ranked-room bodies
    ({room_id, match_version}) and to the comp handler for {match_id}.
    """
    if supabase is None:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    b = await _json(request)
    try:
        rid = str(b.get("room_id") or "").strip()
        mver = int(b.get("match_version"))
    except Exception:
        return JSONResponse({"error": "room_id and match_version required"}, status_code=400)
    if not rid:
        return JSONResponse({"error": "room_id and match_version required"}, status_code=400)
    room = _host_room(rid)
    err = _require_host(room, b.get("discord_id"))
    if err:
        return err
    try:
        who = b.get("winner_discord_id")
        who = str(who).strip() if who else None
        r = supabase.table("ranked_results").select("placements").eq(
            "room_id", rid).eq("match_version", mver).limit(1).execute()
        if not (r.data or []):
            return JSONResponse({"error": "match not found or not sealed"}, status_code=404)
        patch: dict = {"winner_discord_id": who}
        if who:
            # crown swap (not a tie): winner takes P1 alone; the displaced
            # P1 holder takes the winner's old place so the board keeps a
            # total order. Without this the stale P1 triggers the tied-P1
            # shared-win rule and steals the bonus.
            try:
                cur = {str(k): int(v) for k, v in (dict(((r.data[0] or {}).get("placements")) or {}).items())}
            except Exception:
                cur = {}
            try:
                old_place = cur.get(who)
                holders = [d for d, p in cur.items() if int(p) == 1 and d != who]
                for h in holders:
                    try:
                        cur[h] = int(old_place) if old_place else max([int(v) for v in cur.values()] + [1]) + 1
                    except Exception:
                        pass
                cur.pop(who, None)
                cur[who] = 1
                patch["placements"] = cur
            except Exception:
                pass
        supabase.table("ranked_results").update(patch).eq(
            "room_id", rid).eq("match_version", mver).execute()
        try:
            supabase.table("bngc_ranked_matches").update(patch).eq(
                "room_id", rid).eq("match_version", mver).execute()
        except Exception:
            pass
        out = await db_io(_recalc_sync, rid, mver)
        if not out:
            return JSONResponse({"error": "recalc failed"}, status_code=500)
        out.update({"ok": True, "room_id": rid, "match_version": mver,
                    "winner_discord_id": who})
        return JSONResponse(out)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
