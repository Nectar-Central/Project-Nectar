#!/usr/bin/env python3
"""reconcile_ranked.py — nightly points-vs-ledger sweep (report-only).

Reads ranked_points (store) and ranked_results.points (ledger), attributes
every legitimate non-ledgered stream (entry fees charged at room start,
paid tourney payouts, in-flight seals, season reset), classifies the
residual per player (server/lib/reconcile.py), and prints one JSON report.

Run:
    python -m server.scripts.reconcile_ranked
Schedule (host cron, one line):
    15 4 * * *  cd /srv/nectar && /usr/bin/python3 -m server.scripts.reconcile_ranked >> /var/log/ranked_reconcile.log 2>&1
Alerting (optional):
    RECONCILE_WEBHOOK_URL — when set, a Discord-style {"content": ...} POST
    is sent whenever the report has nonzero drift or critical counts.

This script NEVER writes: store, ledger and evidence tables are read; the
report is printed. ALWAYS exits 0 — an alerting sweep that crashes tells
you nothing; the report's counts carry the signal. Exceptions are caught,
reported in the JSON, and still exit 0.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

from server.lib import reconcile
from server.lib.ranked import _sb  # returns the service client or None
from server.lib.ranked import MILESTONE_TIERS

LIVE_GRACE_S = 30 * 60  # rooms live within this window explain pending seals
PAGE = 1000
MAX_ROWS = 200_000
TID_CAP = 200
MAX_TIER_FEE = max(t["fee"] for t in MILESTONE_TIERS)  # 300 (Grandmaster)


def _page_all(table: str, select: str, order: str, errors: list | None = None) -> list:
    """Read every row of a small table, paged. [] on any error; the
    failure (when an errors list is given) is surfaced in the report."""
    sb = _sb()
    if sb is None:
        return []
    rows: list = []
    off = 0
    while off < MAX_ROWS:
        try:
            r = sb.table(table).select(select).order(order).range(off, off + PAGE - 1).execute()
        except Exception as exc:
            print(f"[reconcile] read failed {table}@{off}: {exc}", file=sys.stderr)
            if errors is not None:
                errors.append(f"{table} read failed at offset {off}: {exc}")
            break
        batch = r.data or []
        rows.extend(batch)
        if len(batch) < PAGE:
            break
        off += PAGE
    return rows


def _ts_epoch(iso: str) -> int:
    """ISO timestamp -> epoch seconds (0 on any failure)."""
    try:
        s = str(iso or "").replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return 0


def _read_state() -> dict:
    """All IO in one place: store, ledger, fees, payouts, reset, pending."""
    sb = _sb()
    out: dict = {"errors": []}
    now = int(datetime.now(timezone.utc).timestamp())

    # ---- store -----------------------------------------------------------
    store: dict[str, int] = {}
    for row in _page_all("ranked_points", "discord_id,points", "discord_id",
                         errors=out["errors"]):
        did = str((row or {}).get("discord_id") or "").strip()
        if did:
            try:
                store[did] = int(row.get("points") or 0)
            except Exception:
                continue
    out["store"] = store

    # ---- ledger (ranked_results.points), with per-row timestamps ---------
    ledger: dict[str, int] = {}
    recent_keys: dict[str, list] = {}
    rows = _page_all("ranked_results", "room_id,match_version,points,sealed_at",
                     "sealed_at", errors=out["errors"])
    for row in rows:
        rid = str((row or {}).get("room_id") or "").strip()
        try:
            mver = int(row.get("match_version") or 0)
        except Exception:
            continue
        if not rid or mver <= 0:
            continue
        key = f"{rid}:{mver}"
        ts = _ts_epoch(row.get("sealed_at"))
        for did, p in reconcile.int_map((row or {}).get("points")).items():
            ledger[did] = ledger.get(did, 0) + p
            recent_keys.setdefault(did, []).append((ts, key))
    out["ledger"] = ledger

    # ---- season reset epoch + survivors ----------------------------------
    # Heuristic: the store was wiped when its most-recently-touched row has
    # zero points (reset deletes rows; fresh entries start at 0 until the
    # first seal). Ledger rows sealed AFTER that timestamp belong to the new
    # season; rows still present from before it are reset survivors.
    reset_epoch = 0
    reset_players: set[str] = set()
    try:
        r = sb.table("ranked_points").select("discord_id,points,updated_at").order(
            "updated_at", desc=True).limit(1).execute()
        r0 = (r.data or [None])[0] or {}
        if int(r0.get("points") or 0) == 0:
            reset_epoch = _ts_epoch(r0.get("updated_at"))
    except Exception as exc:
        out["errors"].append(f"reset-epoch probe: {exc}")
    if reset_epoch:
        # Survivors = ledger rows sealed BEFORE the wipe that the reset
        # failed to drain (rows after it are this season's legit income).
        for did, items in recent_keys.items():
            if any(ts and ts < reset_epoch for ts, _k in items):
                reset_players.add(did)
    out["reset_epoch"] = reset_epoch
    out["reset_players"] = reset_players

    # ---- rooms: fees + in-flight seals -----------------------------------
    # All three statuses: lobby (start window may have charged already),
    # live (fees charged, seal in flight), ended (fees charged long ago).
    # Recency-capped: attribution only needs this season's rooms.
    fees: dict[str, int] = {}
    live_pending: set[str] = set()
    rooms_live = 0
    rooms_fee_scan = 0
    try:
        rres = sb.table("ranked_rooms").select(
            "id,short_code,status,fee,roster,match_version").in_(
            "status", ["lobby", "live", "ended"]).order(
            "updated_at", desc=True).limit(500).execute()
        for room in (rres.data or []):
            status = str((room or {}).get("status") or "")
            roster: list[str] = []
            for x in ((room or {}).get("roster") or []):
                try:
                    did = x.get("discord_id") if isinstance(x, dict) else x
                    d = str(did or "").strip()
                    if d:
                        roster.append(d)
                except Exception:
                    continue
            # Each roster member pays the FULL room fee at start
            # (entry_fee = room fee + personal tier fee); the tier fee is
            # not stored on the room and stays a documented caveat.
            fee = int((room or {}).get("fee") or 0)
            if fee > 0 and roster:
                rooms_fee_scan += 1
                for did in roster:
                    fees[did] = fees.get(did, 0) + fee
            if status == "live" and int((room or {}).get("match_version") or 0) > 0:
                rooms_live += 1
                # pending only explains deltas from seals in flight; a live
                # room's older sealed versions are settled and stay in the
                # ledger.
                live_pending.update(roster)
    except Exception as exc:
        out["errors"].append(f"rooms read: {exc}")
    out["fees"] = fees
    out["rooms_live"] = rooms_live
    out["rooms_fee_scan"] = rooms_fee_scan
    out["live_pending"] = live_pending

    # ---- paid tourney payouts (membership note; amount not recomputed) ----
    tourney_members: set[str] = set()
    tourneys_paid_recent = 0
    try:
        tres = sb.table("bngc_ranked_tourneys").select(
            "id,points_paid").eq("points_paid", True).order(
            "id", desc=True).limit(TID_CAP).execute()
        tids = sorted({int((t or {}).get("id") or 0) for t in (tres.data or [])} - {0})
        tourneys_paid_recent = len(tids)
        if tids:
            rres2 = sb.table("bngc_ranked_tourney_registrations").select(
                "tourney_id,member_ids,status").in_("tourney_id", tids[:TID_CAP]).execute()
            for reg in (rres2.data or []):
                if str((reg or {}).get("status") or "") == "accepted":
                    for did in ((reg or {}).get("member_ids") or []):
                        d = str(did or "").strip()
                        if d:
                            tourney_members.add(d)
    except Exception as exc:
        out["errors"].append(f"tourney payout read: {exc}")
    out["tourney_members"] = tourney_members
    out["tourneys_paid_recent"] = tourneys_paid_recent

    # ---- pending tier-fee caveat -----------------------------------------
    # Milestone tier fees (ranked.py milestone_for) are charged from the
    # same add_points call but are NOT stored on the room; a drifted player
    # who joined a room tonight may be explained by fee + tier fee. Reported
    # as context, never silently zeroed.
    out["tier_fee_caveat"] = (
        "entry-fee attribution covers the room's stored fee per member "
        "(full fee each, as charged); the milestone tier fee charged "
        "alongside it is not stored and can explain a small positive "
        "drift for players who joined a room today")
    return out


def _attribute(state: dict) -> tuple[dict, dict, dict]:
    """(deltas, ledger, per-did attribution notes).

    Fees make the store SMALLER than the ledger, so they explain NEGATIVE
    deltas; attribution is clamped so it can never flip a deficit into a
    fake surplus."""
    store = state["store"]
    ledger = state["ledger"]
    fees = state["fees"]
    deltas: dict[str, int] = {}
    attrib: dict[str, str] = {}
    for did in sorted(set(store) | set(ledger)):
        d = int(store.get(did, 0)) - int(ledger.get(did, 0))
        if not d:
            continue
        if d < 0 and did in fees:
            adj = min(fees[did], -d)
            if adj:
                d += adj
                attrib[did] = f"entry fee attributed ({adj})"
        if d:
            deltas[did] = d
    return deltas, ledger, attrib


def _tier_fee_explainable(deltas: dict, state: dict) -> set:
    """Dids whose negative residual sits within their scanned rooms' fee
    + max milestone tier fee (the tier fee is charged but never stored)."""
    out: set = set()
    fees = state.get("fees") or {}
    for did, d in deltas.items():
        if d < 0 and did in fees and -d <= fees[did] + MAX_TIER_FEE:
            out.add(did)
    return out


def _alert(report: dict) -> None:
    """Best-effort webhook when drift/critical exist. Never raises."""
    counts = report.get("counts") or {}
    drift = int(counts.get(reconcile.STATUS_DRIFT, 0))
    crit = int(counts.get(reconcile.STATUS_CRITICAL, 0))
    if not (drift or crit):
        return
    url = os.environ.get("RECONCILE_WEBHOOK_URL") or ""
    if not url:
        return
    try:
        pend = int(counts.get(reconcile.STATUS_PENDING, 0))
        msg = (f"⚠️ Ranked reconcile: **{drift}** drift, **{crit}** critical"
               f" (pending: {pend}). Report-only — see the nightly log"
               " for per-player repair actions.")
        req = urllib.request.Request(
            url, data=json.dumps({"content": msg}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def main() -> int:
    try:
        state = _read_state()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"read failed: {exc}",
                          "note": "report-only sweep; nothing was written"}))
        return 0
    deltas, ledger, attrib = _attribute(state)
    try:
        classified = reconcile.classify_deltas(
            deltas,
            pending_keys=state["live_pending"],
            reset_epoch=state["reset_epoch"],
            reset_players=state["reset_players"],
            tier_fee_explainable=_tier_fee_explainable(deltas, state),
        )
        report = reconcile.build_report(
            store=state["store"], ledger=ledger, deltas=deltas,
            classified=classified,
            reset_epoch=state["reset_epoch"],
            rooms_live=state["rooms_live"],
            tourneys_paid_recent=state["tourneys_paid_recent"],
            fees_attributed=sum(1 for _ in attrib.values()),
        )
        report["context"]["rooms_fee_scan"] = state.get("rooms_fee_scan", 0)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"classify failed: {exc}",
                          "note": "report-only sweep; nothing was written"}))
        return 0
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["context"]["live_pending_players"] = len(state["live_pending"])
    report["context"]["tier_fee_caveat"] = state["tier_fee_caveat"]
    report["attribution"] = attrib
    if state["errors"]:
        report["read_errors"] = state["errors"]
    print(json.dumps(report, indent=2))
    _alert(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
