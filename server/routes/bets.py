"""
bets.py — GET/POST /api/coins/bets/* — parimutuel match betting.

  GET  /api/coins/bets/open?limit=25
  POST /api/coins/bets             {discord_id, match_key, pick_discord_id, amount}
  GET  /api/coins/bets/mine?discord_id=xxx
  GET  /api/coins/bets/pool?match_key=xxx
  POST /api/coins/bets/settle      {}

Bettable: comp tourney open matches, ranked-tourney open matches, ranked
room open matches, and any live public/private room (live:{room_id}:{ver},
results read from the capture evidence already banked per room: a banked
win, first to 10 kills, or last player standing; death/leave eliminates).
Room-less lobby-bucket matches surface per session (live:sess_<id>:<ver>,
side bets only, picks need tracked rows). Stake is held at bet time (bet_placed). A lazy settle pass runs inside every endpoint: sealed matches split (pool - 5% rake)
proportionally among winner-picks (bet_won, profit counts as earned);
winnerless seals refund every stake (bet_refund). No seal-path changes,
idempotent, never raises to the client.
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase, db_io
from ..lib.logger import log_sys
from ..lib import bets as bets_lib
from ..lib import coins as coins_lib
from ..lib import names as names_lib
from .coins import _apply_delta_sync, _missing_table, _MISSING_HINT

router = APIRouter()

_OPEN_LIMIT = 25

_LIVE_TYPES = ("kill", "knock", "death", "win", "leave")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _names_sync(ids: list[str]) -> dict[str, str]:
    """discord_id -> display name (game name first). Never raises."""
    try:
        ids = [str(x) for x in (ids or []) if str(x or "").strip()]
    except Exception:
        return {}
    if not ids:
        return {}
    try:
        res = supabase.table("bngc_users").select(
            "discord_id,game_username,discord_username").in_(
            "discord_id", ids[:200]).execute()
        out = {}
        for r in (res.data or []):
            try:
                did = str(r.get("discord_id"))
                out[did] = str(r.get("game_username") or
                               r.get("discord_username") or did[:6])
            except Exception:
                continue
        return out
    except Exception:
        return {}


def _accepted_members_sync(table: str, tourney_id: int) -> list[str]:
    """Flattened accepted reg member_ids. Never raises."""
    try:
        res = supabase.table(table).select("member_ids").eq(
            "tourney_id", int(tourney_id)).eq("status", "accepted").execute()
        ids: list[str] = []
        for r in (res.data or []):
            try:
                for m in ((r or {}).get("member_ids") or []):
                    if m and str(m) not in ids:
                        ids.append(str(m))
            except Exception:
                continue
        return ids
    except Exception:
        return []


def _accepted_regs_sync(table: str, tourney_id: int) -> list[dict]:
    try:
        res = supabase.table(table).select("member_ids").eq(
            "tourney_id", int(tourney_id)).eq("status", "accepted").execute()
        return list(res.data or [])
    except Exception:
        return []


def _ts_of(row) -> float:
    """created_at -> epoch seconds. 0 when unparseable. Never raises."""
    try:
        v = (row or {}).get("created_at")
        if not v:
            return 0.0
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _scan_evidence_sync(limit: int = bets_lib.EVIDENCE_ROW_LIMIT) -> list[dict]:
    """Recent typed evidence rows (kill/knock/death/win/leave), newest first."""
    try:
        res = supabase.table("bngc_game_events").select(
            "discord_id,session_id,event_type,event_data,created_at").in_(
            "event_type", list(_LIVE_TYPES)).order(
            "created_at", desc=True).limit(int(limit)).execute()
        return list(res.data or [])
    except Exception:
        return []


def _wager_profit_today_sync(discord_id: str) -> int:
    """Wager profit credited today (bet_won + duel_win ledger meta).

    Reads recent ledger rows (no gte op — postgrest version drift), filters
    to today in Python. Legacy rows without a meta.earned stamp count gross
    delta for duel_win (today only) and are skipped for bet_won. Never raises.
    """
    try:
        midnight = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        res = supabase.table("bngc_coin_ledger").select(
            "delta,reason,meta,created_at").eq(
            "discord_id", str(discord_id)).in_(
            "reason", list(coins_lib.WAGER_PROFIT_REASONS)).order(
            "created_at", desc=True).limit(500).execute()
        total = 0
        for r in ((res.data if hasattr(res, "data") else []) or []):
            try:
                ts = _ts_of(r)
                if ts <= 0:
                    continue
                if datetime.fromtimestamp(ts, tz=timezone.utc) < midnight:
                    break  # desc order: older rows only age
                m = (r or {}).get("meta") or {}
                if "earned" in m:
                    total += int(m.get("earned") or 0)
                elif str((r or {}).get("reason")) == "duel_win":
                    total += int((r or {}).get("delta") or 0)
            except Exception:
                continue
        return max(0, total)
    except Exception:
        return 0


# Killfeed name keys for session-match cross-verification.
_SESS_FEED_KEYS = ("instigator", "instigated", "victim", "killer",
                   "player", "player_name")


def _resolve_names_sync(names: list[str]) -> dict[str, str]:
    """game_username -> discord_id for registered users. Never raises.

    Exact match first, then the shared conservative resolver
    (server.lib.names): normalized match covers case/tag/`dc:` drift, and one
    unique confident fuzzy hit covers a player who re-set their username to a
    near-miss - usually the one mobile player in a room, whose only evidence
    is the host's killfeed. Near-ties, weak matches and twins stay unmapped,
    so a wager never pays against a guessed player; those bets refund as they
    did before. Unmapped names are absent from the result. A candidate read
    only happens when a name is still unresolved.
    """
    try:
        clean = sorted({str(n or "").strip() for n in (names or [])
                        if str(n or "").strip()})[:200]
        if not clean or not supabase:
            return {}
        res = supabase.table("bngc_users").select(
            "discord_id,game_username").in_("game_username", clean).execute()
        out: dict[str, str] = {}
        owners: dict[str, set[str]] = {}
        for r in ((res.data if hasattr(res, "data") else []) or []):
            try:
                g, d = r.get("game_username"), r.get("discord_id")
                if g and d and str(g) in clean:
                    owners.setdefault(str(g), set()).add(str(d))
            except Exception:
                continue
        # a name owned by two accounts is not guessable - leave it to the
        # fuzzy tier, which is twin-guarded the same way
        pending = list(clean)
        for n, dids in owners.items():
            if len(dids) == 1:
                out[n] = next(iter(dids))
                pending.remove(n)
        if not pending:
            return out
        fres = supabase.table("bngc_users").select(
            "discord_id,game_username").limit(
                names_lib.MAX_CANDIDATES).execute()
        pool = names_lib.candidates_from_users(
            fres.data if hasattr(fres, "data") else [])
        for n in pending:
            did = names_lib.fuzzy_pick(n, pool)
            if did:
                out[n] = did
        return out
    except Exception:
        return {}


def _load_sess_match_sync(sid: str, version: int):
    """Bettable view for a room-less (lobby-bucket) live session match.

    Participants = row-holding dids + registered users resolved from
    killfeed names. Side bets (kills/out) only — win pools are blocked at
    placement since unattributed players can never win one. Returns the
    same view shape as _load_match_sync plus _row_holders. Never raises.
    """
    try:
        ev = _room_evidence_sync(None, int(version), session_id=sid)
        holders = sorted(ev.get("participants") or [])
        if not holders:
            return None
        # Killfeed feed for this session+version (outside the typed scan):
        # names for the roster, killed-counts folded into kills.
        feed_names: set[str] = set()
        feed_kills: dict[str, int] = {}
        try:
            kf = supabase.table("bngc_game_events").select(
                "event_data").eq("session_id", str(sid)).eq(
                "event_type", "killfeed").order(
                "created_at", desc=True).limit(500).execute()
            for r in ((kf.data if hasattr(kf, "data") else []) or []):
                try:
                    gd = (r or {}).get("event_data") or {}
                    if str(gd.get("room_id") or "") != "lobby":
                        continue
                    try:
                        if int(gd.get("match_version")) != int(version):
                            continue
                    except Exception:
                        continue
                    for _nk in _SESS_FEED_KEYS:
                        _nm = str(gd.get(_nk) or "").strip()
                        if _nm:
                            feed_names.add(_nm)
                    if str(gd.get("kill_type") or "") == "killed":
                        _ig = str(gd.get("instigator") or "").strip()
                        if _ig:
                            feed_kills[_ig] = feed_kills.get(_ig, 0) + 1
                except Exception:
                    continue
        except Exception:
            pass
        mapped = _resolve_names_sync(sorted(feed_names))
        # Fold mapped kill counts into kills so side lines work for them
        # (single capturer's feed: no double count).
        kills = dict(ev.get("kills") or {})
        try:
            for _nm, _n in feed_kills.items():
                _did = mapped.get(str(_nm))
                if _did and _did not in kills:
                    kills[_did] = int(_n or 0)
        except Exception:
            pass
        parts = sorted(set(holders) | set(mapped.values()))
        # Solo tracked grinders list too (side bets on the holder work;
        # win pools are blocked at placement so no pool can form).
        if not parts:
            return None
        res = bets_lib.live_result(kills, ev.get("dead"), ev.get("left"),
                                   ev.get("win"), set(parts))
        if res["status"] == "won":
            status, winner = "sealed", res["winner"]
        elif _live_closed_sync(ev):
            status, winner = "sealed", None
        else:
            status, winner = "open", None
        names = _names_sync(parts)
        try:
            hero = names.get(holders[0], sid[:8])
        except Exception:
            hero = sid[:8]
        label = f"{hero}'s live match"
        if ev.get("gm"):
            label += f" ({ev['gm']})"
        return {"key": bets_lib.make_key("live", f"sess-{sid}", int(version)),
                "kind": "live", "label": label, "status": status,
                "winner": winner, "participants": parts, "names": names,
                "latest": ev.get("latest"), "_regs_table": None,
                "_row_holders": holders, "_sess": True,
                "evidence": {"kills": kills,
                             "dead": sorted(ev.get("dead") or []),
                             "left": sorted(ev.get("left") or []),
                             "kill_target": bets_lib.LIVE_KILL_TARGET}}
    except Exception:
        return None


def _room_key_of(row) -> tuple[str, int] | None:
    """Grouping key for live discovery. Lobby-bucket rows (joins predate
    engine memory — the common pub case) group per session instead, so a
    room-less live match still surfaces. Never raises."""
    try:
        ed = (row or {}).get("event_data") or {}
        rid = str(ed.get("room_id") or "").strip()
        try:
            ver = int(ed.get("match_version"))
        except Exception:
            return None
        if not rid or ver < 0:
            return None
        if rid == "lobby":
            sid = str((row or {}).get("session_id") or "").strip()
            if not sid or ":" in sid:
                return None
            return f"sess-{sid}", ver
        return rid, ver
    except Exception:
        return None


def _room_evidence_sync(room_id: str, version: int,
                        rows: list[dict] | None = None,
                        session_id: str | None = None) -> dict:
    """Evidence for one live room+version. Never raises.

    kills counts `kill` rows only (self-credited per session: no
    double-counting when several companions share a room). dead/left/win
    are booleans per player (dupe-proof). newer_version True when a newer
    version of the same room has rows (the match moved on).

    session_id set: lobby-bucket rows of that session only (room-less live
    matches). Never raises.
    """
    try:
        version = int(version)
    except Exception:
        version = -1
    rid = str(room_id or "")
    _sess = str(session_id or "").strip() or None
    if rows is None:
        rows = _scan_evidence_sync()
    kills: dict[str, int] = {}
    dead: set[str] = set()
    left: set[str] = set()
    win: str | None = None
    parts: set[str] = set()
    gm, code, latest = None, None, 0.0
    newer = False
    try:
        for r in (rows or []):
            try:
                if _sess:
                    # Session-scoped: lobby rows of this session at this version.
                    if str((r or {}).get("session_id") or "") != _sess:
                        continue
                    ed0 = (r or {}).get("event_data") or {}
                    if str(ed0.get("room_id") or "") != "lobby":
                        continue
                    try:
                        _rv = int(ed0.get("match_version"))
                    except Exception:
                        continue
                    if _rv != version:
                        if _rv > version:
                            newer = True
                        continue
                else:
                    k = _room_key_of(r)
                    if not k or k[0] != rid:
                        continue
                    if k[1] != version:
                        if k[1] > version:
                            newer = True
                        continue
                ed = (r or {}).get("event_data") or {}
                ts = _ts_of(r)
                if ts > latest:
                    latest = ts
                if not gm and ed.get("gm"):
                    gm = str(ed.get("gm"))
                if not code and (ed.get("short_code") or ed.get("code")):
                    code = str(ed.get("short_code") or ed.get("code"))
                did = str(r.get("discord_id") or "").strip()
                et = str(r.get("event_type") or "")
                if did:
                    parts.add(did)
                if et == "kill" and did:
                    kills[did] = kills.get(did, 0) + 1
                elif et == "death" and did:
                    dead.add(did)
                elif et == "leave" and did:
                    left.add(did)
                elif et == "win" and did and not win:
                    win = did
            except Exception:
                continue
    except Exception:
        pass
    return {"kills": kills, "dead": dead, "left": left, "win": win,
            "participants": parts, "gm": gm, "code": code,
            "latest": latest, "newer_version": newer}


def _live_closed_sync(evidence: dict) -> bool:
    """True when the room moved on or went quiet (settle what is there)."""
    try:
        if evidence.get("newer_version"):
            return True
        latest = float(evidence.get("latest") or 0)
        return latest <= 0 or (_time.time() - latest) > bets_lib.STALE_S
    except Exception:
        return True


def _load_match_sync(kind: str, ref, version: int) -> dict | None:
    """Bettable-match view or None (missing/closed). Never raises.

    Returns {key, kind, label, status, winner, participants:[ids],
    names:{id: name}}. status open|sealed (+ anything else -> closed).
    """
    try:
        if kind == "comp":
            m = supabase.table("bngc_comp_matches").select(
                "id,version,room_id,status,winner_discord_id").eq(
                "tourney_id", int(ref)).eq("version", int(version)
                                           ).limit(1).execute()
            rows = m.data or []
            if not rows:
                return None
            mrow = rows[0]
            t = supabase.table("bngc_comp_tourneys").select(
                "name,status").eq("id", int(ref)).limit(1).execute()
            trow = (t.data or [{}])[0]
            if str(trow.get("status") or "") != "open":
                return None
            parts = _accepted_members_sync("bngc_comp_registrations", int(ref))
            label = f"{trow.get('name') or f'Comp #{ref}'} M{version}"
        elif kind == "rt":
            m = supabase.table("bngc_ranked_tourney_matches").select(
                "id,version,room_id,status,winner_discord_id").eq(
                "tourney_id", int(ref)).eq("version", int(version)
                                           ).limit(1).execute()
            rows = m.data or []
            if not rows:
                return None
            mrow = rows[0]
            t = supabase.table("bngc_ranked_tourneys").select(
                "name,status").eq("id", int(ref)).limit(1).execute()
            trow = (t.data or [{}])[0]
            if str(trow.get("status") or "") not in ("started", "scheduled"):
                return None
            parts = _accepted_members_sync(
                "bngc_ranked_tourney_registrations", int(ref))
            label = f"{trow.get('name') or f'Ranked #{ref}'} M{version}"
        elif kind == "room":
            m = supabase.table("bngc_ranked_matches").select(
                "room_id,match_version,status,winner_discord_id,room_code").eq(
                "room_id", str(ref)).eq("match_version", int(version)
                                        ).limit(1).execute()
            rows = m.data or []
            if not rows:
                return None
            mrow = rows[0]
            try:
                r = supabase.table("ranked_rooms").select(
                    "short_code,roster").eq("id", str(ref)).limit(1).execute()
                room = (r.data or [{}])[0]
            except Exception:
                room = {}
            try:
                parts = [str(x.get("discord_id"))
                         for x in (room.get("roster") or [])
                         if (x or {}).get("discord_id")]
            except Exception:
                parts = []
            code = (room.get("short_code") or mrow.get("room_code")
                    or str(ref)[:8])
            label = f"Room {code} M{version}"
        elif kind == "live":
            sref = str(ref or "").strip()
            if sref in ("", "lobby"):
                return None
            try:
                version = int(version)
            except Exception:
                return None
            if version < 0:
                return None
            if sref.startswith("sess-"):
                # Room-less lobby-bucket match, scoped to one session.
                return _load_sess_match_sync(sref[5:], version)
            ev = _room_evidence_sync(str(ref), version)
            parts = sorted(ev["participants"])
            res = bets_lib.live_result(ev["kills"], ev["dead"], ev["left"],
                                       ev["win"], ev["participants"])
            if res["status"] == "won":
                status, winner = "sealed", res["winner"]
            elif _live_closed_sync(ev):
                status, winner = "sealed", None
            else:
                status, winner = "open", None
            code = ev.get("code") or str(ref)[:8]
            label = f"Room {code} M{version}"
            if ev.get("gm"):
                label += f" ({ev['gm']})"
            names = _names_sync(parts)
            return {"key": bets_lib.make_key(kind, ref, version),
                    "kind": kind, "label": label, "status": status,
                    "winner": winner, "participants": parts, "names": names,
                    "latest": ev.get("latest"), "_regs_table": None,
                    "evidence": {"kills": ev["kills"],
                                 "dead": sorted(ev["dead"]),
                                 "left": sorted(ev["left"]),
                                 "kill_target": bets_lib.LIVE_KILL_TARGET}}
        else:
            return None
        status = str(mrow.get("status") or "open")
        names = _names_sync(parts)
        return {"key": bets_lib.make_key(kind, ref, int(version)),
                "kind": kind, "label": label, "status": status,
                "winner": str(mrow.get("winner_discord_id") or "") or None,
                "participants": parts, "names": names,
                "_regs_table": {"comp": "bngc_comp_registrations",
                                "rt": "bngc_ranked_tourney_registrations"}.get(kind)}
    except Exception:
        return None


def _winners_for_sync(view: dict) -> set[str]:
    """Winning discord_ids for a sealed view (team-mates included).

    Live rooms pay the single evidence winner (no reg teams out there).
    """
    try:
        if not view or not view.get("winner"):
            return set()
        if str(view.get("kind") or "") == "live":
            return {str(view.get("winner"))}
        if view.get("kind") in ("comp", "rt"):
            # tourney id is embedded in the key
            parsed = bets_lib.parse_key(view.get("key"))
            regs = _accepted_regs_sync(
                view.get("_regs_table"),
                int(parsed.get("tourney_id"))) if parsed else []
            return bets_lib.winners_with_teammates(view.get("winner"), regs)
        return {str(view.get("winner"))}
    except Exception:
        return set()


def _open_bets_by_match_sync() -> dict[str, list[dict]]:
    """All open bets grouped by match_key. Never raises."""
    try:
        res = supabase.table("bngc_match_bets").select(
            "id,match_key,bettor_discord_id,pick_discord_id,amount,"
            "bet_type,line,odds").eq(
            "status", "open").limit(500).execute()
        groups: dict[str, list[dict]] = {}
        for r in (res.data or []):
            try:
                groups.setdefault(str(r.get("match_key")), []).append({
                    "id": r.get("id"),
                    "bettor": str(r.get("bettor_discord_id")),
                    "pick": str(r.get("pick_discord_id")),
                    "amount": int(r.get("amount") or 0),
                    "type": str(r.get("bet_type") or "win"),
                    "line": r.get("line"),
                    "odds": r.get("odds")})
            except Exception:
                continue
        return groups
    except Exception:
        return {}


_FORM_CACHE: dict[str, tuple[float, dict]] = {}  # did -> (ts, form)
_FORM_TTL_S = 60.0


def _form_sync(did: str) -> dict:
    """Scoring form for odds pricing: {lam, win_rate, matches, provisional}.

    lam/win_rate come from the player's own ledger rows (kill + win
    types, distinct rooms = matches). 60s cache. Never raises.
    """
    try:
        did = str(did or "").strip()
    except Exception:
        return {"lam": bets_lib.PRIOR_LAM,
                "win_rate": bets_lib.PRIOR_WIN_RATE,
                "matches": 0, "provisional": True}
    if not did:
        return {"lam": bets_lib.PRIOR_LAM,
                "win_rate": bets_lib.PRIOR_WIN_RATE,
                "matches": 0, "provisional": True}
    try:
        hit = _FORM_CACHE.get(did)
        if hit and _time.time() < hit[0]:
            return hit[1]
    except Exception:
        pass
    form = {"lam": bets_lib.PRIOR_LAM, "win_rate": bets_lib.PRIOR_WIN_RATE,
            "matches": 0, "provisional": True}
    try:
        res = supabase.table("bngc_game_events").select(
            "event_type,event_data").eq("discord_id", did).in_(
            "event_type", ["kill", "win"]).limit(3000).execute()
        kills, wins, rooms = 0, 0, set()
        for r in (res.data or []):
            try:
                et = str((r or {}).get("event_type") or "")
                ed = (r or {}).get("event_data") or {}
                rooms.add((str(ed.get("room_id") or ""),
                           str(ed.get("match_version") or "")))
                if et == "kill":
                    kills += 1
                elif et == "win":
                    wins += 1
            except Exception:
                continue
        form = bets_lib.form_from_ledger(kills, wins, len(rooms))
        form["matches"] = len(rooms)
    except Exception:
        pass
    try:
        if len(_FORM_CACHE) > 2000:
            _FORM_CACHE.clear()
        _FORM_CACHE[did] = (_time.time() + _FORM_TTL_S, form)
    except Exception:
        pass
    return form


def _standings_kills_sync(kind: str, parsed: dict) -> dict | None:
    """Final kills {did: n} for a sealed bracket/room match.

    None when unavailable (kills side bets refund — never guessed).
    Never raises.
    """
    try:
        if kind == "comp":
            m = supabase.table("bngc_comp_matches").select("id").eq(
                "tourney_id", int(parsed.get("tourney_id"))).eq(
                "version", int(parsed.get("version"))).limit(1).execute()
            rows = m.data or []
            if not rows:
                return None
            r = supabase.table("bngc_comp_results").select("standings").eq(
                "match_id", rows[0].get("id")).limit(1).execute()
            if not (r.data or []):
                return None  # no snapshot yet -> refund, never guessed
            srows = r.data[0]
            return {str(x.get("discord_id")): int(x.get("kills") or 0)
                    for x in (srows.get("standings") or [])
                    if (x or {}).get("discord_id")}
        if kind == "rt":
            m = supabase.table("bngc_ranked_tourney_matches").select("id").eq(
                "tourney_id", int(parsed.get("tourney_id"))).eq(
                "version", int(parsed.get("version"))).limit(1).execute()
            rows = m.data or []
            if not rows:
                return None
            r = supabase.table("bngc_ranked_tourney_results").select(
                "standings").eq("match_id", rows[0].get("id")
                                ).limit(1).execute()
            if not (r.data or []):
                return None  # no snapshot yet -> refund, never guessed
            srows = r.data[0]
            return {str(x.get("discord_id")): int(x.get("kills") or 0)
                    for x in (srows.get("standings") or [])
                    if (x or {}).get("discord_id")}
        if kind == "room":
            r = supabase.table("ranked_results").select("kills").eq(
                "room_id", str(parsed.get("room_id"))).eq(
                "match_version", int(parsed.get("version"))
                ).limit(1).execute()
            rows = r.data or []
            if not rows:
                return None
            return {str(k): int(v or 0)
                    for k, v in ((rows[0] or {}).get("kills") or {}).items()
                    if str(k or "").strip()}
    except Exception:
        pass
    return None


def _settle_side_sync(match_key: str, view: dict, parsed: dict,
                      side_bets: list[dict]) -> tuple[int, int]:
    """Settle fixed-odds side bets. Returns (paid, settled_count).

    kills: final kills for the pick >= line wins (standings on bracket
    and room matches, evidence on live rooms). Missing kills data ->
    refund (never guessed).
    out (live only): pick eliminated (dead/left, not the winner) wins;
    pick with zero evidence rows never played -> refund; a sealed
    winner that isn't the pick is a loss; abandoned rooms refund.
    """
    paid, settled = 0, 0
    try:
        kind = str((parsed or {}).get("kind") or "")
    except Exception:
        return 0, 0
    kills_map: dict | None = None
    dead: set[str] = set()
    left: set[str] = set()
    winner: str | None = None
    abandoned = False
    try:
        if kind == "live" and isinstance(view.get("evidence"), dict):
            evd = view["evidence"]
            kills_map = {str(k): int(v or 0)
                         for k, v in (evd.get("kills") or {}).items()}
            dead = set(evd.get("dead") or [])
            left = set(evd.get("left") or [])
            winner = str(view.get("winner") or "") or None
            abandoned = str(view.get("status") or "") == "sealed" \
                and not winner
        elif kind in ("comp", "rt", "room"):
            kills_map = _standings_kills_sync(kind, parsed or {})
            try:
                wset = _winners_for_sync(view)
                winner = next(iter(wset), None)
            except Exception:
                winner = None
    except Exception:
        pass
    for b in (side_bets or []):
        try:
            btype = str(b.get("type") or "win")
            pick = str(b.get("pick") or "")
            stake = int(b.get("amount") or 0)
            odds = float(b.get("odds") or 0)
        except Exception:
            continue
        verdict: str | None = None  # won | lost | refunded
        payout = 0
        try:
            if btype == "kills":
                try:
                    line = int(b.get("line") or 0)
                except Exception:
                    line = 0
                if kills_map is None or line <= 0:
                    verdict = "refunded"
                    payout = stake
                elif int(kills_map.get(pick, 0) or 0) >= line:
                    verdict = "won"
                    payout = bets_lib.payout_for(stake, odds)
                elif kind == "live" and abandoned:
                    verdict = "refunded"  # unfinished room, unevaluated
                    payout = stake
                else:
                    # live rooms still running keep the bet open
                    if kind == "live" and str(view.get("status") or "") == "open":
                        continue
                    verdict = "lost"
            elif btype == "out":
                if kind != "live":
                    verdict, payout = "refunded", stake
                elif winner and pick == winner:
                    verdict = "lost"
                elif pick in dead or pick in left:
                    verdict = "won"
                    payout = bets_lib.payout_for(stake, odds)
                elif not _pick_seen_sync(view, pick):
                    verdict = "refunded"
                    payout = stake
                elif abandoned:
                    verdict, payout = "refunded", stake
                elif str(view.get("status") or "") == "sealed":
                    verdict = "lost"  # survived, didn't win
                else:
                    continue  # still alive in a live room
            else:
                continue
        except Exception:
            continue
        if verdict is None:
            continue
        try:
            if verdict == "won":
                profit = payout - stake
                if profit > 0:
                    # Daily wager-earnings cap: trim profit, stake always returns.
                    payable = coins_lib.cap_wager_profit(
                        profit, _wager_profit_today_sync(b["bettor"]))
                    payout = stake + payable
                    capped = payable < profit
                else:
                    payable, capped = 0, False
                _apply_delta_sync(b["bettor"], payout, "bet_won",
                                  {"match_key": match_key, "bet": stake,
                                   "pick": pick, "side": btype,
                                   "capped": capped},
                                  earned_delta=payable)
                paid += payout
            elif verdict == "refunded":
                _apply_delta_sync(b["bettor"], payout, "bet_refund",
                                  {"match_key": match_key, "bet": stake,
                                   "pick": pick, "side": btype})
            supabase.table("bngc_match_bets").update(
                {"status": verdict, "payout": payout if verdict == "won" else 0,
                 "settled_at": _now()}).eq("id", b["id"]).execute()
            settled += 1
        except Exception:
            continue
    return paid, settled


def _pick_seen_sync(view: dict, pick: str) -> bool:
    """True when the pick has any evidence rows (played at all)."""
    try:
        if pick in set(view.get("participants") or []):
            return True
        evd = view.get("evidence") or {}
        if pick in set((evd.get("kills") or {})):
            return True
        if pick in set(evd.get("dead") or []) or pick in set(evd.get("left") or []):
            return True
    except Exception:
        pass
    return False


def _load_parsed_sync(parsed: dict | None) -> dict | None:
    """Route a parsed bet key to its loader. Never raises."""
    try:
        if not parsed:
            return None
        k = str(parsed.get("kind") or "")
        if k in ("room", "live"):
            return _load_match_sync(k, parsed.get("room_id"),
                                    parsed.get("version"))
        return _load_match_sync(k, parsed.get("tourney_id"),
                                parsed.get("version"))
    except Exception:
        return None


def _settle_match_sync(match_key: str, bets: list[dict]) -> dict:
    """Settle one match's open bets. Returns {settled, refunded, paid}."""
    parsed = bets_lib.parse_key(match_key)
    if not parsed:
        return {"settled": 0, "refunded": False, "paid": 0}
    view = _load_parsed_sync(parsed)
    if not view or str(view.get("status") or "open") == "open":
        return {"settled": 0, "refunded": False, "paid": 0}
    winners = _winners_for_sync(view)
    # backed_out / winnerless seal -> empty winners -> full refund
    if str(view.get("status")) == "backed_out":
        winners = set()
    win_bets = [b for b in bets if str(b.get("type") or "win") == "win"]
    side_bets = [b for b in bets if str(b.get("type") or "win") != "win"]
    out = bets_lib.settle_pool(win_bets, winners)
    paid = 0
    settled = 0
    for b in win_bets:
        try:
            credit = int(out["credits"].get(b["id"], 0))
        except Exception:
            continue
        try:
            if out["refunded"]:
                _apply_delta_sync(b["bettor"], credit, "bet_refund",
                                  {"match_key": match_key, "bet": b["amount"]})
                status = "refunded"
            elif credit > 0:
                profit = credit - int(b["amount"])
                if profit > 0:
                    # Daily wager-earnings cap: trim profit, stake always returns.
                    payable = coins_lib.cap_wager_profit(
                        profit, _wager_profit_today_sync(b["bettor"]))
                    credit = int(b["amount"]) + payable
                    capped = payable < profit
                else:
                    payable, capped = 0, False
                _apply_delta_sync(b["bettor"], credit, "bet_won",
                                  {"match_key": match_key, "bet": b["amount"],
                                   "pick": b["pick"],
                                   "capped": capped},
                                  earned_delta=payable)
                status = "won"
                paid += credit
            else:
                status = "lost"
            supabase.table("bngc_match_bets").update(
                {"status": status, "payout": credit,
                 "settled_at": _now()}).eq("id", b["id"]).execute()
            settled += 1
        except Exception:
            continue
    # Fixed-odds side markets: evaluated individually, never pooled.
    if side_bets:
        try:
            side_paid, side_n = _settle_side_sync(match_key, view, parsed,
                                                  side_bets)
            paid += side_paid
            settled += side_n
        except Exception:
            pass
    try:
        log_sys(f"[bets] settled {match_key} n={len(bets)} "
                f"refund={out['refunded']} rake={out['rake']} paid={paid}")
    except Exception:
        pass
    return {"settled": settled,
            "refunded": bool(out["refunded"]) and bool(win_bets),
            "paid": paid}


def _settle_pass_sync() -> dict:
    """Settle every open bet whose match has sealed. Idempotent."""
    groups = _open_bets_by_match_sync()
    total = {"matches": 0, "bets": 0, "paid": 0}
    for key, bets in groups.items():
        try:
            r = _settle_match_sync(key, bets)
            if r["settled"]:
                total["matches"] += 1
                total["bets"] += r["settled"]
                total["paid"] += r["paid"]
        except Exception:
            continue
    return total


@router.get("/api/coins/bets/open")
async def bets_open(request: Request):
    try:
        limit = max(1, min(50, int(request.query_params.get("limit") or _OPEN_LIMIT)))
    except Exception:
        limit = _OPEN_LIMIT
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    try:
        def _q():
            _settle_pass_sync()
            matches: list[dict] = []
            # comp: open tourneys' open matches
            try:
                cts = supabase.table("bngc_comp_tourneys").select(
                    "id,name").eq("status", "open").limit(20).execute()
                for t in (cts.data or []):
                    try:
                        ms = supabase.table("bngc_comp_matches").select(
                            "version,room_id").eq(
                            "tourney_id", int(t.get("id"))).eq(
                            "status", "open").order(
                            "version").limit(5).execute()
                        for m in (ms.data or []):
                            v = _load_match_sync("comp", int(t.get("id")),
                                                 int(m.get("version")))
                            if v:
                                matches.append(v)
                    except Exception:
                        continue
            except Exception:
                pass
            # ranked tourneys: started/scheduled tourneys' open matches
            try:
                rts = supabase.table("bngc_ranked_tourneys").select(
                    "id,name").in_("status", ["started", "scheduled"]
                                   ).limit(20).execute()
                for t in (rts.data or []):
                    try:
                        ms = supabase.table("bngc_ranked_tourney_matches").select(
                            "version,room_id").eq(
                            "tourney_id", int(t.get("id"))).eq(
                            "status", "open").order(
                            "version").limit(5).execute()
                        for m in (ms.data or []):
                            v = _load_match_sync("rt", int(t.get("id")),
                                                 int(m.get("version")))
                            if v:
                                matches.append(v)
                    except Exception:
                        continue
            except Exception:
                pass
            # rooms: open ranked matches
            try:
                rms = supabase.table("bngc_ranked_matches").select(
                    "room_id,match_version").eq("status", "open").order(
                    "match_version", desc=True).limit(10).execute()
                for m in (rms.data or []):
                    try:
                        v = _load_match_sync("room", str(m.get("room_id")),
                                             int(m.get("match_version")))
                        if v:
                            matches.append(v)
                    except Exception:
                        continue
            except Exception:
                pass
            # live rooms: any public/private match with fresh evidence
            # (no engine needed — the ledger already carries room+version)
            try:
                now = _time.time()
                groups: dict[tuple[str, int], dict] = {}
                for r in _scan_evidence_sync():
                    try:
                        if now - _ts_of(r) > bets_lib.DISCOVERY_SCAN_S:
                            break  # newest-first scan: older rows only age
                        k = _room_key_of(r)
                        if not k:
                            continue
                        g = groups.setdefault(k, {"parts": set(), "win": None,
                                                 "latest": 0.0, "gm": None,
                                                 "code": None})
                        did = str(r.get("discord_id") or "").strip()
                        if did:
                            g["parts"].add(did)
                        if str(r.get("event_type") or "") == "win" and did \
                                and not g["win"]:
                            g["win"] = did
                        ts = _ts_of(r)
                        if ts > g["latest"]:
                            g["latest"] = ts
                        ed = (r or {}).get("event_data") or {}
                        if not g["gm"] and ed.get("gm"):
                            g["gm"] = str(ed.get("gm"))
                        if not g["code"] and (ed.get("short_code") or ed.get("code")):
                            g["code"] = str(ed.get("short_code") or ed.get("code"))
                    except Exception:
                        continue
                for (rid, ver), g in sorted(groups.items(),
                                            key=lambda kv: -kv[1]["latest"]):
                    try:
                        if len(matches) >= limit:
                            break
                        _sess_key = str(rid).startswith("sess:")
                        if not _sess_key and (g["win"] or len(g["parts"]) < 2):
                            continue
                        if now - g["latest"] > bets_lib.LIVE_WINDOW_S:
                            continue
                        v = _load_match_sync("live", rid, ver)
                        if v and str(v.get("status") or "") == "open":
                            matches.append(v)
                    except Exception:
                        continue
            except Exception:
                pass
            pools: dict[str, dict] = {}
            try:
                groups = _open_bets_by_match_sync()
                for key, bets in groups.items():
                    pools[key] = {"pool": sum(int(b.get("amount") or 0)
                                             for b in bets),
                                  "bets": len(bets)}
            except Exception:
                pass
            out = []
            for v in matches[:limit]:
                p = pools.get(v["key"] or "", {"pool": 0, "bets": 0})
                shown = (v.get("participants") or [])[:20]
                out.append({"key": v["key"], "kind": v["kind"],
                            "label": v["label"],
                            "participants": [{"id": did,
                                              "name": (v.get("names") or {}
                                                       ).get(did, did[:6])}
                                             for did in shown],
                            "participant_count": len(v.get("participants") or []),
                            "pool": p["pool"], "bets": p["bets"]})
            return {"matches": out}
        res = await db_io(_q)
        return JSONResponse(res)
    except Exception as e:
        if _missing_table(e) or "bngc_match_bets" in str(e):
            return JSONResponse(
                {"error": "Database migration pending — ask a host to apply "
                          "add_match_betting_v253.sql."}, status_code=500)
        return JSONResponse({"error": str(e)[:160]}, status_code=500)


@router.post("/api/coins/bets")
async def bets_place(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    did = str((body or {}).get("discord_id") or "").strip()
    key = str((body or {}).get("match_key") or "").strip()
    pick = str((body or {}).get("pick_discord_id") or (body or {}).get("pick") or "").strip()
    btype = bets_lib.normalize_bet_type((body or {}).get("bet_type"))
    line = (body or {}).get("line")
    try:
        amount = int((body or {}).get("amount") or 0)
    except Exception:
        amount = 0
    if not did or not key or not pick:
        return JSONResponse(
            {"error": "discord_id, match_key and pick_discord_id required"},
            status_code=400)
    parsed = bets_lib.parse_key(key)
    if not parsed:
        return JSONResponse({"error": "Bad match_key — comp:{tid}:{ver} | rt:{tid}:{ver} | room:{rid}:{ver} | live:{room_id}:{ver} (see /matches)"},
                            status_code=400)
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    try:
        def _place():
            _settle_pass_sync()
            view = _load_parsed_sync(parsed)
            if not view:
                raise LookupError("No such match (or its tourney closed).")
            if str(view.get("status") or "open") != "open":
                raise ValueError("Betting closed — that match already sealed.")
            if pick not in set(view.get("participants") or []):
                raise ValueError("That pick is not playing this match.")
            kind = str(parsed.get("kind") or "")
            _sess_match = kind == "live" and str(parsed.get("room_id") or "").startswith("sess-")
            if _sess_match and btype == "win":
                raise ValueError("Session matches take side bets only (kills/out) — no win pool.")
            if not _sess_match and len(set(view.get("participants") or [])) < 2:
                raise ValueError("Not enough tracked players in that room yet.")
            if btype == "out" and kind != "live":
                raise ValueError("Out bets are live-room only (elimination "
                                 "is only tracked there).")
            if _sess_match and btype == "out" and pick not in set(view.get("_row_holders") or []):
                # Mapped names have no death/leave rows — an out bet on them
                # could never win. Kills lines work for anyone in the room
                # (folded feed kills), enforced by the participants check above.
                raise ValueError("That pick has no tracked rows in this match — "
                                 "pick a tracked player for outs.")
            line_no: int | None = None
            odds: float | None = None
            if btype == "kills":
                ok_l, err_l = bets_lib.validate_line(line)
                if not ok_l:
                    raise ValueError(err_l or "Bad kills line.")
                line_no = int(line)
            user = supabase.table("bngc_users").select(
                "nectar_coins").eq("discord_id", did).limit(1).execute()
            rows = user.data or []
            if not rows:
                raise LookupError("user not found")
            ok, err = bets_lib.validate_stake((rows[0] or {}).get("nectar_coins"), amount)
            if not ok:
                raise ValueError(err or "Invalid bet.")
            if btype in ("out", "kills"):
                form = _form_sync(pick)
                field = len(set(view.get("participants") or []))
                if btype == "out":
                    odds = bets_lib.out_odds(form["win_rate"], field)
                else:
                    odds = bets_lib.kills_odds(form["lam"], line_no)
            _apply_delta_sync(did, -int(amount), "bet_placed",
                              {"match_key": key, "pick": pick,
                               "bet": int(amount), "side": btype,
                               **({"line": line_no} if line_no else {})})
            ins = supabase.table("bngc_match_bets").insert({
                "match_key": key, "kind": parsed["kind"],
                "tourney_id": parsed.get("tourney_id"),
                "room_id": parsed.get("room_id"),
                "version": int(parsed["version"]),
                "bettor_discord_id": did, "pick_discord_id": pick,
                "amount": int(amount), "status": "open",
                "bet_type": btype, "line": line_no, "odds": odds,
            }).execute()
            bid = ((ins.data or [{}])[0] or {}).get("id")
            groups = _open_bets_by_match_sync()
            mine = [b for b in groups.get(key, [])
                    if str(b.get("type") or "win") == "win"]
            pool = sum(int(b.get("amount") or 0) for b in mine)
            resp = {"bet_id": bid, "match_key": key,
                    "label": view.get("label"), "pick": pick,
                    "pick_name": (view.get("names") or {}).get(pick, pick[:6]),
                    "bet_type": btype, "amount": int(amount),
                    "pool": pool, "pool_bets": len(mine)}
            if odds is not None:
                resp["odds"] = odds
                resp["to_win"] = bets_lib.payout_for(int(amount), odds)
            return resp
        out = await db_io(_place)
        return JSONResponse(out)
    except LookupError as e:
        return JSONResponse({"error": str(e)[:160]}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)[:160]}, status_code=400)
    except Exception as e:
        if _missing_table(e) or "bngc_match_bets" in str(e):
            return JSONResponse(
                {"error": "Database migration pending — ask a host to apply "
                          "add_match_betting_v253.sql."}, status_code=500)
        return JSONResponse({"error": str(e)[:160]}, status_code=500)


@router.get("/api/coins/bets/mine")
async def bets_mine(request: Request):
    did = (request.query_params.get("discord_id")
           or request.query_params.get("did") or "").strip()
    if not did:
        return JSONResponse({"error": "discord_id required"}, status_code=400)
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    try:
        def _q():
            _settle_pass_sync()
            res = supabase.table("bngc_match_bets").select(
                "match_key,pick_discord_id,amount,status,payout,bet_type,"
                "line,odds,created_at").eq("bettor_discord_id", did).order(
                "created_at", desc=True).limit(30).execute()
            rows = res.data or []
            ids = {str(r.get("pick_discord_id")) for r in rows if r.get("pick_discord_id")}
            names = _names_sync(sorted(ids))
            # labels for the open ones (cheap: reuse loader per key)
            labels: dict[str, str] = {}
            for r in rows:
                try:
                    k = str(r.get("match_key"))
                    if k not in labels and str(r.get("status")) == "open":
                        v = _load_parsed_sync(bets_lib.parse_key(k))
                        labels[k] = (v or {}).get("label") or k
                except Exception:
                    continue
            open_b, settled = [], []
            for r in rows:
                try:
                    item = {"match_key": str(r.get("match_key")),
                            "label": labels.get(str(r.get("match_key")),
                                                str(r.get("match_key"))),
                            "pick": str(r.get("pick_discord_id")),
                            "pick_name": names.get(str(r.get("pick_discord_id")),
                                                  str(r.get("pick_discord_id"))[:6]),
                            "amount": int(r.get("amount") or 0),
                            "status": str(r.get("status")),
                            "payout": int(r.get("payout") or 0),
                            "bet_type": str(r.get("bet_type") or "win")}
                except Exception:
                    continue
                try:
                    if r.get("line") is not None:
                        item["line"] = int(r.get("line"))
                    if r.get("odds") is not None:
                        item["odds"] = float(r.get("odds"))
                except Exception:
                    pass
                (open_b if item["status"] == "open" else settled).append(item)
            return {"open": open_b, "settled": settled[:10]}
        return JSONResponse(await db_io(_q))
    except Exception as e:
        if _missing_table(e) or "bngc_match_bets" in str(e):
            return JSONResponse(
                {"error": "Database migration pending — ask a host to apply "
                          "add_match_betting_v253.sql."}, status_code=500)
        return JSONResponse({"error": str(e)[:160]}, status_code=500)


_OFFERED_MENU = (3, 5, 10)


def _offered_sync(view: dict, parsed: dict) -> list[dict]:
    """Priced side markets per participant for the pool view.

    Each entry: {id, name, matches, avg_kills, win_rate, provisional,
    out_odds (live only), kills: {3: x, 5: y, 10: z}}. Never raises.
    """
    out: list[dict] = []
    try:
        kind = str((parsed or {}).get("kind") or "")
        parts = list(view.get("participants") or [])[:24]
        names = view.get("names") or {}
        field = max(2, len(parts))
        for did in parts:
            try:
                form = _form_sync(did)
                entry = {"id": did, "name": names.get(did, did[:6]),
                         "matches": int(form.get("matches") or 0),
                         "avg_kills": round(float(form.get("lam") or 0), 1),
                         "win_rate": round(float(form.get("win_rate") or 0), 3),
                         "provisional": bool(form.get("provisional")),
                         "kills": {str(x): bets_lib.kills_odds(
                             form.get("lam"), x) for x in _OFFERED_MENU}}
                if kind == "live":
                    entry["out_odds"] = bets_lib.out_odds(
                        form.get("win_rate"), field)
                out.append(entry)
            except Exception:
                continue
    except Exception:
        pass
    return out


@router.get("/api/coins/bets/pool")
async def bets_pool(request: Request):
    key = (request.query_params.get("match_key")
           or request.query_params.get("key") or "").strip()
    parsed = bets_lib.parse_key(key)
    if not parsed:
        return JSONResponse({"error": "Valid match_key required"}, status_code=400)
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    try:
        def _q():
            _settle_pass_sync()
            view = _load_parsed_sync(parsed)
            if not view:
                return {"error": "no-such-match"}
            res = supabase.table("bngc_match_bets").select(
                "bettor_discord_id,pick_discord_id,amount,status,payout,"
                "bet_type,line,odds").eq(
                "match_key", key).limit(500).execute()
            rows = res.data or []
            win_open = [r for r in rows
                        if str((r or {}).get("status")) == "open"
                        and str((r or {}).get("bet_type") or "win") == "win"]
            side_open = [r for r in rows
                         if str((r or {}).get("status")) == "open"
                         and str((r or {}).get("bet_type") or "win") != "win"]
            per: dict[str, dict] = {}
            for r in win_open:
                try:
                    pid = str(r.get("pick_discord_id"))
                    d = per.setdefault(pid, {"total": 0, "bettors": 0})
                    d["total"] += int(r.get("amount") or 0)
                    d["bettors"] += 1
                except Exception:
                    continue
            pool = sum(int((r or {}).get("amount") or 0) for r in win_open)
            per_pick = [{"id": pid,
                         "name": (view.get("names") or {}).get(pid, pid[:6]),
                         "total": d["total"], "bettors": d["bettors"]}
                        for pid, d in sorted(per.items(),
                                             key=lambda kv: -kv[1]["total"])]
            settled_rows = [r for r in rows if str((r or {}).get("status")) != "open"]
            paid = sum(int((r or {}).get("payout") or 0) for r in settled_rows
                       if str((r or {}).get("status")) == "won")
            wname = None
            try:
                if view.get("winner"):
                    wname = (view.get("names") or {}).get(
                        str(view["winner"]), str(view["winner"])[:6])
            except Exception:
                pass
            resp = {"match_key": key, "label": view.get("label"),
                    "status": view.get("status"), "winner": view.get("winner"),
                    "winner_name": wname, "pool": pool,
                    "open_bets": len(win_open), "per_pick": per_pick,
                    "settled_bets": len(settled_rows), "paid": paid}
            try:
                resp["side_bets"] = [{
                    "pick": str(r.get("pick_discord_id")),
                    "pick_name": (view.get("names") or {}).get(
                        str(r.get("pick_discord_id")),
                        str(r.get("pick_discord_id"))[:6]),
                    "type": str(r.get("bet_type") or "win"),
                    "line": r.get("line"),
                    "odds": float(r.get("odds"))
                    if r.get("odds") is not None else None,
                    "amount": int(r.get("amount") or 0),
                } for r in side_open[:50]]
            except Exception:
                resp["side_bets"] = []
            try:
                if str(view.get("status") or "") == "open":
                    resp["offered"] = _offered_sync(view, parsed)
            except Exception:
                pass
            try:
                if str(view.get("kind") or "") == "live" and view.get("evidence"):
                    evd = view["evidence"]
                    names = view.get("names") or {}
                    kills = evd.get("kills") or {}
                    dead = set(evd.get("dead") or [])
                    left = set(evd.get("left") or [])
                    resp["evidence"] = {
                        "kill_target": evd.get("kill_target",
                                               bets_lib.LIVE_KILL_TARGET),
                        "players": [{"id": did,
                                     "name": names.get(did, did[:6]),
                                     "kills": int(kills.get(did) or 0),
                                     "status": ("out" if did in dead or did in left
                                                else "alive")}
                                    for did in (view.get("participants") or [])],
                    }
            except Exception:
                pass
            return resp
        out = await db_io(_q)
        if isinstance(out, dict) and out.get("error") == "no-such-match":
            return JSONResponse({"error": "No such match."}, status_code=404)
        return JSONResponse(out)
    except Exception as e:
        if _missing_table(e) or "bngc_match_bets" in str(e):
            return JSONResponse(
                {"error": "Database migration pending — ask a host to apply "
                          "add_match_betting_v253.sql."}, status_code=500)
        return JSONResponse({"error": str(e)[:160]}, status_code=500)


@router.post("/api/coins/bets/settle")
async def bets_settle(request: Request):
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    try:
        out = await db_io(_settle_pass_sync)
        return JSONResponse({"settled_matches": out["matches"],
                             "bets_settled": out["bets"],
                             "credits_paid": out["paid"]})
    except Exception as e:
        if _missing_table(e) or "bngc_match_bets" in str(e):
            return JSONResponse(
                {"error": "Database migration pending — ask a host to apply "
                          "add_match_betting_v253.sql."}, status_code=500)
        return JSONResponse({"error": str(e)[:160]}, status_code=500)
