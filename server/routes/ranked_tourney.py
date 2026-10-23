"""
ranked_tourney.py -ranked tourney API (comp clone, ladder-scored) (pure compute + thin endpoints).

  GET /api/ranked/tourneys?status=scheduled|running|ended
      -> [{id, code, name, status, series_len, sealed, players, leader, ...}]
  GET /api/ranked/tourney?id=
      -> {tourney, registrations, matches, standings}

Statuses are DERIVED (never stored beyond scheduled/open/closed):
  closed -> ended; scheduled -> scheduled; open + >=1 sealed match -> running;
  open + 0 matches -> scheduled.

Standings math (shared by bot, dashboard, Events tab -one implementation):
  per sealed match: kills per discord (kill events, mapped only) +
  win bonus (manual winner_discord_id, else last alive from alv order) +
  placement table by reverse alv-death order (last alive = 1st).
  Aggregate = sum across sealed matches. Points table from the tourney row,
  default kill 1 / win 3 / placement [10,7,5,3,1].
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from typing import Any

from ..lib.supabase_client import supabase, db_io, is_transient_error
from ..lib.logger import log_err, log_comp
from ..lib.ranked import (_get_points_many as _ladder_points,
                           loss_for as _ladder_loss,
                           loss_vs as _ladder_loss_vs,
                           milestone_for as _ladder_milestone,
                           clamp_match_delta as _clamp_match_delta,
                           ranked_sleeping as _ranked_sleeping,
                           _bump_points_many as _ladder_bump)

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
        res = supabase.table("bngc_ranked_tourney_adjustments").select(
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


# ---------------------------------------------------------------------------
# Dynamic tourney scoring (ladder inputs, frozen per match)
#
# Ranked tourneys score like ranked rooms, not like comp: every award is a
# function of ranked-ladder points, computed at seal time and frozen on the
# match row. Boards and recalculations replay the frozen table verbatim.
# Matches sealed before the freeze column existed replay the legacy static
# tourney table with NO loss (exactly what they paid).
# ---------------------------------------------------------------------------

_TOURNEY_PURSE = [10, 7, 5, 3, 1]


def tourney_kill_delta(killer_pts: Any, victim_pts: Any,
                       avg_pts: Any, n: Any) -> int:
    """One kill award from ladder inputs. Pure, unit-tested.

    Upset curve (rooms' logistic: beating stronger pays toward 12, farming
    weaker trends to the floor) plus session size (bigger lobbies pay a
    little more). Bounded 4..20 so every kill pays but none explodes.
    """
    try:
        import math
        killer = float(killer_pts or 0)
        victim = float(victim_pts or 0)
        avg = max(0.0, float(avg_pts or 0))
        n = max(1, int(n or 1))
        upset = 1.0 / (1.0 + math.pow(10.0, (killer - victim) / 600.0))
        base = 8 * (0.5 + upset + 0.15 * avg / 4000.0 + 0.02 * n)
        return max(4, min(20, int(round(base))))
    except Exception:
        return 8


def tourney_win_delta(winner_pts: Any, n: Any) -> int:
    """Victory award from the winner's ladder points + lobby size. Pure.

    Bounded 12..30, deliberately under the +30 per-match clamp: weak
    winners in small lobbies feel the scale (12+), strong lobbies still
    saturate the cap. A legacy-sized purse would cap every single win and
    make scaling moot -that saturation was the "every win pays +30" bug.
    """
    try:
        w = max(0.0, float(winner_pts or 0))
        n = max(1, int(n or 1))
        return max(12, min(30, int(round(18 * (1.0 + w / 4000.0 + 0.03 * n)))))
    except Exception:
        return 18


def tourney_place_delta(place: Any, player_pts: Any, avg_pts: Any) -> int:
    """Placement purse from the placed player's points + session avg. Pure."""
    try:
        p = int(place)
        if p < 1 or p > 5:
            return 0
        pl = max(0.0, float(player_pts or 0))
        avg = max(0.0, float(avg_pts or 0))
        return max(6, min(24, int(round(
            _TOURNEY_PURSE[p - 1] * (1.0 + avg / 5000.0 + pl / 20000.0)))))
    except Exception:
        return 0


def tourney_table_for(avg: Any, n: Any, kill_awards: list | None = None,
                      win: Any = None, placement: list | None = None,
                      loss_awards: dict | None = None) -> dict:
    """Frozen dynamic table for one tourney match. Pure."""
    try:
        avg_f = round(max(0.0, float(avg or 0)), 1)
    except Exception:
        avg_f = 0.0
    try:
        n_i = max(0, int(n or 0))
    except Exception:
        n_i = 0
    try:
        frozen_loss = {str(k): int(v) for k, v in (loss_awards or {}).items()
                       if str(k or "").strip()}
    except Exception:
        frozen_loss = {}
    return {
        "mode": "tourney-dynamic-v1",
        "avg_points": avg_f,
        "players": n_i,
        "kill": None,
        "win": win,
        "placement": list(placement or []),
        "kill_awards": list(kill_awards or []),
        "loss_awards": frozen_loss,
    }


def _tourney_ladder_snapshot(dids) -> dict:
    """ranked_points snapshot for dids (fresh: seal-time truth). Never raises.

    Unranked roster members come back at 0, so a fresh tourney still shows a
    real average (avg 0.0 pts / Unranked) instead of a blank rank line.
    """
    try:
        want = sorted({str(d) for d in (dids or []) if str(d or "").strip()})[:200]
        if not want or not supabase:
            return {}
        pts = _ladder_points(want, fresh=True) or {}
        return {d: int(pts.get(d) or 0) for d in want}
    except Exception:
        return {}


def _norm_nm(s: str) -> str:
    try:
        import re as _re
        nm = _re.sub(r"<[^>]*>", "", str(s or "")).strip().lower()
        return nm[3:].strip() if nm.startswith("dc:") else nm
    except Exception:
        return str(s or "").lower()


def freeze_match_table(mid: int) -> dict | None:
    """Compute a match's dynamic table from the current ladder and store it.

    Reads the match row + its events, snapshots ranked_points for every
    participant, and writes {points_table, avg_points} back to the match.
    Best-effort: returns the table, or None on any failure (callers fall
    back to legacy replay and never fail because of this). Never raises.
    """
    try:
        if not supabase or mid is None:
            return None
        try:
            mres = supabase.table("bngc_ranked_tourney_matches").select(
                "id,tourney_id,winner_discord_id,placements,removed_discord_ids,point_awards,points_table"
            ).eq("id", int(mid)).limit(1).execute()
        except Exception as exc:
            # pre-v249 (no points_table column): classic shape, no seal modes
            if "points_table" not in str(exc) and "42703" not in str(exc):
                return None
            mres = supabase.table("bngc_ranked_tourney_matches").select(
                "id,tourney_id,winner_discord_id,placements,removed_discord_ids,point_awards"
            ).eq("id", int(mid)).limit(1).execute()
        if not mres.data:
            return None
        m = mres.data[0] or {}
        try:
            _existing = (m or {}).get("points_table")
            if isinstance(_existing, dict) and str(_existing.get("mode") or "") in (
                    "room-seal-v1", "room-seal-pending-v1"):
                # bridge-owned: the room seal's deltas (or its pending join
                # keys) -tourney-dynamic math must never overwrite them.
                return _existing if _existing.get("mode") == "room-seal-v1" else None
        except Exception:
            pass
        try:
            xset = {str(x) for x in ((m or {}).get("removed_discord_ids") or [])
                    if str(x or "").strip()}
        except Exception:
            xset = set()
        try:
            eres = supabase.table("bngc_ranked_tourney_events").select(
                "id,etype,discord_id,actor_name,target_name,alive"
            ).eq("match_id", int(mid)).order("id").limit(2000).execute()
            events = eres.data or []
        except Exception:
            return None
        # participant universe: every mapped did in any event row, plus the
        # winner and manual-placement holders (a sub added with zero events
        # still counts for the session size).
        parts: set[str] = set()
        for e in events:
            try:
                did = str((e or {}).get("discord_id") or "").strip()
                if did and did not in xset:
                    parts.add(did)
            except Exception:
                continue
        try:
            w0 = str((m or {}).get("winner_discord_id") or "").strip()
            if w0 and w0 not in xset:
                parts.add(w0)
        except Exception:
            pass
        try:
            for k in (((m or {}).get("placements")) or {}):
                if str(k or "").strip() and str(k) not in xset:
                    parts.add(str(k))
        except Exception:
            pass
        if not parts:
            return None
        ladder = _tourney_ladder_snapshot(parts)
        n = len(parts)
        try:
            avg = round(sum(ladder.get(d, 0) for d in parts) / n, 1)
        except Exception:
            avg = 0.0
        # victim name -> did (best effort: regs + game names, else the
        # killer's own rating, rooms' conservative rule).
        name2did: dict[str, str] = {}
        _reg_teams: dict[str, list] = {}
        try:
            rres = supabase.table("bngc_ranked_tourney_registrations").select(
                "member_ids,member_names").eq(
                "tourney_id", (m or {}).get("tourney_id")).neq(
                "status", "banned").limit(500).execute()
            try:
                for _r in (rres.data or []):
                    _mids = [str(x).strip() for x in ((_r or {}).get("member_ids") or [])
                             if str(x or "").strip()]
                    if len(_mids) > 1:
                        for _d in _mids:
                            _reg_teams.setdefault(_d, _mids)
            except Exception:
                pass
            rids = sorted({str(x) for r in (rres.data or [])
                           for x in ((r or {}).get("member_ids") or []) if x})[:200]
            disp: dict[str, str] = {}
            for r in (rres.data or []):
                try:
                    for did, nm in (((r or {}).get("member_names")) or {}).items():
                        label = str((nm or {}).get("display_name")
                                    or (nm or {}).get("username") or "").strip()
                        if did and label:
                            disp[str(did)] = label
                except Exception:
                    continue
            if rids:
                try:
                    ures = supabase.table("bngc_users").select(
                        "discord_id,game_username").in_("discord_id", rids).execute()
                    for u in (ures.data or []):
                        try:
                            g = str((u or {}).get("game_username") or "").strip()
                            if g and str((u or {}).get("discord_id")):
                                disp.setdefault(str((u or {}).get("discord_id")), g)
                        except Exception:
                            continue
                except Exception:
                    pass
            for did, nm in disp.items():
                nn = _norm_nm(nm)
                if nn:
                    name2did.setdefault(nn, str(did))
        except Exception:
            pass
        # Eliminated victims never own a kill row (discord_id is the
        # killer's) and their alv row can be unmapped -without this they
        # drop out of parts entirely: no loss, no board row, no toast.
        # Resolve kill targets into parts so every death pays.
        try:
            _added: set[str] = set()
            try:
                _m_ign = _ignored_names(m)
            except Exception:
                _m_ign = set()
            for e in (events or []):
                try:
                    if (e or {}).get("etype") != "kill":
                        continue
                    _t = str((e or {}).get("target_name") or "").strip()
                    if not _t:
                        continue
                    if _norm_ignored_name(_t) in _m_ign:
                        continue
                    _v = name2did.get(_norm_nm(_t))
                    if _v and _v not in xset and _v not in parts:
                        parts.add(_v)
                        _added.add(_v)
                except Exception:
                    continue
            if _added:
                try:
                    _fresh = _tourney_ladder_snapshot(_added)
                    for _d in _added:
                        ladder[_d] = int((_fresh or {}).get(_d, 0))
                    n = len(parts)
                    avg = round(sum(ladder.get(d, 0) for d in parts) / n, 1) if n else 0.0
                except Exception:
                    pass
        except Exception:
            pass
        # Intruder exclusion: banned session members and per-match ignored
        # names score nowhere -not in parts, not as killers/victims, never
        # the winner. Dashboard Ban / match Ignore feed both lists.
        _banned: set = set()
        try:
            _br = supabase.table("bngc_ranked_tourney_registrations").select(
                "member_ids").eq("tourney_id", (m or {}).get("tourney_id")).eq(
                "status", "banned").limit(500).execute()
            for _r in ((_br.data) or []):
                for _x in ((_r or {}).get("member_ids") or []):
                    if str(_x or "").strip():
                        _banned.add(str(_x))
            _banned -= xset
        except Exception:
            _banned = set()
        try:
            _ign = _ignored_names(m)
        except Exception:
            _ign = set()
        try:
            if _banned:
                parts -= _banned
                n = len(parts)
                avg = round(sum(ladder.get(d, 0) for d in parts) / n, 1) if n else 0.0
        except Exception:
            pass
        awards: list[dict] = []
        counts: dict[str, int] = {}
        # death -> killer for killer-relative losses (last killer wins).
        # Only resolved victims count: an unresolvable target falls back
        # to the killer's own did, which reads as "unknown killer".
        death_killer: dict[str, str] = {}
        for e in events:
            try:
                if (e or {}).get("etype") != "kill":
                    continue
                killer = str((e or {}).get("discord_id") or "").strip()
                if not killer or killer in xset or killer in _banned:
                    continue
                target = str((e or {}).get("target_name") or "").strip()
                if _norm_ignored_name(target) in _ign:
                    continue
                victim = name2did.get(_norm_nm(target)) or killer
                if victim in xset or victim in _banned:
                    victim = killer
                if victim in xset:
                    victim = killer
                try:
                    if victim != killer and victim in parts:
                        death_killer[victim] = killer
                except Exception:
                    pass
                n_k = int(counts.get(killer, 0)) + 1
                counts[killer] = n_k
                awards.append({
                    "killer": killer, "victim": victim, "nth": n_k,
                    "points": tourney_kill_delta(
                        ladder.get(killer, 0), ladder.get(victim, 0), avg, n),
                })
            except Exception:
                continue
        # placement purse per place from the current holder (death order,
        # manual map wins); unheld places fall back to the session avg.
        death_order: list[str] = []
        seen_dead: set[str] = set()
        for e in events:
            try:
                if (e or {}).get("etype") == "alv" and (e or {}).get("alive") is False:
                    did = str((e or {}).get("discord_id") or "").strip()
                    if did and did not in xset and did not in seen_dead:
                        seen_dead.add(did)
                        death_order.append(did)
            except Exception:
                continue
        order = [d for d in reversed(death_order) if d in parts]
        try:
            man = {str(k): int(v) for k, v in
                   (((m or {}).get("placements")) or {}).items()
                   if str(k or "").strip() and str(k) not in xset}
        except Exception:
            man = {}
        holder_of = {did: i + 1 for i, did in enumerate(order)}
        holder_of.update({d: p for d, p in man.items() if d in parts})
        place_of = {p: d for d, p in holder_of.items()}
        # Top-half placement only: the purse covers places 1..ceil(n/2), so
        # last place (P2 of 2, P4 of 6, ...) pays nothing. The engine pays
        # purse[p-1] only when p fits the list, so truncating IS the rule —
        # frozen verbatim, replayed forever, no engine change.
        try:
            _paid_places = max(1, -(-int(n) // 2))
        except Exception:
            _paid_places = 5
        placement = [tourney_place_delta(p, ladder.get(place_of.get(p), avg), avg)
                     for p in range(1, 6)][:max(1, min(5, _paid_places))]
        try:
            wdid = str((m or {}).get("winner_discord_id") or "").strip() or None
            if not wdid and order:
                wdid = order[0]
            if wdid and (wdid in xset or wdid in _banned):
                wdid = None
        except Exception:
            wdid = None
        # Duo co-winners: teammates of the winner who survived (no kill
        # evidence against them) share P1 -otherwise the surviving
        # partner "wins" the game and still pays a defeat. Solo regs
        # (single member), removed players, dead teammates (their death
        # placement + loss stand) and manually-placed teammates are
        # untouched. Engine P1-tie semantics pay every holder the win
        # bonus with no loss.
        try:
            if wdid:
                _mates = (_reg_teams or {}).get(str(wdid)) or []
                if len(_mates) > 1:
                    _deadset = set()
                    try:
                        _deadset = {str(v) for v in (death_killer or {})}
                        _deadset |= {str(d) for d in (death_order or [])}
                    except Exception:
                        pass
                    _tie1 = []
                    for _tm in _mates:
                        try:
                            _t = str(_tm or "").strip()
                            if (not _t or _t == str(wdid) or _t in xset
                                    or _t in _banned or _t not in parts or _t in _deadset
                                    or _t in (man or {})):
                                continue
                            _tie1.append(_t)
                        except Exception:
                            continue
                    if _tie1:
                        try:
                            _base_pl = dict(((m or {}).get("placements")) or {})
                            for _t in _tie1:
                                _base_pl.setdefault(str(_t), 1)
                            m = dict(m or {}, placements=_base_pl)
                        except Exception:
                            pass
        except Exception:
            pass
        win = tourney_win_delta(ladder.get(wdid, avg) if wdid else avg, n)
        # Killer-relative seal-time losses, frozen with the table: dying to
        # a weaker killer costs up to -50, dying up is cheap, unknown/zone
        # killers pay the flat tier stake. Stored for every involved did.
        try:
            loss = {}
            for d in parts:
                try:
                    _kd = death_killer.get(str(d))
                    if _kd:
                        loss[str(d)] = int(_ladder_loss_vs(
                            ladder.get(str(d), 0), ladder.get(_kd)))
                    else:
                        loss[str(d)] = int(_ladder_loss(ladder.get(str(d), 0)))
                except Exception:
                    try:
                        loss[str(d)] = int(_ladder_loss(ladder.get(str(d), 0)))
                    except Exception:
                        continue
        except Exception:
            loss = {}
        table = tourney_table_for(avg, n, awards, win, placement, loss)
        try:
            # Per-player seal components (clamped totals) for toasts/embeds:
            # same math the boards replay, so board == toast totals.
            _, bd = _score_frozen_with_breakdown(table, m, events)
            if bd:
                table["breakdown"] = {str(k): v for k, v in bd.items()}
        except Exception:
            pass
        try:
            supabase.table("bngc_ranked_tourney_matches").update(
                {"points_table": table, "avg_points": avg}).eq(
                "id", int(mid)).execute()
        except Exception:
            # pre-v249 (no columns): table still returned for this call
            pass
        return table
    except Exception:
        return None


def _score_room_seal_bundle(ft: dict, m: dict, events: list) -> list[dict]:
    """Score ONE match from its stored room-seal deltas. Pure.

    The room seal already paid these exact deltas to the ladder at seal
    time, so the board replays them verbatim (board == ladder movement):
    no kill math, no loss, no clamp (the seal applied all three). Names
    resolve from the banked tourney events, falling back to did prefix.
    Manual point_awards on the tourney row still apply on top (dashboard
    corrections). Never raises; [] on failure.
    """
    try:
        pts = {str(k): int(v) for k, v in ((ft or {}).get("points") or {}).items()
               if str(k or "").strip()}
        if not pts:
            return []
        try:
            kills = {str(k): int(v) for k, v in ((ft or {}).get("kills") or {}).items()}
        except Exception:
            kills = {}
        try:
            plc = {str(k): int(v) for k, v in ((ft or {}).get("placements") or {}).items()}
        except Exception:
            plc = {}
        try:
            winner = str((ft or {}).get("winner") or (m or {}).get("winner_discord_id") or "")
        except Exception:
            winner = ""
        try:
            awards_map = ((m or {}).get("point_awards")) or {}
        except Exception:
            awards_map = {}
        try:
            breakdown_map = ((ft or {}).get("breakdown")) or {}
        except Exception:
            breakdown_map = {}
        names: dict[str, str] = {}
        for e in (events or []):
            try:
                did = str((e or {}).get("discord_id") or "").strip()
                nm = str((e or {}).get("actor_name") or "").strip()
                if did and nm and did not in names:
                    names[did] = nm[:32]
            except Exception:
                continue
        out = []
        for did, p in pts.items():
            try:
                extra = 0
                try:
                    extra = int((awards_map or {}).get(did) or 0)
                except Exception:
                    extra = 0
                try:
                    _bd = (breakdown_map or {}).get(did) or {}
                    _breakdown = {"kills": int(_bd.get("kills") or 0),
                                  "win": int(_bd.get("win") or 0),
                                  "placement": int(_bd.get("placement") or 0),
                                  "loss": int(_bd.get("loss") or 0),
                                  "total": int(_bd.get("total", p) or 0),
                                  "capped": bool(_bd.get("capped", False))}
                except Exception:
                    _breakdown = None
                _row = {"discord_id": did, "name": names.get(did) or did[:6],
                        "kills": int(kills.get(did) or 0),
                        "placement": plc.get(did),
                        "won": bool(winner and did == winner),
                        "points": int(p) + extra}
                if _breakdown is not None:
                    _row["breakdown"] = _breakdown
                out.append(_row)
            except Exception:
                continue
        out.sort(key=lambda r: ((r or {}).get("points", 0),
                                (r or {}).get("kills", 0)), reverse=True)
        return out
    except Exception:
        return []


def _score_frozen_with_breakdown(ft: dict, m: dict, events: list) -> tuple[list, dict]:
    """Frozen tourney-dynamic-v1 scoring + per-player seal components. Pure
    except the post-freeze loss fallback (current-ladder loss for a did that
    postdates the freeze, e.g. linked after sealing).

    Kills inject per-killer frozen awards on a kill-0 base, win/placement
    come from the frozen table, non-winners pay the frozen loss. Each
    player's delta is clamped to the ranked per-match clamp (-50/+30, same
    as rooms) so tourney boards and ladder movement obey one rule.

    Returns (rows, breakdown) with rows shaped like _score_tourney_match
    and breakdown {did: {kills, win, placement, loss, total, capped}} for
    toasts/embeds (same shape as rooms' arena_breakdown). Never raises;
    ([], {}) on failure.
    """
    try:
        winner = (m or {}).get("winner_discord_id")
        try:
            removed = [str(x) for x in ((m or {}).get("removed_discord_ids") or [])
                       if str(x or "").strip()]
        except Exception:
            removed = []
        try:
            awards_map = (m or {}).get("point_awards") or {}
        except Exception:
            awards_map = {}
        try:
            base = {"kill": 0, "win": int((ft or {}).get("win") or 0),
                    "placement": list((ft or {}).get("placement") or [])}
        except Exception:
            base = {"kill": 0, "win": 0, "placement": []}
        rows = compute_match_points(
            events or [], base, winner, (m or {}).get("placements"),
            removed, awards_map)
        try:
            bonus: dict[str, int] = {}
            for a in ((ft or {}).get("kill_awards") or []):
                try:
                    k = str((a or {}).get("killer") or "").strip()
                    if k:
                        bonus[k] = bonus.get(k, 0) + int((a or {}).get("points") or 0)
                except Exception:
                    continue
        except Exception:
            bonus = {}
        try:
            lossmap = {str(k): int(v) for k, v in
                       ((ft or {}).get("loss_awards") or {}).items()}
        except Exception:
            lossmap = {}
        try:
            # victim -> killer from the frozen awards (last wins), for the
            # post-freeze fallback below (did linked after sealing).
            deathmap: dict[str, str] = {}
            for a in ((ft or {}).get("kill_awards") or []):
                try:
                    _k = str((a or {}).get("killer") or "").strip()
                    _v = str((a or {}).get("victim") or "").strip()
                    if _k and _v and _v != _k:
                        deathmap[_v] = _k
                except Exception:
                    continue
        except Exception:
            deathmap = {}
        try:
            purse = list((ft or {}).get("placement") or [])
        except Exception:
            purse = []
        try:
            twin = int((ft or {}).get("win") or 0)
        except Exception:
            twin = 0
        out, breakdown = [], {}
        for r in rows or []:
            try:
                did = str((r or {}).get("discord_id") or "")
                won = bool((r or {}).get("won"))
                try:
                    plc = (r or {}).get("placement")
                    plc = int(plc) if plc is not None else None
                except Exception:
                    plc = None
                kb = int(bonus.get(did, 0))
                wb = twin if won else 0
                try:
                    pp = purse[plc - 1] if plc and 1 <= plc <= len(purse) else 0
                except Exception:
                    pp = 0
                lb = 0
                if not won:
                    if did in lossmap:
                        lb = int(lossmap[did])
                    else:
                        try:
                            _snap = _tourney_ladder_snapshot([did])
                            _kd = deathmap.get(did)
                            if _kd:
                                _ksnap = _tourney_ladder_snapshot([_kd])
                                lb = int(_ladder_loss_vs(
                                    _snap.get(did, 0), _ksnap.get(_kd)))
                            else:
                                lb = int(_ladder_loss(_snap.get(did, 0)))
                        except Exception:
                            lb = 0
                raw = int((r or {}).get("points") or 0) + kb + (lb if not won else 0)
                try:
                    total = int(_clamp_match_delta(raw))
                except Exception:
                    total = raw
                breakdown[did] = {"kills": kb, "win": wb,
                                  "placement": int(pp), "loss": int(lb),
                                  "total": total,
                                  "capped": bool(raw != total)}
                out.append({"discord_id": did, "name": (r or {}).get("name"),
                            "kills": int((r or {}).get("kills") or 0),
                            "placement": plc,
                            "won": won, "points": total,
                            "breakdown": dict(breakdown.get(did) or {})})
            except Exception:
                continue
        # Victims with no engine row (no kills, no mapped alv, no manual
        # placement) still paid the frozen loss at seal time -they must
        # board as loss-only rows or they vanish: no board, no ladder pay,
        # no toast. Winner never lands here.
        try:
            _seen = {str((r or {}).get("discord_id") or "") for r in (out or [])}
            for _did, _lb in (lossmap or {}).items():
                try:
                    _d = str(_did or "").strip()
                    if not _d or _d in _seen or _d in {str(x) for x in (removed or [])}:
                        continue
                    if _d == str(winner or ""):
                        continue
                    _lbv = int(_lb or 0)
                    try:
                        _raw = _lbv + int((awards_map or {}).get(_d) or 0)
                    except Exception:
                        _raw = _lbv
                    try:
                        _total = int(_clamp_match_delta(_raw))
                    except Exception:
                        _total = _raw
                    breakdown[_d] = {"kills": 0, "win": 0, "placement": 0,
                                     "loss": int(_lbv), "total": int(_total),
                                     "capped": bool(_raw != _total)}
                    out.append({"discord_id": _d, "name": _d[:6],
                                "kills": 0, "placement": None,
                                "won": False, "points": int(_total),
                                "breakdown": dict(breakdown.get(_d) or {})})
                    _seen.add(_d)
                except Exception:
                    continue
        except Exception:
            pass
        out.sort(key=lambda r: ((r or {}).get("points", 0),
                                (r or {}).get("kills", 0)), reverse=True)
        return out, breakdown
    except Exception:
        return [], {}


def _score_tourney_match(tourney: dict | None, m: dict, events: list) -> list[dict]:
    """Score ONE match: stored room-seal replay, frozen dynamic replay,
    else legacy static replay.

    Room-seal (mode room-seal-v1): the seal's ladder deltas, verbatim.
    Frozen (mode tourney-dynamic-v1): kills inject per-killer frozen awards
    on a kill-0 base, win/placement come from the frozen table, non-winners
    pay the frozen loss (current-ladder loss when the did postdates the
    freeze, e.g. linked after sealing). Legacy (no frozen table): the exact
    old static replay -comp math on the tourney table, no loss.
    Never raises; [] on failure.
    """
    try:
        ft0 = (m or {}).get("points_table")
        if isinstance(ft0, dict) and ft0.get("mode") == "room-seal-v1":
            return _score_room_seal_bundle(ft0, m, events)
        winner = (m or {}).get("winner_discord_id")
        try:
            removed = [str(x) for x in ((m or {}).get("removed_discord_ids") or [])
                       if str(x or "").strip()]
        except Exception:
            removed = []
        try:
            awards_map = (m or {}).get("point_awards") or {}
        except Exception:
            awards_map = {}
        ft = (m or {}).get("points_table")
        if isinstance(ft, dict) and ft.get("mode") == "tourney-dynamic-v1":
            try:
                rows, bd = _score_frozen_with_breakdown(ft, m, events)
                try:
                    for _r in (rows or []):
                        try:
                            _d = str((_r or {}).get("discord_id") or "")
                            if _d and "breakdown" not in (_r or {}) and _d in (bd or {}):
                                _r["breakdown"] = dict((bd or {})[_d] or {})
                        except Exception:
                            continue
                except Exception:
                    pass
                return rows
            except Exception:
                return []
        # legacy static replay (bit-identical to the old path)
        try:
            table = _table_of(tourney)
        except Exception:
            table = dict(DEFAULT_TABLE)
        return compute_match_points(
            events or [], table, winner, (m or {}).get("placements"),
            removed, awards_map)
    except Exception:
        return []


def ranked_standings_for_matches(tourney: dict | None, sealed: list[dict],
                                 events_by_match: dict,
                                 adjustments: list[dict] | None = None):
    """(per_match, board) scored dynamically per match.

    Each sealed match replays its frozen table (or legacy static when no
    freeze exists); the aggregate board folds the shared adjustments
    ledger on top. Never raises.
    """
    per_match = []
    try:
        for m in (sealed or []):
            try:
                per_match.append(_score_tourney_match(
                    tourney, m, (events_by_match or {}).get((m or {}).get("id"), [])))
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


def _attach_frozen_tables(matches: list) -> list:
    """Merge frozen {points_table, avg_points} into match dicts (bulk, once).

    Pre-v249 databases (no columns) degrade to a no-op, so every scoring
    path stays working and falls back to legacy replay. Never raises.
    """
    try:
        rows = list(matches or [])
        ids = [m.get("id") for m in rows if (m or {}).get("id") is not None
               and (m or {}).get("points_table") is None]
        if not ids or not supabase:
            return rows
        try:
            res = supabase.table("bngc_ranked_tourney_matches").select(
                "id,points_table,avg_points").in_("id", ids[:200]).execute()
        except Exception:
            return rows  # pre-v249: no columns, legacy replay everywhere
        by_id = {}
        try:
            by_id = {(r or {}).get("id"): r for r in (res.data or [])}
        except Exception:
            pass
        for m in rows:
            try:
                r = by_id.get((m or {}).get("id"))
                if r and (m or {}).get("points_table") is None:
                    m["points_table"] = r.get("points_table")
                    m["avg_points"] = r.get("avg_points")
            except Exception:
                continue
        return rows
    except Exception:
        return list(matches or [])


def code_tourney_points(code: str | None, discord_id: str | None) -> dict | None:
    """My live points for the tourney tagged to this room code. For the
    passthrough HUD. Returns {tourney_id, name, points, kills, wins, rank,
    players} or None (no tourney / no rows / DB down). Never raises."""
    try:
        code = str(code or "").strip().upper()
        did = str(discord_id or "").strip()
        if not code or not did or not supabase:
            return None
        tres = supabase.table("bngc_ranked_tourneys").select(
            "id,name,points_table,series_len").eq("code", code).neq(
            "status", "ended").limit(2).execute()
        rows = tres.data or []
        if len(rows) != 1:
            return None
        t = rows[0]
        try:
            _series_len = int(t.get("series_len") or 0)
        except Exception:
            _series_len = 0
        mres = supabase.table("bngc_ranked_tourney_matches").select(
            "id,version,winner_discord_id,placements,removed_discord_ids,point_awards").eq("tourney_id", t["id"]).in_(
            "status", ["sealed", "manual"]).order("version").limit(200).execute()
        sealed = mres.data or []
        if not sealed:
            return {"tourney_id": t["id"], "name": t.get("name"), "points": 0,
                    "kills": 0, "wins": 0, "rank": None, "players": 0,
                    "series_len": _series_len, "sealed": 0}
        mids = [m["id"] for m in sealed]
        eres = supabase.table("bngc_ranked_tourney_events").select(
            "match_id,etype,discord_id,actor_name,alive").in_(
            "match_id", mids[:200]).limit(5000).execute()
        by_mid: dict = {}
        for e in (eres.data or []):
            by_mid.setdefault((e or {}).get("match_id"), []).append(e)
        attached = _attach_frozen_tables(sealed)
        per_match, board = ranked_standings_for_matches(
            t, attached, by_mid, _tourney_adjustments(t.get("id")))
        # Latest sealed result for me (drives component toasts): stored
        # room-seal bundle or frozen dynamic breakdown -the exact deltas
        # the board paid. Legacy rows (no stored components) stay None and
        # the client falls back to its single-delta toast.
        last_result = None
        try:
            for m in sorted(attached, key=lambda x: int((x or {}).get("version") or 0),
                            reverse=True):
                ft = (m or {}).get("points_table")
                bd = None
                try:
                    if isinstance(ft, dict) and ft.get("mode") == "room-seal-v1":
                        bd = ((ft.get("breakdown") or {}).get(did)) or None
                    elif isinstance(ft, dict) and ft.get("mode") == "tourney-dynamic-v1":
                        bd = ((ft.get("breakdown") or {}).get(did)) or None
                except Exception:
                    bd = None
                if not (bd and isinstance(bd, dict)):
                    continue
                try:
                    plc = (((m or {}).get("placements")) or {}).get(did)
                    plc = int(plc) if plc is not None else None
                except Exception:
                    plc = None
                try:
                    kc = sum(1 for e in (by_mid.get((m or {}).get("id"), []) or [])
                             if (e or {}).get("etype") == "kill"
                             and str((e or {}).get("discord_id") or "") == did)
                except Exception:
                    kc = 0
                last_result = {
                    "match_version": int((m or {}).get("version") or 0),
                    "gained": int(bd.get("total") or 0),
                    "won": bool(did and str((m or {}).get("winner_discord_id") or "")
                                == did),
                    "kills": int(kc),
                    "placement": plc,
                    "breakdown": {"kills": int(bd.get("kills") or 0),
                                  "win": int(bd.get("win") or 0),
                                  "placement": int(bd.get("placement") or 0),
                                  "loss": int(bd.get("loss") or 0),
                                  "total": int(bd.get("total") or 0),
                                  "capped": bool(bd.get("capped"))},
                }
                break
        except Exception:
            last_result = None
        me = next((r for r in board if str(r.get("discord_id")) == did), None)
        rank = next((i + 1 for i, r in enumerate(board) if str(r.get("discord_id")) == did), None)
        if me is None:
            return {"tourney_id": t["id"], "name": t.get("name"), "points": 0,
                    "kills": 0, "wins": 0, "avg_placement": None,
                    "rank": None, "players": len(board),
                    "series_len": _series_len, "sealed": len(sealed),
                    "last_result": last_result}
        return {"tourney_id": t["id"], "name": t.get("name"),
                "points": int(me.get("points") or 0), "kills": int(me.get("kills") or 0),
                "wins": int(me.get("wins") or 0), "avg_placement": me.get("avg_placement"),
                "rank": rank, "players": len(board),
                "series_len": _series_len, "sealed": len(sealed),
                "last_result": last_result}
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
        tres = supabase.table("bngc_ranked_tourneys").select(
            "id,name,points_table").eq("status", "started").limit(20).execute()
        trous = tres.data or []
        if not trous:
            return {}
        tids = [t["id"] for t in trous]
        mres = supabase.table("bngc_ranked_tourney_matches").select(
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
        eres = supabase.table("bngc_ranked_tourney_events").select(
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
            per_match, board = ranked_standings_for_matches(
                t, _attach_frozen_tables(list(tmap.values())), evs,
                _tourney_adjustments(t.get("id")))
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


@router.get("/ranked/status")
async def ranked_status():
    """Live tracker counts for ranked tourneys (same shape as /comp/status).

    Counts come from the ranked-tourney tables, so the /ranked dashboard
    tracks its own series. Never 500s on blips."""
    if not supabase:
        return {"ok": False, "error": "Server not configured", "open_matches": 0}
    try:
        mres = await db_io(lambda: supabase.table("bngc_ranked_tourney_matches").select(
            "id,status").in_("status", ["open", "manual", "sealed"]).limit(2000).execute())
        rows = mres.data or []
        n_open = sum(1 for r in rows if (r or {}).get("status") in ("open", "manual"))
        n_sealed = sum(1 for r in rows if (r or {}).get("status") == "sealed")
    except Exception:
        n_open, n_sealed = -1, -1
    try:
        eres = await db_io(lambda: supabase.table("bngc_ranked_tourney_events").select(
            "id").is_("discord_id", "null").limit(1000).execute())
        n_unmapped = len(eres.data or [])
    except Exception:
        n_unmapped = -1
    return {
        "ok": True,
        "service": "ranked-tourney (python)",
        "open_matches": n_open,
        "sealed_matches": n_sealed,
        "unmapped_events": n_unmapped,
        "note": "ranked tourneys: own series, ladder scoring; -1 = transient DB blip",
    }


@router.get("/api/ranked/tourneys")
async def list_tourneys(request: Request):
    err = _need()
    if err:
        return err
    want = str(request.query_params.get("status") or "all").strip().lower()
    if want not in ("all", "created", "scheduled", "running", "ended"):
        return JSONResponse({"error": "status must be all|created|scheduled|running|ended"}, status_code=400)
    try:
        tres = supabase.table("bngc_ranked_tourneys").select(
            "id,code,name,status,series_len,scheduled_at,started_at,ended_at,created_at"
        ).order("created_at", desc=False).limit(100).execute()
        rows = tres.data or []
        tids = [r["id"] for r in rows]
        sealed_by: dict = {}
        players_by: dict = {}
        if tids:
            try:
                mres = supabase.table("bngc_ranked_tourney_matches").select(
                    "tourney_id,status").in_("tourney_id", tids).limit(2000).execute()
                for m in (mres.data or []):
                    if (m or {}).get("status") in ("sealed", "manual"):
                        sealed_by[m["tourney_id"]] = sealed_by.get(m["tourney_id"], 0) + 1
            except Exception:
                pass
            try:
                rres = supabase.table("bngc_ranked_tourney_registrations").select(
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


@router.get("/api/ranked/tourney")
async def tourney_detail(request: Request):
    err = _need()
    if err:
        return err
    try:
        tid = int(request.query_params.get("id"))
    except Exception:
        return JSONResponse({"error": "id required (numeric, from 0)"}, status_code=400)
    try:
        tres = supabase.table("bngc_ranked_tourneys").select("*").eq("id", tid).limit(1).execute()
        if not tres.data:
            return JSONResponse({"error": "tourney not found"}, status_code=404)
        t = tres.data[0]
        try:
            mres = supabase.table("bngc_ranked_tourney_matches").select(
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
                eres = supabase.table("bngc_ranked_tourney_events").select(
                    "match_id,etype,discord_id,actor_name,alive"
                ).in_("match_id", mids[:200]).limit(5000).execute()
                for e in (eres.data or []):
                    by_mid.setdefault((e or {}).get("match_id"), []).append(e)
            per_match, board = ranked_standings_for_matches(
                t, _attach_frozen_tables(sealed), by_mid,
                _tourney_adjustments(t.get("id")))
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
                rres = supabase.table("bngc_ranked_tourney_registrations").select(
                    "team_label,member_ids,member_names,captain_id,status"
                ).eq("tourney_id", tid).eq("status", "accepted").limit(500).execute()
            except Exception:
                rres = supabase.table("bngc_ranked_tourney_registrations").select(
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
        # Rank display: per-player ranked-ladder points + tourney average.
        # Display only -tourney scoring reads the ladder at seal time and
        # freezes it per match; this snapshot just labels the board.
        try:
            _rost = sorted({str(x) for r in regs for x in (r.get("member_ids") or []) if x})
            _rank = _tourney_ladder_snapshot(_rost)
        except Exception:
            _rank, _rost = {}, []
        try:
            for _r in board:
                try:
                    _d = str((_r or {}).get("discord_id") or "")
                    if _d and not (_r or {}).get("team") and _d in _rank:
                        _r["rank_pts"] = _rank[_d]
                except Exception:
                    continue
        except Exception:
            pass
        try:
            _avg = round(sum(_rank.values()) / len(_rank), 1) if _rank else None
            _ms = _ladder_milestone(_avg or 0).get("name") if _avg is not None else None
        except Exception:
            _avg, _ms = None, None
        return JSONResponse({
            "tourney": {"id": t["id"], "code": t.get("code"), "name": t.get("name"),
                        "status": derive_status(t, len(sealed)),
                        "mode": str(t.get("mode") or "solo").lower(),
                        "series_len": t.get("series_len") or 0, "sealed": len(sealed),
                        "points_table": table, "scheduled_at": t.get("scheduled_at"),
                        "started_at": t.get("started_at"), "ended_at": t.get("ended_at"),
                        # Hosts panel (dashboard): creator + listed hosts, plus
                        # the bridge's host_playing flag. Display only.
                        "created_by": (str(t.get("created_by") or "") or None),
                        "hosts": [str(x) for x in (t.get("hosts") or []) if str(x or "").strip()],
                        "host_playing": t.get("host_playing") is not False,
                        "rank_avg": _avg, "rank_milestone": _ms},
            "players": [{"discord_id": str(x), "name": names.get(str(x), str(x)[:6])}
                        for r in regs for x in (r.get("member_ids") or []) if x],
            "reg_names": reg_names,
            "matches": [{"id": m.get("id"), "version": m.get("version"), "status": m.get("status"),
                          "room_id": (str(m.get("room_id") or "")[:8] or None),
                          "winner": winners.get(m.get("id"))}
                         for m in matches],
            "match_boards": [{"version": m.get("version"),
                               "status": m.get("status"),
                               "paid": _match_ladder_paid(m, t),
                               "rows": (per_match[i] if i < len(per_match) else [])[:10]}
                              for i, m in enumerate(sealed)],
            "standings": board,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _match_ladder_paid(m: dict | None, t: dict | None) -> bool:
    """True when this sealed match already moved the ladder. Pure.

    Room-seal bundles paid via the room seal; dynamic rows pay per seal
    (ladder_paid marker); manual rows settle at stop (tourney points_paid
    flag). Anything else (legacy, unpaid) is False. Never raises.
    """
    try:
        m, t = (m or {}), (t or {})
        if str(m.get("status") or "") == "manual":
            return bool(t.get("points_paid"))
        if str(m.get("status") or "") != "sealed":
            return False
        ft = m.get("points_table")
        if not isinstance(ft, dict):
            return False
        if ft.get("mode") == "room-seal-v1":
            return True
        if ft.get("mode") == "tourney-dynamic-v1":
            return isinstance(ft.get("ladder_paid"), dict)
        return False
    except Exception:
        return False


async def _json(request: Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


@router.get("/api/ranked/match")
async def comp_match_detail(request: Request):
    """One match for the dashboard match view: live board + killfeed.

    Works for open matches too (board computed from events so far).
    Returns {match, tourney, board, killfeed}. Never raises.

    Shape dispatch: ?id= serves a ranked-tourney match below;
    ?room_id=&match_version= is the userscript live overlay shape and is
    served by ranked_api (ranked-room board) so the in-game overlay keeps
    working on this path."""
    try:
        has_id = request.query_params.get("id") is not None
    except Exception:
        has_id = False
    if not has_id:
        try:
            from . import ranked_api as _ranked_api
            return await _ranked_api.ranked_match_detail(request)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    err = _need()
    if err:
        return err
    try:
        mid = int(request.query_params.get("id"))
    except Exception:
        return JSONResponse({"error": "id required (match id)"}, status_code=400)
    try:
        try:
            mres = supabase.table("bngc_ranked_tourney_matches").select(
                "id,tourney_id,version,room_id,status,winner_discord_id,placements,removed_discord_ids,point_awards,started_at,sealed_at"
            ).eq("id", mid).limit(1).execute()
        except Exception:
            mres = supabase.table("bngc_ranked_tourney_matches").select(
                "id,tourney_id,version,room_id,status,winner_discord_id,placements,started_at,sealed_at"
            ).eq("id", mid).limit(1).execute()
        mrows = mres.data or []
        if not mrows:
            return JSONResponse({"error": "match not found"}, status_code=404)
        m = mrows[0]
        try:
            _attach_frozen_tables([m])
        except Exception:
            pass
        try:
            removed = [str(x) for x in ((m or {}).get("removed_discord_ids") or []) if str(x or "").strip()]
        except Exception:
            removed = []
        try:
            tres = supabase.table("bngc_ranked_tourneys").select(
                "id,name,code,status,points_table").eq(
                "id", m.get("tourney_id")).limit(1).execute()
            t = (tres.data or [{}])[0]
        except Exception:
            t = {}
        table = _table_of(t)
        try:
            try:
                eres = supabase.table("bngc_ranked_tourney_events").select(
                    "id,match_id,etype,actor_nr,actor_name,target_name,discord_id,alive,manual,created_at"
                ).eq("match_id", mid).order("id").limit(1000).execute()
            except Exception:
                # pre-migration (no manual column): classic shape, manual unknown
                eres = supabase.table("bngc_ranked_tourney_events").select(
                    "id,match_id,etype,actor_nr,actor_name,target_name,discord_id,alive,created_at"
                ).eq("match_id", mid).order("id").limit(1000).execute()
            events = eres.data or []
        except Exception:
            events = []
        board = _score_tourney_match(t, m, events)
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
        res = supabase.table("bngc_ranked_tourney_matches").select("version").eq(
            "tourney_id", int(tourney_id)).order("version", desc=True).limit(1).execute()
        rows = res.data or []
        return int(rows[0].get("version", -1)) + 1 if rows else 0
    except Exception:
        return 0


@router.post("/api/ranked/match/open")
async def comp_match_open(request: Request):
    """Manual blank match (dashboard). Never raises beyond JSON errors.

    Manual matches start with started_at set (the dashboard Match Started
    bookend) and status manual -the bridge never auto-seals or writes to
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
        res = supabase.table("bngc_ranked_tourney_matches").insert({
            "tourney_id": tid, "version": ver,
            "room_id": (str(b.get("room_id") or "").strip() or None),
            "status": "manual",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        row = (res.data or [{}])[0]
        return JSONResponse({"ok": True, "match_id": row.get("id"), "version": ver})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/match/seal")
async def comp_match_seal(request: Request):
    """Seal an open/manual match (dashboard end-freeze).

    Freezes the match's dynamic scoring table from the current ladder
    right after sealing (best-effort: boards fall back to legacy replay
    until a freeze lands, and calculate backfills missing tables), then
    runs a calculate so boards refresh and the ladder is paid per seal."""
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
        res = await db_io(lambda: supabase.table("bngc_ranked_tourney_matches").update(
            {"status": "sealed", "sealed_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", mid).in_("status", ["open", "manual"]).execute())
        if not res.data:
            return JSONResponse({"error": "match not found or already sealed"}, status_code=404)
        try:
            log_comp(f"sealed match={mid} via dashboard")
        except Exception:
            pass
        try:
            frozen = await db_io(freeze_match_table, mid)
            if frozen is None:
                try:
                    log_comp(f"seal match={mid}: dynamic freeze missed (legacy replay until calculate)")
                except Exception:
                    pass
        except Exception:
            pass
        try:
            # Ranked-session economy: sealing pays the ladder now (per-seal
            # deltas + boards refresh), not at stop. Best-effort.
            _tid = None
            try:
                _mr = await db_io(lambda: supabase.table(
                    "bngc_ranked_tourney_matches").select("tourney_id").eq(
                    "id", mid).limit(1).execute())
                _rows = (_mr.data or [])
                _tid = (_rows[0] or {}).get("tourney_id") if _rows else None
            except Exception:
                _tid = None
            if _tid is not None:
                await db_io(calculate_tourney_sync, int(_tid), "dashboard-seal")
        except Exception:
            pass
        try:
            # Losers' win streaks reset here: no defeat rows exist, so the
            # seal is the only place the loser is known. Best-effort.
            await db_io(reset_loser_streaks_sync, int(mid))
        except Exception:
            pass
        return JSONResponse({"ok": True, "match_id": mid})
    except Exception as e:
        return _db_err(e, f"seal match={mid}")


@router.post("/api/ranked/match/start")
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
        mres = supabase.table("bngc_ranked_tourney_matches").select(
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
        supabase.table("bngc_ranked_tourney_matches").update(
            {"started_at": now}).eq("id", mid).execute()
        return JSONResponse({"ok": True, "match_id": mid, "started_at": now})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/match/unseal")
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
        mres = supabase.table("bngc_ranked_tourney_matches").select(
            "id,status").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        if (mres.data[0] or {}).get("status") != "sealed":
            return JSONResponse({"ok": True, "match_id": mid,
                                 "status": (mres.data[0] or {}).get("status")})
        res = supabase.table("bngc_ranked_tourney_matches").update(
            {"status": "manual", "sealed_at": None}).eq("id", mid).execute()
        if not res.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        return JSONResponse({"ok": True, "match_id": mid, "status": "manual"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/match/winner")
async def comp_match_winner(request: Request):
    """Set/clear the manual winner override (dashboard).

    Shape dispatch: {match_id} serves a ranked-tourney match below;
    {room_id, match_version} is the userscript host-control shape and is
    served by ranked_api (ranked-room winner) so in-game controls keep
    working on this path."""
    try:
        _peek = await request.json()
        is_comp = isinstance(_peek, dict) and _peek.get("match_id") is not None
    except Exception:
        is_comp = True
    if not is_comp:
        try:
            from . import ranked_api as _ranked_api
            return await _ranked_api.ranked_match_winner(request)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
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
        mres = supabase.table("bngc_ranked_tourney_matches").select(
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
        res = supabase.table("bngc_ranked_tourney_matches").update(patch).eq("id", mid).execute()
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


@router.post("/api/ranked/tourney/create")
async def ranked_tourney_create(request: Request):
    """Create a ranked tourney (dashboard or /rtourney create).
    Body {name, series_len?, mode?, code?, game_mode?}."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        name = str(b.get("name") or "").strip()[:80]
    except Exception:
        name = ""
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    try:
        series_len = int(b.get("series_len") or 0)
        if series_len < 0 or series_len > 100:
            raise ValueError()
    except Exception:
        return JSONResponse({"error": "series_len must be 0..100"}, status_code=400)
    try:
        mode = str(b.get("mode") or "solo").strip().lower() or "solo"
        if mode not in ("solo", "duo"):
            raise ValueError()
    except Exception:
        return JSONResponse({"error": "mode must be solo|duo"}, status_code=400)
    try:
        code = str(b.get("code") or "").strip().upper()[:5] or None
        if code is not None and (len(code) != 5 or not code.isalnum()):
            return JSONResponse({"error": "code must be 5 alphanumerics"}, status_code=400)
    except Exception:
        code = None
    try:
        game_mode = str(b.get("game_mode") or "").strip()[:32] or None
    except Exception:
        game_mode = None
    try:
        res = supabase.table("bngc_ranked_tourneys").insert({
            "name": name, "code": code, "status": "created",
            "mode": mode, "series_len": series_len,
            "game_mode": game_mode,
        }).execute()
        row = (res.data or [{}])[0]
        return JSONResponse({"ok": True, "tourney_id": row.get("id"),
                             "name": name})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/tourney/table")
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
        res = supabase.table("bngc_ranked_tourneys").update(
            {"points_table": table}).eq("id", tid).execute()
        if not res.data:
            return JSONResponse({"error": "tourney not found"}, status_code=404)
        return JSONResponse({"ok": True, "tourney_id": tid, "points_table": table})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/tourney/host-playing")
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
        res = supabase.table("bngc_ranked_tourneys").update(
            {"host_playing": hp}).eq("id", tid).execute()
        if not res.data:
            return JSONResponse({"error": "tourney not found"}, status_code=404)
        return JSONResponse({"ok": True, "tourney_id": tid, "host_playing": hp})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/tourney/hosts")
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
        rres = supabase.table("bngc_ranked_tourneys").select("hosts").eq(
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
        ures = supabase.table("bngc_ranked_tourneys").update(
            {"hosts": hosts}).eq("id", tid).execute()
        if not ures.data:
            return JSONResponse({"error": "tourney not found"}, status_code=404)
        return JSONResponse({"ok": True, "tourney_id": tid, "hosts": hosts})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/tourney/code")
async def comp_tourney_code(request: Request):
    """Set (or clear) a session's room code by hand (dashboard code box).

    Body: {tourney_id, code}. Empty code clears. A set code must be 5
    letters/digits and unused by any other non-ended session (the bridge
    binds by code -duplicates would stall both sessions). Same rules as
    the bot's /ranked change-code. Never raises beyond JSON errors."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        tid = int(b.get("tourney_id"))
    except Exception:
        return JSONResponse({"error": "tourney_id required"}, status_code=400)
    try:
        code = str(b.get("code") or "").strip().upper() or None
        if code is not None and (len(code) != 5 or not code.isalnum()):
            return JSONResponse({"error": "code must be 5 letters/digits"}, status_code=400)
        tres = supabase.table("bngc_ranked_tourneys").select(
            "id,status").eq("id", tid).limit(1).execute()
        if not tres.data:
            return JSONResponse({"error": "tourney not found"}, status_code=404)
        if str((tres.data[0] or {}).get("status") or "") == "ended":
            return JSONResponse({"error": "session already ended"}, status_code=400)
        if code is not None:
            ores = supabase.table("bngc_ranked_tourneys").select("id").eq(
                "code", code).neq("status", "ended").limit(10).execute()
            taken = [str((r or {}).get("id")) for r in (ores.data or [])
                     if str((r or {}).get("id")) != str(tid)]
            if taken:
                return JSONResponse(
                    {"error": f"code already used by session #{taken[0]}"},
                    status_code=400)
        ures = supabase.table("bngc_ranked_tourneys").update(
            {"code": code}).eq("id", tid).execute()
        if not ures.data:
            return JSONResponse({"error": "tourney not found"}, status_code=404)
        return JSONResponse({"ok": True, "tourney_id": tid, "code": code})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/reg/add")
async def comp_reg_add(request: Request):
    """Add a player to a session roster by hand (dashboard add-player box).

    Body: {tourney_id, discord_id, display_name?}. Inserts one accepted
    solo row; refuses banned players and duplicates (idempotent —
    already-in returns ok). Mirrors the bot join flow. Never raises
    beyond JSON errors."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        tid = int(b.get("tourney_id"))
        did = str(b.get("discord_id") or "").strip()
    except Exception:
        return JSONResponse({"error": "tourney_id and discord_id required"},
                            status_code=400)
    if not did:
        return JSONResponse({"error": "discord_id required"}, status_code=400)
    try:
        rres = supabase.table("bngc_ranked_tourney_registrations").select(
            "id,member_ids,status").eq("tourney_id", tid).limit(500).execute()
        regs = rres.data or []
        for r in regs:
            mids = [str(x) for x in ((r or {}).get("member_ids") or [])]
            if did in mids and str((r or {}).get("status")) == "banned":
                return JSONResponse({"error": "player is banned from this session"},
                                    status_code=400)
            if did in mids and str((r or {}).get("status")) == "accepted":
                return JSONResponse({"ok": True, "tourney_id": tid,
                                     "discord_id": did, "present": True})
        names = {}
        try:
            _dn = str(b.get("display_name") or "").strip()[:32]
            if _dn:
                names = {did: {"username": _dn, "display_name": _dn}}
        except Exception:
            names = {}
        supabase.table("bngc_ranked_tourney_registrations").insert({
            "tourney_id": tid, "member_ids": [did],
            "member_names": names, "status": "accepted",
        }).execute()
        return JSONResponse({"ok": True, "tourney_id": tid,
                             "discord_id": did, "present": False})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/reg/remove")
async def comp_reg_remove(request: Request):
    """Remove a player from a session roster by hand (dashboard remove).

    Body: {tourney_id, discord_id}. Deletes their accepted solo rows (duo
    rows untouched -same rule as leaving via Discord). Missing player
    returns ok/not-present. Never raises beyond JSON errors."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        tid = int(b.get("tourney_id"))
        did = str(b.get("discord_id") or "").strip()
    except Exception:
        return JSONResponse({"error": "tourney_id and discord_id required"},
                            status_code=400)
    if not did:
        return JSONResponse({"error": "discord_id required"}, status_code=400)
    try:
        rres = supabase.table("bngc_ranked_tourney_registrations").select(
            "id,member_ids,status").eq("tourney_id", tid).limit(500).execute()
        n = 0
        for r in (rres.data or []):
            try:
                mids = [str(x) for x in ((r or {}).get("member_ids") or [])]
                if (mids == [did] and str((r or {}).get("status")) == "accepted"
                        and (r or {}).get("id") is not None):
                    supabase.table("bngc_ranked_tourney_registrations").delete().eq(
                        "id", (r or {}).get("id")).execute()
                    n += 1
            except Exception:
                continue
        return JSONResponse({"ok": True, "tourney_id": tid,
                             "discord_id": did, "removed": n,
                             "present": n == 0})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _pending_seal_tables(mids: list) -> dict:
    """{match_id: pending-table} for bridge-pending rows. Never raises.

    Separate narrow select (the calculate selects predate the points_table
    column): {} when the column/table is missing (pre-v249) or DB down."""
    try:
        if not supabase or not mids:
            return {}
        try:
            ids = [int(x) for x in (mids or []) if x is not None][:200]
        except Exception:
            return {}
        if not ids:
            return {}
        res = supabase.table("bngc_ranked_tourney_matches").select(
            "id,points_table").in_("id", ids).execute()
        out = {}
        for r in (res.data or []):
            try:
                pt = (r or {}).get("points_table")
                if isinstance(pt, dict) and pt.get("mode") == "room-seal-pending-v1":
                    out[(r or {}).get("id")] = pt
            except Exception:
                continue
        return out
    except Exception:
        return {}


def reset_loser_streaks_sync(mid: int) -> int:
    """Reset win_streak for a sealed match's losers. Sync. Never raises.

    No code banks "defeat" rows, so the batch streak logic can never reset
    on a loss -the seal is the one place the loser is known exactly.
    Leavers (removed_discord_ids) are spared: leaves are ignored by design
    (crashes happen). Winner None (total wipe) resets everyone involved.
    Returns reset count. One UPDATE.
    """
    try:
        if not supabase or mid is None:
            return 0
        try:
            mres = supabase.table("bngc_ranked_tourney_matches").select(
                "winner_discord_id,placements,removed_discord_ids,status").eq(
                "id", int(mid)).limit(1).execute()
        except Exception:
            return 0
        rows = mres.data or []
        if not rows:
            return 0
        m = rows[0] or {}
        if str(m.get("status") or "") not in ("sealed", "manual"):
            return 0
        try:
            removed = {str(x) for x in ((m or {}).get("removed_discord_ids") or [])
                       if str(x or "").strip()}
        except Exception:
            removed = set()
        involved: set[str] = set()
        try:
            eres = supabase.table("bngc_ranked_tourney_events").select(
                "discord_id").eq("match_id", int(mid)).limit(2000).execute()
            for e in (eres.data or []):
                try:
                    d = str((e or {}).get("discord_id") or "").strip()
                    if d:
                        involved.add(d)
                except Exception:
                    continue
        except Exception:
            pass
        try:
            for k in (((m or {}).get("placements")) or {}):
                if str(k or "").strip():
                    involved.add(str(k))
        except Exception:
            pass
        try:
            winner = str((m or {}).get("winner_discord_id") or "").strip() or None
        except Exception:
            winner = None
        losers = sorted((involved - removed) - ({winner} if winner else set()))
        if not losers:
            return 0
        try:
            supabase.table("bngc_users").update({"win_streak": 0}).in_(
                "discord_id", losers[:200]).execute()
            return len(losers)
        except Exception:
            return 0
    except Exception:
        return 0


def _pay_sealed_ladders(ms: list) -> dict:
    """Pay clamped per-seal deltas to the ladder. Sync. Never raises.

    For every status=sealed match with a tourney-dynamic-v1 frozen table
    carrying a stored breakdown and no ladder_paid marker, bumps
    ranked_points by the breakdown totals and stamps the marker (persisted
    on the match row). Room-seal bundles (ladder paid by the room seal),
    manual rows (settled at stop) and legacy rows (no components) are
    skipped. Returns {match_id: {did: delta}} paid this call. Best-effort
    exactly-once: concurrent calculates can double-pay a marker-less row,
    same as the rest of this module's best-effort writes.
    """
    paid: dict = {}
    try:
        if not supabase or not ms:
            return paid
        try:
            if _ranked_sleeping():
                return paid
        except Exception:
            pass
        for m in (ms or []):
            try:
                if str((m or {}).get("status") or "") != "sealed":
                    continue
                ft = (m or {}).get("points_table")
                if not (isinstance(ft, dict)
                        and ft.get("mode") == "tourney-dynamic-v1"):
                    continue
                if isinstance((ft or {}).get("ladder_paid"), dict):
                    continue
                bd = (ft or {}).get("breakdown")
                if not (isinstance(bd, dict) and bd):
                    continue
                deltas = {}
                for did, comp in bd.items():
                    try:
                        d = str(did or "").strip()
                        v = int((comp or {}).get("total") or 0)
                        if d and v:
                            deltas[d] = v
                    except Exception:
                        continue
                # Fresh marker re-read: the table above may be seconds old
                # and a concurrent calculate can pay the same row twice.
                # Re-read, skip when another writer already stamped it, and
                # pay the freshly read breakdown (same numbers by construction).
                try:
                    if (m or {}).get("id") is not None:
                        _chk = supabase.table("bngc_ranked_tourney_matches").select(
                            "points_table").eq("id", (m or {}).get("id")).limit(1).execute()
                        _rows = (_chk.data or [])
                        _fresh = ((_rows[0] or {}).get("points_table")
                                  if _rows else None)
                        if isinstance((_fresh or {}).get("ladder_paid"), dict):
                            try:
                                m["points_table"] = _fresh
                            except Exception:
                                pass
                            continue
                        _fbd = (_fresh or {}).get("breakdown") if isinstance(_fresh, dict) else None
                        if isinstance(_fbd, dict) and _fbd:
                            deltas = {}
                            for did, comp in _fbd.items():
                                try:
                                    d = str(did or "").strip()
                                    v = int((comp or {}).get("total") or 0)
                                    if d and v:
                                        deltas[d] = v
                                except Exception:
                                    continue
                except Exception:
                    pass
                try:
                    if deltas:
                        _ladder_bump(deltas)
                except Exception:
                    continue  # bump failed: no marker, next calculate retries
                marker = {str(k): int(v) for k, v in deltas.items()}
                # Stamp on the freshly read table when we have it, so a
                # concurrent writer's keys (e.g. a backfilled breakdown)
                # are preserved instead of clobbered by our stale copy.
                try:
                    _base = _fresh if isinstance(_fresh, dict) else ft
                except Exception:
                    _base = ft
                ft = dict(_base if isinstance(_base, dict) else (ft or {}))
                try:
                    from datetime import datetime, timezone
                    ft["ladder_paid_at"] = datetime.now(timezone.utc).isoformat()
                except Exception:
                    pass
                ft["ladder_paid"] = marker
                try:
                    if (m or {}).get("id") is not None:
                        supabase.table("bngc_ranked_tourney_matches").update(
                            {"points_table": ft}).eq(
                            "id", (m or {}).get("id")).execute()
                        m["points_table"] = ft
                        paid[(m or {}).get("id")] = marker
                except Exception:
                    continue
            except Exception:
                continue
    except Exception:
        pass
    return paid


def _calculate_sync(trous: dict, matches: list, by_mid: dict,
                    computed_by: str = "dashboard") -> dict:
    """Shared calculate core: per-match points + aggregate boards + snapshot
    rows into bngc_ranked_tourney_results. Sync (direct supabase calls, for request and
    bridge threads alike). Never raises; returns {str(tid): board}.

    Scoring is dynamic per match (frozen replay, legacy static fallback).
    Sealed/manual matches still missing a frozen table get one computed
    here first (backfill for pre-freeze seals), best-effort.
    """
    boards: dict = {}
    try:
        from datetime import datetime, timezone
        by_tid: dict = {}
        for m in matches or []:
            by_tid.setdefault((m or {}).get("tourney_id"), []).append(m)
        for tid, ms in by_tid.items():
            trow = (trous or {}).get(tid) or {}
            try:
                # Bridge pending join keys -> stored seal bundles (room seals
                # that landed after the tourney seal). Resolved here so the
                # freeze loop below and every board replay see the bundle.
                _pend = _pending_seal_tables([m.get("id") for m in ms])
                if _pend:
                    from ..lib import ranked as _rk_seal
                    for m in ms:
                        try:
                            pt = _pend.get((m or {}).get("id"))
                            if not pt:
                                continue
                            b = _rk_seal.seal_bundle_for(
                                pt.get("rid"), pt.get("room_match_version"))
                            if isinstance(b, dict):
                                supabase.table("bngc_ranked_tourney_matches").update(
                                    {"points_table": b}).eq(
                                    "id", (m or {}).get("id")).execute()
                                m["points_table"] = b
                        except Exception:
                            continue
            except Exception:
                pass
            try:
                for m in ms:
                    if (m or {}).get("points_table") is None and (m or {}).get("id") is not None:
                        ft = freeze_match_table((m or {}).get("id"))
                        if isinstance(ft, dict):
                            m["points_table"] = ft
            except Exception:
                pass
            try:
                # Breakdown backfill: frozen tables sealed before stored
                # breakdowns existed get clamped per-player components so
                # toasts/embeds match the (now clamped) boards. One write
                # per match, then the row is current.
                for m in ms:
                    try:
                        ft = (m or {}).get("points_table")
                        if not (isinstance(ft, dict)
                                and ft.get("mode") == "tourney-dynamic-v1"):
                            continue
                        if isinstance(ft.get("breakdown"), dict):
                            continue
                        _, bd = _score_frozen_with_breakdown(
                            ft, m, (by_mid or {}).get((m or {}).get("id"), []))
                        if bd and (m or {}).get("id") is not None:
                            ft = dict(ft)
                            ft["breakdown"] = {str(k): v for k, v in bd.items()}
                            try:
                                supabase.table("bngc_ranked_tourney_matches").update(
                                    {"points_table": ft}).eq(
                                    "id", (m or {}).get("id")).execute()
                            except Exception:
                                pass
                            m["points_table"] = ft
                    except Exception:
                        continue
            except Exception:
                pass
            per: dict = {}
            for m in ms:
                try:
                    per[m["id"]] = _score_tourney_match(
                        trow, m, (by_mid or {}).get(m["id"], []))
                except Exception:
                    per[m["id"]] = []
            try:
                # Per-seal ladder pay (ranked-session economy): every sealed
                # dynamic match pays its clamped breakdown deltas to
                # ranked_points immediately, so counters move per match.
                # Room-seal bundles already paid via the room seal, manual
                # rows settle at stop, legacy rows carry no components —
                # none of those are touched. Exactly-once per match via the
                # ladder_paid marker stored on the frozen table (no schema
                # change; backfills preserve it). Never breaks the board.
                _pay_sealed_ladders(ms)
            except Exception:
                pass
            _pm = [per[m["id"]] for m in ms]
            try:
                boards[str(tid)] = apply_adjustments(
                    aggregate_standings(_pm), _tourney_adjustments(tid))
            except Exception:
                boards[str(tid)] = aggregate_standings(_pm)
            for m in ms:
                try:
                    supabase.table("bngc_ranked_tourney_results").upsert({
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
        trows = supabase.table("bngc_ranked_tourneys").select(
            "id,points_table").eq("id", int(tid)).limit(1).execute()
        if not trows.data:
            return []
        mres = supabase.table("bngc_ranked_tourney_matches").select(
            "id,tourney_id,version,room_id,status,winner_discord_id,placements,removed_discord_ids,point_awards").eq(
            "tourney_id", int(tid)).in_("status", ["sealed", "manual"]).order(
            "version").limit(200).execute()
        matches = mres.data or []
        if not matches:
            return []
        mids = [m["id"] for m in matches]
        eres = supabase.table("bngc_ranked_tourney_events").select(
            "match_id,etype,discord_id,actor_name,alive").in_(
            "match_id", mids[:200]).limit(5000).execute()
        by_mid: dict = {}
        for e in (eres.data or []):
            by_mid.setdefault((e or {}).get("match_id"), []).append(e)
        boards = _calculate_sync({int(tid): trows.data[0]}, matches, by_mid, computed_by)
        return boards.get(str(int(tid)), [])
    except Exception:
        return []


@router.post("/api/ranked/calculate")
async def comp_calculate(request: Request):
    """Compute standings with the shared math and snapshot per-match rows
    into bngc_ranked_tourney_results (dashboard Calculate button). Accepts
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
            mres = supabase.table("bngc_ranked_tourney_matches").select(
                "id,tourney_id,version,room_id,status,winner_discord_id,placements,removed_discord_ids,point_awards").in_("id", mids).execute()
            matches = [m for m in (mres.data or []) if (m or {}).get("status") in ("sealed", "manual")]
            if not matches:
                return JSONResponse({"error": "match not found or not sealed"}, status_code=404)
            tmap = {m["id"]: m for m in matches}
            trows = supabase.table("bngc_ranked_tourneys").select(
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
            trows = supabase.table("bngc_ranked_tourneys").select(
                "id,points_table").eq("id", tid).limit(1).execute()
            if not trows.data:
                return JSONResponse({"error": "tourney not found"}, status_code=404)
            trous = {tid: trows.data[0]}
            mres = supabase.table("bngc_ranked_tourney_matches").select(
                "id,tourney_id,version,room_id,status,winner_discord_id,placements,removed_discord_ids,point_awards").eq(
                "tourney_id", tid).in_("status", ["sealed", "manual"]).order("version").limit(200).execute()
            matches = mres.data or []
            mids = [m["id"] for m in matches]
            tmap = {m["id"]: m for m in matches}
        by_mid: dict = {}
        if mids:
            eres = supabase.table("bngc_ranked_tourney_events").select(
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


def _apply_ranked_pr_ratchet(pr_board) -> list:
    """Write bngc_users.ranked_tourney_pr as a high-water mark. Never raises.

    Mirrors comp._apply_tourney_pr_ratchet: one global column, a per-tourney
    score, therefore only ever up. The per-tourney history row still records
    what this tourney actually scored.
    """
    wrote: list = []
    try:
        rows = list(pr_board or [])
        if not rows or not supabase:
            return wrote
        ids = [str((r or {}).get("discord_id") or "") for r in rows]
        cur: dict = {}
        try:
            res = supabase.table("bngc_users").select(
                "discord_id,ranked_tourney_pr").in_("discord_id", ids).execute()
            for u in (res.data or []):
                did = str((u or {}).get("discord_id") or "")
                if not did:
                    continue
                try:
                    cur[did] = float((u or {}).get("ranked_tourney_pr"))
                except Exception:
                    cur[did] = None
        except Exception:
            cur = {}
        for r in rows:
            did = str((r or {}).get("discord_id") or "")
            if not did:
                continue
            try:
                want = float((r or {}).get("pr"))
            except Exception:
                continue
            old = cur.get(did)
            if old is not None:
                try:
                    want = max(want, float(old))
                except Exception:
                    pass
            try:
                supabase.table("bngc_users").update(
                    {"ranked_tourney_pr": want}).eq("discord_id", did).execute()
                wrote.append(did)
            except Exception:
                continue
    except Exception:
        pass
    return wrote


def compute_and_store_tourney_pr_sync(tid: int) -> list[dict]:
    """Compute Tourney PR once at tourney end + store. Idempotent: when
    bngc_ranked_tourney_pr_history rows exist for the tourney they are returned as-is
    (no recompute). Writes bngc_users.tourney_pr per player. Sync. Never
    raises; returns the PR board (possibly empty)."""
    try:
        if not supabase:
            return []
        try:
            try:
                existing = supabase.table("bngc_ranked_tourney_pr_history").select(
                    "tourney_id,discord_id,pr,kills,wins,matches,avg_placement,players,manual"
                ).eq("tourney_id", int(tid)).limit(500).execute()
            except Exception:
                existing = supabase.table("bngc_ranked_tourney_pr_history").select(
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
        trows = supabase.table("bngc_ranked_tourneys").select(
            "id,points_table").eq("id", int(tid)).limit(1).execute()
        if not trows.data:
            return []
        trow = trows.data[0]
        mres = supabase.table("bngc_ranked_tourney_matches").select(
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
                # ORDER BY id: an unordered LIMIT can hand back a different
                # 5000-row window on every call once a tourney is big, which
                # drops players off the board and moves the shared `players`
                # divisor. A recompute that persists a rating has to be
                # reproducible.
                eres = supabase.table("bngc_ranked_tourney_events").select(
                    "id,match_id,etype,discord_id,actor_name,alive").in_(
                    "match_id", mids[:200]).order("id").limit(5000).execute()
                for e in (eres.data or []):
                    by_mid.setdefault((e or {}).get("match_id"), []).append(e)
        except Exception:
            pass
        per_match, board = ranked_standings_for_matches(
            trow, _attach_frozen_tables(sealed), by_mid)
        if not board:
            return []
        pr_board = compute_tourney_pr(board, per_match)
        for r in pr_board:
            try:
                supabase.table("bngc_ranked_tourney_pr_history").upsert({
                    "tourney_id": int(tid), "discord_id": str(r["discord_id"]),
                    "pr": float(r["pr"]), "kills": int(r["kills"]), "wins": int(r["wins"]),
                    "matches": int(r["matches"]),
                    "avg_placement": r["avg_placement"], "players": float(r["players"]),
                }, on_conflict="tourney_id,discord_id").execute()
            except Exception:
                pass
        # Same high-water-mark rule as comp PR (see
        # comp._apply_tourney_pr_ratchet): compute_tourney_pr is a
        # per-TOURNEY score with no memory of the player's previous rating, so
        # writing it absolutely to the single `ranked_tourney_pr` column let the
        # last tourney to close decide the rating and players went DOWN.
        _apply_ranked_pr_ratchet(pr_board)
        return pr_board
    except Exception:
        return []


@router.post("/api/ranked/tourney/pr")
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


@router.post("/api/ranked/tourney/pr-override")
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
            hres = supabase.table("bngc_ranked_tourney_pr_history").select(
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
            supabase.table("bngc_ranked_tourney_pr_history").upsert(
                row, on_conflict="tourney_id,discord_id").execute()
        except Exception:
            row.pop("manual", None)
            supabase.table("bngc_ranked_tourney_pr_history").upsert(
                row, on_conflict="tourney_id,discord_id").execute()
        if pr is not None:
            try:
                supabase.table("bngc_users").update(
                    {"ranked_tourney_pr": float(pr)}).eq("discord_id", did).execute()
            except Exception:
                pass
        return JSONResponse({"ok": True, "tourney_id": tid, "discord_id": did, "pr": pr})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/tourney/pr-reset")
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
        res = supabase.table("bngc_ranked_tourney_pr_history").delete().eq(
            "tourney_id", int(tid)).execute()
        return JSONResponse({"ok": True, "tourney_id": tid, "deleted": len(res.data or [])})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/ranked/regs")
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
            q = supabase.table("bngc_ranked_tourney_registrations").select(
                "id,team_label,member_ids,member_names,captain_id,status,created_at").eq("tourney_id", tid)
            if want:
                q = q.eq("status", want)
            res = q.order("created_at").limit(500).execute()
        except Exception:
            q = supabase.table("bngc_ranked_tourney_registrations").select(
                "id,team_label,member_ids,captain_id,status,created_at").eq("tourney_id", tid)
            if want:
                q = q.eq("status", want)
            res = q.order("created_at").limit(500).execute()
        return JSONResponse({"count": len(res.data or []), "regs": res.data or []})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/ranked/names")
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


@router.post("/api/ranked/reg/status")
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
        res = supabase.table("bngc_ranked_tourney_registrations").update(
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


@router.post("/api/ranked/match/kills")
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
        mres = supabase.table("bngc_ranked_tourney_matches").select("id").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        if delta > 0:
            name = _comp_display_name(did)
            supabase.table("bngc_ranked_tourney_events").insert([{
                "match_id": mid, "etype": "kill", "discord_id": did,
                "actor_name": name, "target_name": "manual", "manual": True,
            }] * delta).execute()
        else:
            eres = supabase.table("bngc_ranked_tourney_events").select("id").eq(
                "match_id", mid).eq("discord_id", did).eq("etype", "kill").eq(
                "manual", True).order("id", desc=True).limit(abs(delta)).execute()
            removed = 0
            for r in (eres.data or []):
                try:
                    supabase.table("bngc_ranked_tourney_events").delete().eq("id", r["id"]).execute()
                    removed += 1
                except Exception:
                    pass
        cres = supabase.table("bngc_ranked_tourney_events").select("id,manual").eq(
            "match_id", mid).eq("discord_id", did).eq("etype", "kill").execute()
        rows = cres.data or []
        tracked = sum(1 for r in rows if not (r or {}).get("manual"))
        return JSONResponse({"ok": True, "match_id": mid, "discord_id": did,
                             "kills": len(rows), "tracked_kept": tracked,
                             "requested": abs(delta) if delta < 0 else delta,
                             "removed_manual": removed if delta < 0 else delta})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/match/elim")
async def comp_match_elim(request: Request):
    """Hand-adjust a player's eliminations in a match (dashboard elim buttons).

    Positive delta inserts manual alv (alive=False) rows -they join the
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
        mres = supabase.table("bngc_ranked_tourney_matches").select("id").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        if delta > 0:
            name = _comp_display_name(did)
            supabase.table("bngc_ranked_tourney_events").insert([{
                "match_id": mid, "etype": "alv", "discord_id": did,
                "actor_name": name, "target_name": None,
                "alive": False, "manual": True,
            }] * delta).execute()
        else:
            eres = supabase.table("bngc_ranked_tourney_events").select("id").eq(
                "match_id", mid).eq("discord_id", did).eq("etype", "alv").eq(
                "manual", True).order("id", desc=True).limit(abs(delta)).execute()
            removed = 0
            for r in (eres.data or []):
                try:
                    supabase.table("bngc_ranked_tourney_events").delete().eq("id", r["id"]).execute()
                    removed += 1
                except Exception:
                    pass
        cres = supabase.table("bngc_ranked_tourney_events").select("id,manual,alive").eq(
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


@router.post("/api/ranked/match/place")
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
        mres = supabase.table("bngc_ranked_tourney_matches").select(
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
        supabase.table("bngc_ranked_tourney_matches").update({"placements": cur}).eq("id", mid).execute()
        return JSONResponse({"ok": True, "match_id": mid, "placements": cur})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _removed_list(m: dict) -> list:
    try:
        return [str(x) for x in ((m or {}).get("removed_discord_ids") or []) if str(x or "").strip()]
    except Exception:
        return []


IGNORED_NAME_PREFIX = "name:"


def _norm_ignored_name(s: str) -> str:
    """Canonical key for an ignored actor name (same fold as _norm_nm)."""
    try:
        import re as _re
        nm = _re.sub(r"<[^>]*>", "", str(s or "")).strip().lower()
        return nm[3:].strip() if nm.startswith("dc:") else nm
    except Exception:
        return str(s or "").lower()


def _ignored_names(m: dict) -> set:
    """Actor names this match ignores (intruders): `name:<norm>` entries."""
    try:
        return {str(x)[len(IGNORED_NAME_PREFIX):]
                for x in _removed_list(m)
                if str(x).startswith(IGNORED_NAME_PREFIX)
                and len(str(x)) > len(IGNORED_NAME_PREFIX)}
    except Exception:
        return set()


@router.post("/api/ranked/match/remove")
async def comp_match_remove(request: Request):
    """Remove a player from a match roster (left mid-match) or ignore an
    unmapped intruder name for this match.

    Body: {match_id, discord_id?, actor_name?}. discord_id preferred; when
    only actor_name is given the server resolves it via regs/game_username
    (best effort). An actor_name that resolves to nobody is stored as an
    ignored name (`name:<norm>`) instead of erroring: ignored names never
    map, place, or score in this match (dashboard Ignore button).
    Idempotent: appends to removed_discord_ids. Never raises beyond
    JSON errors."""
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
            mres = supabase.table("bngc_ranked_tourney_matches").select(
                "id,tourney_id,removed_discord_ids").eq("id", mid).limit(1).execute()
        except Exception:
            return JSONResponse({"error": "removed_discord_ids column missing -apply add_comp_v237.sql"}, status_code=500)
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        m = mres.data[0] or {}
        if not did and aname:
            # best-effort resolve: tourney regs + game_username match
            try:
                rres = supabase.table("bngc_ranked_tourney_registrations").select(
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
            if not aname:
                return JSONResponse({"error": "discord_id or actor_name required"},
                                    status_code=400)
            # intruder with no linkable identity: ignore the raw name for
            # this match instead of erroring (dashboard Ignore button).
            key = IGNORED_NAME_PREFIX + _norm_ignored_name(aname)
            if not key[len(IGNORED_NAME_PREFIX):]:
                return JSONResponse({"error": "actor_name required"}, status_code=400)
            cur = _removed_list(m)
            if key not in cur:
                cur.append(key)
                supabase.table("bngc_ranked_tourney_matches").update(
                    {"removed_discord_ids": cur}).eq("id", mid).execute()
            return JSONResponse({"ok": True, "match_id": mid, "ignored": aname,
                                 "removed_discord_ids": cur})
        cur = _removed_list(m)
        if did not in cur:
            cur.append(did)
            supabase.table("bngc_ranked_tourney_matches").update(
                {"removed_discord_ids": cur}).eq("id", mid).execute()
        return JSONResponse({"ok": True, "match_id": mid, "removed_discord_ids": cur})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/match/add-back")
async def comp_match_add_back(request: Request):
    """Add a removed player back to a match roster (/tourney add-back).

    Body: {match_id, discord_id?, actor_name?}. Removes the did (or the
    ignored `name:<norm>` entry for actor_name) from removed_discord_ids
    so the player scores again. Never raises beyond JSON errors."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        mid = int(b.get("match_id"))
        did = str(b.get("discord_id") or "").strip()
        aname = str(b.get("actor_name") or "").strip()
    except Exception:
        return JSONResponse({"error": "match_id and discord_id required"}, status_code=400)
    if not did and not aname:
        return JSONResponse({"error": "discord_id or actor_name required"}, status_code=400)
    try:
        try:
            mres = supabase.table("bngc_ranked_tourney_matches").select(
                "id,removed_discord_ids").eq("id", mid).limit(1).execute()
        except Exception:
            return JSONResponse({"error": "removed_discord_ids column missing -apply add_comp_v237.sql"}, status_code=500)
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        drop = {did} if did else set()
        if aname:
            drop.add(IGNORED_NAME_PREFIX + _norm_ignored_name(aname))
        cur = [x for x in _removed_list(mres.data[0]) if x not in drop]
        supabase.table("bngc_ranked_tourney_matches").update(
            {"removed_discord_ids": cur}).eq("id", mid).execute()
        return JSONResponse({"ok": True, "match_id": mid, "removed_discord_ids": cur})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/match/add-player")
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
            mres = supabase.table("bngc_ranked_tourney_matches").select(
                "id,removed_discord_ids").eq("id", mid).limit(1).execute()
        except Exception:
            mres = supabase.table("bngc_ranked_tourney_matches").select(
                "id").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        eres = supabase.table("bngc_ranked_tourney_events").select("id").eq(
            "match_id", mid).eq("discord_id", did).limit(1).execute()
        if (eres.data or []):
            return JSONResponse({"ok": True, "match_id": mid,
                                 "discord_id": did, "present": True})
        name = _comp_display_name(did)
        supabase.table("bngc_ranked_tourney_events").insert({
            "match_id": mid, "etype": "alv", "discord_id": did,
            "actor_name": name, "target_name": None,
            "alive": True, "manual": True,
        }).execute()
        try:
            cur = [x for x in _removed_list((mres.data[0] or {})) if x != did]
            if len(cur) != len(_removed_list((mres.data[0] or {}))):
                supabase.table("bngc_ranked_tourney_matches").update(
                    {"removed_discord_ids": cur}).eq("id", mid).execute()
        except Exception:
            pass
        return JSONResponse({"ok": True, "match_id": mid,
                             "discord_id": did, "present": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/match/points")
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
            mres = supabase.table("bngc_ranked_tourney_matches").select(
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
        supabase.table("bngc_ranked_tourney_matches").update({"point_awards": cur}).eq("id", mid).execute()
        return JSONResponse({"ok": True, "match_id": mid, "point_awards": cur})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/match/reset")
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
        mres = supabase.table("bngc_ranked_tourney_matches").select(
            "id,placements,point_awards").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        m = mres.data[0] or {}
        cur_p = dict(m.get("placements") or {})
        cur_a = dict(m.get("point_awards") or {})
        if did:
            cur_p.pop(did, None)
            cur_a.pop(did, None)
            q = supabase.table("bngc_ranked_tourney_events").delete().eq("match_id", mid).eq("discord_id", did)
            if not hard:
                q = q.eq("manual", True)
            try:
                q.execute()
            except Exception:
                # pre-migration manual column: delete scoped rows only when hard
                if hard:
                    supabase.table("bngc_ranked_tourney_events").delete().eq(
                        "match_id", mid).eq("discord_id", did).execute()
        else:
            cur_p, cur_a = {}, {}
            try:
                if hard:
                    supabase.table("bngc_ranked_tourney_events").delete().eq("match_id", mid).execute()
                else:
                    supabase.table("bngc_ranked_tourney_events").delete().eq(
                        "match_id", mid).eq("manual", True).execute()
            except Exception:
                pass
        supabase.table("bngc_ranked_tourney_matches").update(
            {"placements": cur_p, "point_awards": cur_a}).eq("id", mid).execute()
        return JSONResponse({"ok": True, "match_id": mid, "placements": cur_p, "point_awards": cur_a})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/reset")
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
        q = supabase.table("bngc_ranked_tourney_adjustments").delete().eq("tourney_id", int(tid))
        if did:
            q = q.eq("discord_id", did)
        res = q.execute()
        return JSONResponse({"ok": True, "tourney_id": tid, "deleted": len(res.data or [])})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/match/delete")
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
        mres = supabase.table("bngc_ranked_tourney_matches").select(
            "id,tourney_id,version").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        m = mres.data[0] or {}
        try:
            supabase.table("bngc_ranked_tourney_events").delete().eq("match_id", mid).execute()
        except Exception:
            pass
        try:
            supabase.table("bngc_ranked_tourney_results").delete().eq("match_id", mid).execute()
        except Exception:
            pass
        supabase.table("bngc_ranked_tourney_matches").delete().eq("id", mid).execute()
        # Retire the version so the bridge treats the slot as consumed and never
        # re-issues the number (which re-created the match on the next sweep).
        # Best effort: pre-migration column, the delete still stands.
        try:
            ver = int(m.get("version"))
        except Exception:
            ver = None
        if ver is not None and m.get("tourney_id") is not None:
            try:
                tres = supabase.table("bngc_ranked_tourneys").select(
                    "deleted_versions").eq("id", m.get("tourney_id")).limit(1).execute()
                trow = ((tres.data or [{}])[0] or {})
                cur = [int(x) for x in (trow.get("deleted_versions") or []) if x is not None]
                if ver not in cur:
                    supabase.table("bngc_ranked_tourneys").update(
                        {"deleted_versions": sorted(cur + [ver])}).eq(
                        "id", m.get("tourney_id")).execute()
            except Exception:
                pass
        return JSONResponse({"ok": True, "match_id": mid, "tourney_id": m.get("tourney_id"),
                             "version": m.get("version")})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/ranked/adjust")
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
        tres = supabase.table("bngc_ranked_tourneys").select("id").eq("id", tid).limit(1).execute()
        if not tres.data:
            return JSONResponse({"error": "tourney not found"}, status_code=404)
        row = {"tourney_id": tid, "discord_id": did, "delta_points": delta,
               "reason": reason, "created_by": by}
        if mid is not None:
            row["match_id"] = mid
        supabase.table("bngc_ranked_tourney_adjustments").insert(row).execute()
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


@router.get("/api/ranked/adjustments")
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
        res = supabase.table("bngc_ranked_tourney_adjustments").select(
            "id,match_id,discord_id,delta_points,reason,created_by,created_at").eq(
            "tourney_id", tid).order("created_at", desc=True).limit(200).execute()
        return JSONResponse({"tourney_id": tid, "adjustments": res.data or []})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/match/tie")
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
        mres = supabase.table("bngc_ranked_tourney_matches").select(
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
        supabase.table("bngc_ranked_tourney_matches").update(patch).eq("id", mid).execute()
        return JSONResponse({"ok": True, "match_id": mid, "placements": cur,
                             "tied": dids, "winner_discord_id": patch.get("winner_discord_id", w)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/ranked/match/unmapped")
async def comp_match_unmapped(request: Request):
    """Actor names in a match with no discord link (dashboard link UI).

    Returns {unmapped: [{name, kills, deaths}], ignored: [name]} -ignored
    intruder names are listed separately so the dashboard can un-ignore
    them. Ignored names never appear in unmapped.
    """
    err = _need()
    if err:
        return err
    try:
        mid = int(request.query_params.get("match_id"))
    except Exception:
        return JSONResponse({"error": "match_id required"}, status_code=400)
    try:
        try:
            mres = supabase.table("bngc_ranked_tourney_matches").select(
                "removed_discord_ids").eq("id", mid).limit(1).execute()
            ignored = _ignored_names((mres.data or [{}])[0]) if mres.data else set()
        except Exception:
            ignored = set()
        eres = supabase.table("bngc_ranked_tourney_events").select(
            "etype,discord_id,actor_name,target_name,alive").eq("match_id", mid).limit(5000).execute()
        agg: dict[str, dict] = {}
        seen_ignored: set = set()
        for e in (eres.data or []):
            try:
                if (e or {}).get("discord_id"):
                    continue
                for nm_raw in ((e or {}).get("actor_name"), (e or {}).get("target_name")):
                    nm = str(nm_raw or "").strip()
                    if not nm:
                        continue
                    if _norm_ignored_name(nm) in ignored:
                        seen_ignored.add(nm)
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
                             "unmapped": sorted(agg.values(), key=lambda r: -r["kills"]),
                             "ignored": sorted(seen_ignored)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/ranked/match/link")
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
        if did:
            try:
                mchk = supabase.table("bngc_ranked_tourney_matches").select(
                    "removed_discord_ids").eq("id", mid).limit(1).execute()
                if mchk.data and _norm_ignored_name(name) in _ignored_names(mchk.data[0]):
                    return JSONResponse(
                        {"error": "name is ignored for this match -un-ignore it first"},
                        status_code=400)
            except Exception:
                pass
        q = supabase.table("bngc_ranked_tourney_events").update(
            {"discord_id": did}).eq("match_id", mid).is_("discord_id", "null").eq(
            "actor_name", name)
        res = q.execute()
        n = len(res.data or [])
        n_all = 0
        if apply_all and did:
            try:
                mrow = supabase.table("bngc_ranked_tourney_matches").select(
                    "tourney_id").eq("id", mid).limit(1).execute()
                tid = ((mrow.data or [{}])[0] or {}).get("tourney_id")
                if tid is not None:
                    mids = supabase.table("bngc_ranked_tourney_matches").select(
                        "id").eq("tourney_id", tid).limit(200).execute()
                    ids = [m.get("id") for m in (mids.data or []) if m.get("id") != mid]
                    for oid in ids[:200]:
                        try:
                            r2 = supabase.table("bngc_ranked_tourney_events").update(
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


@router.get("/api/ranked/tourney/by-code")
async def comp_tourney_by_code(request: Request):
    """Tourney carrying this room code (userscript join detection + bridge).

    Returns the single non-ended tourney for the code, else {tourney: null}.
    Ambiguous (2+ open rows, same code) resolves to null -never guess.
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
        res = await db_io(lambda: supabase.table("bngc_ranked_tourneys").select(
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
        # Join-detection polls this every few seconds -a pool blip must
        # resolve to null (retry on next poll), never a 500 that the
        # userscript treats as fatal.
        if is_transient_error(e):
            try:
                log_err(f"COMP by-code={code} transient ({str(e)[:60]}) → null")
            except Exception:
                pass
            return JSONResponse({"tourney": None})
        return _db_err(e, f"by-code={code}")
