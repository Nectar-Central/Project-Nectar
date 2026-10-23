"""
ranked_rooms.py -admin API + gate for the ranked ROOMS ladder
(page at /ranked/rooms).

This is the repair surface comp always had and ranked rooms lost when the
old dashboard was deleted. All endpoints are new /api/ranked/admin/* paths
(the old /api/ranked/rooms|room|link|match/placements paths stay removed —
test_removed_dashboard_endpoints_gone pins that, and none of those had an
admin gate).

Admin model (fail closed): every request must carry the caller's
discord_id, and that id must be a row in the `ranked_admins` table
(allowlist managed directly in Supabase -same trust level as the bot's
operator commands, no self-serve promotion). Unknown id / missing id /
DB down -> 403. This is stronger than the userscript host gates, which
only prevent accidents; points edits move the ladder, so they get a real
allowlist. Admin actions deliberately skip the host-only gate: an admin
must be able to repair a room whose host is gone.

Surfaces:
  GET  /api/ranked/admin/rooms            room browser (status filter)
  GET  /api/ranked/admin/room             one room + its sealed versions
  GET  /api/ranked/admin/match            seal browser: board + killfeed + awards
  GET  /api/ranked/admin/match/unmapped   names with no discord_id (link UI)
  POST /api/ranked/admin/link             attach/clear did on null rows + recalc
  POST /api/ranked/admin/match/place      manual placement (evicts holder) + recalc
  POST /api/ranked/admin/match/remove     remove player from a sealed match + recalc
  POST /api/ranked/admin/match/add-back   undo a remove + recalc
  POST /api/ranked/admin/match/winner     crown override (room shape) + recalc
  POST /api/ranked/admin/match/recalc     differential ledger repair

Edits that change the ledger finish with ranked_api._recalc_sync, which is
differential (new - old per did, re-run diffs to zero), so an admin can
hammer recalc without double-paying anyone.
"""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase, db_io
from ..lib import ranked as ranked_mod
from . import ranked_api as _ranked_api

router = APIRouter()

# status filter values for the browser
_ROOM_STATUSES = ("lobby", "live", "ended")


async def _json(request: Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


class _Req:
    """Minimal request shim for reusing ranked_api helpers internally."""
    def __init__(self, body=None, params=None):
        self._body = dict(body or {})
        self.query_params = dict(params or {})

    async def json(self):
        return dict(self._body)


def _admin_check(discord_id) -> JSONResponse | None:
    """(error | None). Allowlist gate: ranked_admins.discord_id must contain
    the caller. Fail closed on missing id, empty table, or DB error."""
    try:
        did = str(discord_id or "").strip()
    except Exception:
        did = ""
    if not did:
        return JSONResponse({"error": "admin discord_id required"}, status_code=403)
    try:
        if supabase is None:
            return JSONResponse({"error": "admin check failed"}, status_code=403)
        r = supabase.table("ranked_admins").select("discord_id").eq(
            "discord_id", did).limit(1).execute()
        if not (r.data or []):
            return JSONResponse({"error": "not a ranked admin"}, status_code=403)
        return None
    except Exception:
        return JSONResponse({"error": "admin check failed"}, status_code=403)


def _seal_query(rid: str, mver: int, cols: str) -> dict:
    """ranked_results read with the pre-migration points_table fallback
    (same contract as ranked_api._sealed_row, but raising so callers can
    404/500 explicitly). Empty dict when the seal does not exist."""
    try:
        r = supabase.table("ranked_results").select(cols).eq(
            "room_id", rid).eq("match_version", int(mver)).limit(1).execute()
    except Exception as exc:
        if "points_table" not in str(exc) and "42703" not in str(exc):
            raise
        r = supabase.table("ranked_results").select(
            "winner_discord_id,placements,kills,points").eq(
            "room_id", rid).eq("match_version", int(mver)).limit(1).execute()
    return dict((r.data or [{}])[0] or {})


@router.get("/api/ranked/admin/rooms")
async def admin_rooms(request: Request):
    """Room browser. ?discord_id= (admin gate) &status=lobby|live|ended."""
    err = _admin_check(request.query_params.get("discord_id"))
    if err:
        return err
    try:
        status = str(request.query_params.get("status") or "").strip().lower()
    except Exception:
        status = ""
    try:
        q = supabase.table("ranked_rooms").select(
            "id,short_code,status,host_discord_id,match_version,max_matches,"
            "roster,updated_at")
        if status in _ROOM_STATUSES:
            q = q.eq("status", status)
        r = await db_io(lambda: q.order("updated_at", desc=True).limit(100).execute())
        rooms = []
        for row in (r.data or []):
            try:
                roster = row.get("roster") or []
                rooms.append({
                    "id": row.get("id"), "short_code": row.get("short_code"),
                    "status": row.get("status"),
                    "host_discord_id": row.get("host_discord_id"),
                    "match_version": row.get("match_version"),
                    "max_matches": row.get("max_matches"),
                    "players": len(roster),
                    "updated_at": row.get("updated_at"),
                })
            except Exception:
                continue
        return JSONResponse({"rooms": rooms})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/ranked/admin/room")
async def admin_room(request: Request):
    """One room + its sealed versions (newest first) for the seal browser.
    ?discord_id= &room_id="""
    err = _admin_check(request.query_params.get("discord_id"))
    if err:
        return err
    try:
        rid = str(request.query_params.get("room_id") or "").strip()
    except Exception:
        rid = ""
    if not rid:
        return JSONResponse({"error": "room_id required"}, status_code=400)
    try:
        r = await db_io(lambda: supabase.table("ranked_rooms").select(
            "id,short_code,status,host_discord_id,match_version,max_matches,"
            "roster,updated_at").eq("id", rid).limit(1).execute())
        rows = r.data or []
        if not rows:
            return JSONResponse({"error": "room not found"}, status_code=404)
        room = dict(rows[0])
        try:
            m = await db_io(lambda: supabase.table("bngc_ranked_matches").select(
                "room_id,match_version,status,started_at,sealed_at").eq(
                "room_id", rid).order("match_version", desc=True).limit(200).execute())
            matches = list(m.data or [])
        except Exception:
            matches = []
        try:
            names = _ranked_api._display_names(
                sorted({str(x.get("discord_id")) for x in (room.get("roster") or [])
                        if (x or {}).get("discord_id")}))
        except Exception:
            names = {}
        roster = []
        for x in (room.get("roster") or []):
            try:
                did = str(x.get("discord_id") or "")
                if not did:
                    continue
                roster.append({"discord_id": did, "name": names.get(did) or did[:8]})
            except Exception:
                continue
        room["roster_view"] = roster
        room.pop("roster", None)
        return JSONResponse({"room": room, "matches": matches})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/ranked/admin/match")
async def admin_match(request: Request):
    """Seal browser for one (room_id, match_version): ranked_results row,
    frozen economics, computed board, banked events. ?discord_id= params."""
    err = _admin_check(request.query_params.get("discord_id"))
    if err:
        return err
    try:
        rid = str(request.query_params.get("room_id") or "").strip()
        mver = int(request.query_params.get("match_version"))
    except Exception:
        return JSONResponse({"error": "room_id and match_version required"},
                            status_code=400)
    if not rid:
        return JSONResponse({"error": "room_id and match_version required"},
                            status_code=400)
    try:
        # admin view needs the raw frozen table, so read results directly
        # (the userscript detail shape hides points_table on open matches)
        try:
            sealed = _seal_query(rid, mver, "winner_discord_id,placements,kills,"
                                            "points,points_table")
        except Exception:
            return JSONResponse({"error": "db error"}, status_code=500)
        meta = _ranked_api._match_meta(rid, mver)
        events = _ranked_api._banked_events(rid, mver)
        winner = sealed.get("winner_discord_id") or None
        try:
            pmap = {str(k): int(v) for k, v in
                    (sealed.get("placements") or {}).items()}
        except Exception:
            pmap = {}
        try:
            kills = {str(k): max(0, int(v or 0)) for k, v in
                     (sealed.get("kills") or {}).items()}
        except Exception:
            kills = {}
        try:
            points = {str(k): int(v or 0) for k, v in
                      (sealed.get("points") or {}).items()}
        except Exception:
            points = {}
        try:
            table = ranked_mod.table_of_result(sealed) if sealed else None
        except Exception:
            table = None
        # the shared userscript shape (board + killfeed) for consistency
        try:
            detail = await _ranked_api.ranked_match_detail(
                _Req(params={"room_id": rid, "match_version": str(mver)}))
            detail_body = json.loads(detail.body.decode())
        except Exception:
            detail_body = {}
        return JSONResponse({
            "room_id": rid, "match_version": mver,
            "sealed": bool(sealed), "winner_discord_id": winner,
            "placements": pmap, "kills": kills, "points": points,
            "points_table": table,
            "removed_discord_ids": meta.get("removed_discord_ids") or [],
            "point_awards": dict(meta.get("point_awards") or {}),
            "started_at": meta.get("started_at"), "sealed_at": meta.get("sealed_at"),
            "board": detail_body.get("board") or [],
            "killfeed": detail_body.get("killfeed") or [],
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/ranked/admin/match/unmapped")
async def admin_unmapped(request: Request):
    """Names with no discord_id in one seal's events (link UI candidates).
    ?discord_id= &room_id= &match_version="""
    err = _admin_check(request.query_params.get("discord_id"))
    if err:
        return err
    try:
        rid = str(request.query_params.get("room_id") or "").strip()
        mver = int(request.query_params.get("match_version"))
    except Exception:
        return JSONResponse({"error": "room_id and match_version required"},
                            status_code=400)
    try:
        events = _ranked_api._banked_events(rid, mver)
        agg: dict[str, dict] = {}
        for e in events:
            try:
                if (e or {}).get("discord_id"):
                    continue
                et = (e or {}).get("etype")
                actor = str((e or {}).get("actor_name") or "").strip()
                target = str((e or {}).get("target_name") or "").strip()
                if et == "kill":
                    if actor:
                        d = agg.setdefault(actor, {"name": actor, "kills": 0, "deaths": 0})
                        d["kills"] += 1
                    if target:
                        d = agg.setdefault(target, {"name": target, "kills": 0, "deaths": 0})
                        d["deaths"] += 1
                elif et == "alv" and (e or {}).get("alive") is False:
                    if actor:
                        d = agg.setdefault(actor, {"name": actor, "kills": 0, "deaths": 0})
                        d["deaths"] += 1
            except Exception:
                continue
        return JSONResponse({"room_id": rid, "match_version": mver,
                             "unmapped": sorted(agg.values(),
                                                key=lambda r: -r["kills"])})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _seal_id_of(rid: str, mver: int):
    """v250 seal_id stamping: rows carrying a seal_id scope identity edits to
    exactly this seal, so a retried seal's legacy NULL rows never leak."""
    try:
        q = supabase.table("bngc_ranked_events").select(
            "seal_id").eq("room_id", rid).eq("match_version", mver).not_.is_(
            "seal_id", "null").limit(1).execute()
        return ((q.data or [{}])[0] or {}).get("seal_id")
    except Exception:
        return None


@router.post("/api/ranked/admin/link")
async def admin_link(request: Request):
    """Attach (or clear) a discord_id on a seal's null actor_name rows, then
    recalc so the ledger pays the player. Body {room_id, match_version,
    discord_id(admin), actor_name, player_discord_id|null, apply_to_all?}."""
    b = await _json(request)
    err = _admin_check(b.get("discord_id"))
    if err:
        return err
    try:
        rid = str(b.get("room_id") or "").strip()
        mver = int(b.get("match_version"))
        name = str(b.get("actor_name") or "").strip()
    except Exception:
        return JSONResponse({"error": "room_id, match_version, actor_name required"},
                            status_code=400)
    if not rid or not name:
        return JSONResponse({"error": "room_id, match_version, actor_name required"},
                            status_code=400)
    try:
        did = b.get("player_discord_id")
        did = str(did).strip() if did else None
        seal_id = _seal_id_of(rid, mver)
        try:
            upd = supabase.table("bngc_ranked_events").update(
                {"discord_id": did}).eq("room_id", rid).eq(
                "match_version", mver).eq("actor_name", name)
            if did:
                # linking fills only unattributed rows; never overwrite an
                # existing identity (a mapped row means someone decided)
                upd = upd.is_("discord_id", "null")
            else:
                # unlink clears ANY attribution for this actor name in the
                # seal (the comp handler can't do this -its update is
                # null-scoped, so unlink never matched); deliberate here
                pass
            if seal_id:
                upd = upd.eq("seal_id", seal_id)
            res = upd.execute()
            n = len(res.data or [])
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        # apply_to_all: backfill the same actor_name across every other
        # match version of this room (each seal_id-scoped like the main edit)
        n_all = 0
        if b.get("apply_to_all") and did:
            try:
                vs = supabase.table("bngc_ranked_matches").select(
                    "match_version").eq("room_id", rid).limit(200).execute()
                for mv in (vs.data or []):
                    try:
                        over = int(mv.get("match_version") or 0)
                    except Exception:
                        continue
                    if over == mver:
                        continue
                    try:
                        u2 = supabase.table("bngc_ranked_events").update(
                            {"discord_id": did}).eq("room_id", rid).eq(
                            "match_version", over).is_("discord_id", "null").eq(
                            "actor_name", name)
                        s2 = _seal_id_of(rid, over)
                        if s2:
                            u2 = u2.eq("seal_id", s2)
                        n_all += len(u2.execute().data or [])
                    except Exception:
                        continue
            except Exception:
                pass
        # recompute the ledger so the newly linked rows pay out
        out = await db_io(_ranked_api._recalc_sync, rid, mver)
        return JSONResponse({"ok": True, "room_id": rid, "match_version": mver,
                             "actor_name": name, "player_discord_id": did,
                             "linked": n, "linked_all": n_all, "recalc": out})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/admin/match/place")
async def admin_place(request: Request):
    """Manual placement on a sealed match (evicts the holder), then recalc.
    Body {room_id, match_version, discord_id(admin), player_discord_id,
    place|null}."""
    b = await _json(request)
    err = _admin_check(b.get("discord_id"))
    if err:
        return err
    try:
        rid = str(b.get("room_id") or "").strip()
        mver = int(b.get("match_version"))
        pdid = str(b.get("player_discord_id") or "").strip()
    except Exception:
        return JSONResponse({"error": "room_id, match_version, player_discord_id required"},
                            status_code=400)
    if not rid or not pdid:
        return JSONResponse({"error": "room_id, match_version, player_discord_id required"},
                            status_code=400)
    try:
        place = b.get("place")
        place = int(place) if place is not None else None
        if place is not None and (place < 1 or place > 30):
            return JSONResponse({"error": "place must be 1..30 or null"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "place must be 1..30 or null"}, status_code=400)
    try:
        sealed = _seal_query(rid, mver, "placements")
        if not sealed:
            return JSONResponse({"error": "match not found or not sealed"},
                                status_code=404)
        try:
            cur = {str(k): int(v) for k, v in (sealed.get("placements") or {}).items()}
        except Exception:
            cur = {}
        if place is None:
            cur.pop(pdid, None)
        else:
            # one player per place: evict whoever held it
            for k in [k for k, v in cur.items() if int(v) == place and k != pdid]:
                del cur[k]
            cur[pdid] = place
        supabase.table("ranked_results").update({"placements": cur}).eq(
            "room_id", rid).eq("match_version", mver).execute()
        try:
            supabase.table("bngc_ranked_matches").update(
                {"placements": cur}).eq("room_id", rid).eq(
                "match_version", mver).execute()
        except Exception:
            pass
        out = await db_io(_ranked_api._recalc_sync, rid, mver)
        return JSONResponse({"ok": True, "room_id": rid, "match_version": mver,
                             "placements": cur, "recalc": out})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _edit_removed(rid: str, mver: int, pdid: str, remove: bool) -> list | None:
    """Toggle a did on bngc_ranked_matches.removed_discord_ids. Returns the
    new list, or None when the match row is missing."""
    try:
        r = supabase.table("bngc_ranked_matches").select(
            "removed_discord_ids").eq("room_id", rid).eq(
            "match_version", mver).limit(1).execute()
        if not (r.data or []):
            return None
        cur = [str(x) for x in ((r.data[0] or {}).get("removed_discord_ids") or [])]
        if remove and pdid not in cur:
            cur.append(pdid)
        elif not remove:
            cur = [x for x in cur if x != pdid]
        supabase.table("bngc_ranked_matches").update(
            {"removed_discord_ids": cur}).eq("room_id", rid).eq(
            "match_version", mver).execute()
        return cur
    except Exception:
        return None


@router.post("/api/ranked/admin/match/remove")
async def admin_remove(request: Request):
    """Remove a player from a sealed match (cheater/leave repair), then
    recalc. Body {room_id, match_version, discord_id(admin),
    player_discord_id}."""
    b = await _json(request)
    err = _admin_check(b.get("discord_id"))
    if err:
        return err
    try:
        rid = str(b.get("room_id") or "").strip()
        mver = int(b.get("match_version"))
        pdid = str(b.get("player_discord_id") or "").strip()
    except Exception:
        return JSONResponse({"error": "room_id, match_version, player_discord_id required"},
                            status_code=400)
    if not rid or not pdid:
        return JSONResponse({"error": "room_id, match_version, player_discord_id required"},
                            status_code=400)
    try:
        new_list = _edit_removed(rid, mver, pdid, True)
        if new_list is None:
            return JSONResponse({"error": "match row not found"}, status_code=404)
        out = await db_io(_ranked_api._recalc_sync, rid, mver)
        return JSONResponse({"ok": True, "room_id": rid, "match_version": mver,
                             "removed_discord_ids": new_list, "recalc": out})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/admin/match/add-back")
async def admin_add_back(request: Request):
    """Undo a remove, then recalc. Body {room_id, match_version,
    discord_id(admin), player_discord_id}."""
    b = await _json(request)
    err = _admin_check(b.get("discord_id"))
    if err:
        return err
    try:
        rid = str(b.get("room_id") or "").strip()
        mver = int(b.get("match_version"))
        pdid = str(b.get("player_discord_id") or "").strip()
    except Exception:
        return JSONResponse({"error": "room_id, match_version, player_discord_id required"},
                            status_code=400)
    if not rid or not pdid:
        return JSONResponse({"error": "room_id, match_version, player_discord_id required"},
                            status_code=400)
    try:
        new_list = _edit_removed(rid, mver, pdid, False)
        if new_list is None:
            return JSONResponse({"error": "match row not found"}, status_code=404)
        out = await db_io(_ranked_api._recalc_sync, rid, mver)
        return JSONResponse({"ok": True, "room_id": rid, "match_version": mver,
                             "removed_discord_ids": new_list, "recalc": out})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/admin/match/winner")
async def admin_winner(request: Request):
    """Crown override on a sealed room match, then recalc. Admin-gated twin
    of the userscript ranked_match_winner (same crown-swap so the board
    keeps a total order), minus the host gate: admins repair rooms whose
    host is gone. Body {room_id, match_version, discord_id(admin),
    winner_discord_id|null}."""
    b = await _json(request)
    err = _admin_check(b.get("discord_id"))
    if err:
        return err
    try:
        rid = str(b.get("room_id") or "").strip()
        mver = int(b.get("match_version"))
    except Exception:
        return JSONResponse({"error": "room_id and match_version required"},
                            status_code=400)
    if not rid:
        return JSONResponse({"error": "room_id and match_version required"},
                            status_code=400)
    try:
        who = b.get("winner_discord_id")
        who = str(who).strip() if who else None
        sealed = _seal_query(rid, mver, "placements")
        if not sealed:
            return JSONResponse({"error": "match not found or not sealed"},
                                status_code=404)
        patch: dict = {"winner_discord_id": who}
        if who:
            # crown swap (not a tie): winner takes P1 alone; the displaced
            # P1 holder takes the winner's old place so the board keeps a
            # total order (same semantics as the userscript endpoint)
            try:
                cur = {str(k): int(v) for k, v in
                       (sealed.get("placements") or {}).items()}
            except Exception:
                cur = {}
            try:
                old_place = cur.get(who)
                holders = [d for d, p in cur.items() if int(p) == 1 and d != who]
                for h in holders:
                    try:
                        cur[h] = int(old_place) if old_place else \
                            max([int(v) for v in cur.values()] + [1]) + 1
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
        out = await db_io(_ranked_api._recalc_sync, rid, mver)
        if not out:
            return JSONResponse({"error": "recalc failed"}, status_code=500)
        out.update({"ok": True, "room_id": rid, "match_version": mver,
                    "winner_discord_id": who})
        return JSONResponse(out)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/admin/match/recalc")
async def admin_recalc(request: Request):
    """Differential ledger repair for one seal (safe to re-run). Admin-gated
    twin of the userscript recalc, minus the host gate. Body {room_id,
    match_version, discord_id(admin)}."""
    b = await _json(request)
    err = _admin_check(b.get("discord_id"))
    if err:
        return err
    try:
        rid = str(b.get("room_id") or "").strip()
        mver = int(b.get("match_version"))
    except Exception:
        return JSONResponse({"error": "room_id and match_version required"},
                            status_code=400)
    if not rid:
        return JSONResponse({"error": "room_id and match_version required"},
                            status_code=400)
    try:
        out = await db_io(_ranked_api._recalc_sync, rid, mver)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    if not out:
        return JSONResponse({"error": "match not found or not sealed"}, status_code=404)
    out.update({"ok": True, "room_id": rid, "match_version": mver})
    return JSONResponse(out)
