"""Ranked points-vs-ledger reconciliation core (pure).

The ladder economy (server/lib/ranked.py + companion/bot) has exactly one
points store: ``ranked_points``.  Streams that legitimately move it:

1. Seal awards    — recorded in ranked_results.points
                     (seal_match -> _bump_points_many)
2. Recalc fixes   — differential by construction, self-zeroing
3. Tourney pay    — flagged bngc_ranked_tourneys.points_paid = true
4. Entry fees     — charged at room start (bot add_points -fee),
                     attributed from the room's frozen start roster
5. Season reset   — deletes every ranked_points row (bot reset_points)

Store minus ledger therefore has legitimate nonzero shapes.  This module
classifies them; it never touches the database.  The runner
(server/scripts/reconcile_ranked.py) reads, attributes, reports — and
does not write.
"""

from __future__ import annotations

from typing import Any, Iterable

__all__ = [
    "classify_deltas", "build_report", "int_map",
    "STATUS_OK", "STATUS_PENDING", "STATUS_DRIFT", "STATUS_CRITICAL",
]

STATUS_OK = "ok"
STATUS_PENDING = "pending_seal"
STATUS_DRIFT = "drift"
STATUS_CRITICAL = "critical"

# Deltas within this absolute size (and not eligible for a pending-seal
# explanation) are still drift, but graded "minor" — small enough that a
# lost -5 loss or a rounding edge is the likely story.
MINOR_DRIFT_ABS = 5



def int_map(m: Any) -> dict[str, int]:
    """Any dict-like -> {str did: int points}, empty keys dropped."""
    out: dict[str, int] = {}
    try:
        for k, v in dict(m or {}).items():
            try:
                key = str(k or "").strip()
                if key:
                    out[key] = int(v or 0)
            except Exception:
                continue
    except Exception:
        pass
    return out


def classify_deltas(
    deltas: dict[str, int],
    *,
    pending_keys: Iterable[str] = (),
    reset_epoch: int = 0,
    reset_players: Iterable[str] = (),
    tier_fee_explainable: Iterable[str] = (),
) -> dict[str, dict]:
    """Classify store-vs-ledger deltas into per-player statuses.

    deltas:  store_points[did] - ledger_sum[did]  (may be any sign)
    pending_keys: dids with an in-flight seal at sweep time (live room,
        match_version bumped, no results row yet)
    reset_players: dids the season reset forgot to drain the ledger for
        (ledger rows written after the reset timestamp)
    tier_fee_explainable: dids whose negative residual sits within the
        fee+tier-fee bound of a scanned room (the milestone tier fee is
        charged at start but never stored, so a small negative residual
        for a room-starting player is explained, not alarming)

    Pure: no I/O, no globals, deterministic.
    """
    try:
        reset_set = {str(x) for x in (reset_players or []) if str(x or "").strip()}
    except Exception:
        reset_set = set()
    try:
        tier_set = {str(x) for x in (tier_fee_explainable or []) if str(x or "").strip()}
    except Exception:
        tier_set = set()
    try:
        pend = {str(x) for x in (pending_keys or []) if str(x or "").strip()}
    except Exception:
        pend = set()
    out: dict[str, dict] = {}
    for did, delta in int_map(deltas).items():
        if delta == 0:
            out[did] = {"delta": 0, "status": STATUS_OK}
            continue
        rec: dict[str, Any] = {"delta": int(delta), "status": None, "note": None, "actions": []}
        # 1. Forgotten pre-reset rows: ledger carries points the store lost
        #    when the season was wiped. Reparable by draining the ledger.
        if delta < 0 and did in reset_set:
            rec["status"] = STATUS_DRIFT
            rec["note"] = "pre-reset ledger rows survived the season reset"
            rec["actions"] = ["drain_ledger_rows"]
            out[did] = rec
            continue
        # 1b. Tier-fee band: negative residual within a scanned room's
        #     fee + max tier fee is most likely the un-stored tier fee.
        if delta < 0 and did in tier_set:
            rec["status"] = STATUS_DRIFT
            rec["grade"] = "minor"
            rec["note"] = ("within the fee+tier-fee bound of a scanned room "
                           "(milestone tier fee is charged at start but not stored)")
            rec["actions"] = ["verify the player's tier fee at room start",
                              "repair only if confirmed beyond fees"]
            out[did] = rec
            continue
        # 2. In-flight seal: awards are between the bump and the results
        #    write. Tolerated for one night, then reclassified.
        if delta > 0 and did in pend:
            rec["status"] = STATUS_PENDING
            rec["note"] = "in-flight seal (awards may land after this read)"
            rec["actions"] = ["none"]
            out[did] = rec
            continue
        # 3. Positive delta: store above ledger (overpaid / fee not
        #    modeled / lost results row). Negative: ledger above store
        #    (deficit — double-pay or charge raced an unpaid fee) and is
        #    ALWAYS critical: the ladder pays out what evidence denies.
        absd = abs(delta)
        if delta > 0:
            rec["status"] = STATUS_DRIFT
            rec["grade"] = "minor" if absd <= MINOR_DRIFT_ABS else "material"
            rec["note"] = "store above ledger"
            rec["actions"] = [
                "check results row exists for recent seals",
                "verify entry fees are attributed (unmodeled fee looks like drift)",
                "repair: apply negative adjustment to ranked_points, or fix the results row then rerun recalc",
            ]
        else:
            rec["status"] = STATUS_CRITICAL
            rec["note"] = "ledger above store (deficit)"
            rec["actions"] = [
                "check for double seal awards (v250 unique rails prevent re-seal replay)",
                "verify fee charge did not race the sweep's roster read",
                "repair: apply positive adjustment to ranked_points",
            ]
        out[did] = rec
    # Anything left in pending_keys without a delta is trivially fine.
    return out


def build_report(
    *,
    store: dict[str, int],
    ledger: dict[str, int],
    deltas: dict[str, int],
    classified: dict[str, dict],
    reset_epoch: int = 0,
    rooms_live: int = 0,
    tourneys_paid_recent: int = 0,
    fees_attributed: int = 0,
) -> dict:
    """Assemble the report dict. Pure — the runner adds IO facts around it."""
    counts = {STATUS_OK: 0, STATUS_PENDING: 0, STATUS_DRIFT: 0, STATUS_CRITICAL: 0}
    # classified carries only problem players (the runner skips zero
    # deltas); everyone in store/ledger not flagged here is clean OK.
    nonzero = {d for d, c in classified.items()
               if (c.get("status") or STATUS_DRIFT) != STATUS_OK}
    counts[STATUS_OK] = len(({str(k) for k in int_map(store)}
                             | {str(k) for k in int_map(ledger)}) - nonzero)
    players = []
    for did in sorted(nonzero, key=lambda d: (abs(int(classified[d].get("delta", 0))), d), reverse=True):
        c = classified[did]
        st = c.get("status") or STATUS_DRIFT
        counts[st] = counts.get(st, 0) + 1
        players.append({
            "discord_id": did,
            "store": int(store.get(did, 0)),
            "ledger": int(ledger.get(did, 0)),
            **c,
        })
    total_store = sum(int(v) for v in (store or {}).values())
    total_ledger = sum(int(v) for v in (ledger or {}).values())
    return {
        "ok": True,
        "counts": counts,
        "players": players,
        "totals": {
            "store": total_store,
            "ledger": total_ledger,
            "delta": total_store - total_ledger,
            "store_players": len([1 for v in (store or {}).values() if v]),
        },
        "context": {
            "reset_epoch": int(reset_epoch or 0),
            "rooms_live": int(rooms_live or 0),
            "tourneys_paid_recent": int(tourneys_paid_recent or 0),
            "fees_attributed": int(fees_attributed or 0),
        },
        "notes": [
            "report-only: this sweep never writes ranked_points, "
            "ranked_results, or any other table",
        ],
    }
