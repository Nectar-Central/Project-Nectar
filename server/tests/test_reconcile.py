"""Nightly points-vs-ledger reconciliation (report-only).

Covers the pure classifier (server/lib/reconcile.py) and the runner's
attribution logic (server/scripts/reconcile_ranked.py) against a fake
supabase: clean store, in-flight seal tolerance, entry-fee attribution
(full fee per member, clamped), drift vs critical classification,
season-reset survivors, write-attempt detection (the sweep must never
write), exit-code contract, and webhook gating.
"""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from server.lib import reconcile
from server.scripts import reconcile_ranked as rr

T0 = "2026-09-17T00:00:00+00:00"
T_BEFORE = "2026-09-16T23:00:00+00:00"


def _epoch(iso):
    return int(datetime.fromisoformat(iso).timestamp())


class _Q:
    def __init__(self, fake, table):
        self.fake, self.table = fake, table
        self.op = "select"
        self.cols = "*"
        self.filters = []

    def select(self, cols):
        self.cols = cols
        return self

    def eq(self, c, v):
        self.filters.append(("eq", c, v)); return self

    def in_(self, c, v):
        self.filters.append(("in", c, v)); return self

    def neq(self, c, v):
        self.filters.append(("neq", c, v)); return self

    def order(self, c, desc=False):
        self.filters.append(("order", c, desc)); return self

    def range(self, a, b):
        self.filters.append(("range", a, b)); return self

    def limit(self, n):
        self.filters.append(("limit", n)); return self

    # write attempts must never happen in this sweep
    def upsert(self, p):
        raise AssertionError(f"RECONCILE WROTE via upsert on {self.table}: {p}")

    def update(self, p):
        raise AssertionError(f"RECONCILE WROTE via update on {self.table}: {p}")

    def insert(self, p):
        raise AssertionError(f"RECONCILE WROTE via insert on {self.table}: {p}")

    def delete(self):
        raise AssertionError(f"RECONCILE WROTE via delete on {self.table}")

    def execute(self):
        return _R(self.fake._run(self))


class _R:
    def __init__(self, data):
        self.data = data


class _FakeSB:
    """Row store keyed by table with generic select filtering."""

    def __init__(self):
        self.tables = {
            "ranked_points": [],
            "ranked_results": [],
            "ranked_rooms": [],
            "bngc_ranked_tourneys": [],
            "bngc_ranked_tourney_registrations": [],
        }
        self.selects = []

    def table(self, name):
        return _Q(self, name)

    def _run(self, q):
        if q.op != "select":
            raise AssertionError(f"unexpected op {q.op}")
        self.selects.append(q.table)
        rows = list(self.tables.get(q.table, []))
        rng = None
        lim = None
        for f in q.filters:
            kind = f[0]
            if kind == "eq":
                rows = [r for r in rows if str(r.get(f[1])) == str(f[2])]
            elif kind == "in":
                vals = {str(v) for v in (f[2] or [])}
                rows = [r for r in rows if str(r.get(f[1])) in vals]
            elif kind == "neq":
                rows = [r for r in rows if str(r.get(f[1])) != str(f[2])]
            elif kind == "order":
                rows.sort(key=lambda r: str(r.get(f[1]) or ""), reverse=bool(f[2]))
            elif kind == "range":
                rng = (f[1], f[2])
            elif kind == "limit":
                lim = f[1]
        if rng:
            rows = rows[rng[0]:rng[1] + 1]
        if lim is not None:
            rows = rows[:lim]
        return rows


def _points(did, pts, updated_at=T0):
    return {"discord_id": did, "points": pts, "updated_at": updated_at}


def _result(rid, mver, points, sealed_at=T0):
    return {"room_id": rid, "match_version": mver, "points": points,
            "sealed_at": sealed_at}


def _room(rid, status, fee, roster, mver=0):
    return {"id": rid, "short_code": rid, "status": status, "fee": fee,
            "roster": roster, "match_version": mver, "updated_at": T0}


def _run_sweep(monkeypatch, fake):
    monkeypatch.setattr(rr, "_sb", lambda: fake)
    buf = {}
    rc = rr.main()
    out = buf  # main prints; capture via capsys instead
    return rc


@pytest.fixture
def sweep(monkeypatch, capsys):
    def go(fake):
        monkeypatch.setattr(rr, "_sb", lambda: fake)
        rc = rr.main()
        captured = capsys.readouterr().out
        return rc, json.loads(captured)
    return go


# ---------------------------------------------------------------------------
# classifier (pure)
# ---------------------------------------------------------------------------

def test_classify_zero_is_ok():
    out = reconcile.classify_deltas({"a": 0})
    assert out["a"]["status"] == reconcile.STATUS_OK


def test_classify_positive_pending():
    out = reconcile.classify_deltas({"a": 60}, pending_keys={"a"})
    assert out["a"]["status"] == reconcile.STATUS_PENDING


def test_classify_positive_drift_grades():
    minor = reconcile.classify_deltas({"a": 4})["a"]
    material = reconcile.classify_deltas({"b": 40})["b"]
    assert minor["status"] == reconcile.STATUS_DRIFT and minor["grade"] == "minor"
    assert material["grade"] == "material"
    assert any("repair" in x for x in material["actions"])


def test_classify_negative_is_critical():
    out = reconcile.classify_deltas({"a": -30})
    assert out["a"]["status"] == reconcile.STATUS_CRITICAL
    assert "deficit" in (out["a"]["note"] or "")
    assert out["a"]["actions"]


def test_classify_reset_survivor_not_critical():
    out = reconcile.classify_deltas({"a": -30}, reset_players={"a"})
    assert out["a"]["status"] == reconcile.STATUS_DRIFT
    assert "pre-reset" in (out["a"]["note"] or "")
    assert out["a"]["actions"] == ["drain_ledger_rows"]


def test_classify_pure():
    deltas = {"a": 5, "b": -1}
    import copy
    snap = copy.deepcopy(deltas)
    r1 = reconcile.classify_deltas(deltas, pending_keys={"a"})
    r2 = reconcile.classify_deltas(deltas, pending_keys={"a"})
    assert deltas == snap and r1 == r2


# ---------------------------------------------------------------------------
# runner (fake IO)
# ---------------------------------------------------------------------------

def test_clean_store_zero_drift(sweep):
    fake = _FakeSB()
    fake.tables["ranked_points"] = [_points("A", 100), _points("B", 50)]
    fake.tables["ranked_results"] = [_result("R1", 1, {"A": 60, "B": 50}),
                                     _result("R1", 2, {"A": 40})]
    rc, rep = sweep(fake)
    assert rc == 0 and rep["ok"] is True
    assert rep["counts"][reconcile.STATUS_OK] == 2
    assert rep["counts"][reconcile.STATUS_DRIFT] == 0
    assert rep["counts"][reconcile.STATUS_CRITICAL] == 0
    assert rep["totals"]["delta"] == 0


def test_inflight_seal_pending(sweep):
    fake = _FakeSB()
    fake.tables["ranked_points"] = [_points("A", 160)]
    fake.tables["ranked_results"] = [_result("R1", 2, {"A": 100})]
    fake.tables["ranked_rooms"] = [_room("R1", "live", 0,
                                         [{"discord_id": "A"}], mver=3)]
    rc, rep = sweep(fake)
    assert rc == 0
    assert rep["counts"][reconcile.STATUS_PENDING] == 1
    row = rep["players"][0]
    assert row["discord_id"] == "A" and row["delta"] == 60


def test_pending_never_masks_deficit(sweep):
    fake = _FakeSB()
    fake.tables["ranked_points"] = [_points("A", 90)]
    fake.tables["ranked_results"] = [_result("R1", 2, {"A": 100})]
    fake.tables["ranked_rooms"] = [_room("R1", "live", 0,
                                         [{"discord_id": "A"}], mver=3)]
    rc, rep = sweep(fake)
    assert rep["counts"][reconcile.STATUS_CRITICAL] == 1


def test_fee_attributed_full_per_member(sweep):
    # fee 25 charged from both members; store reflects it -> zero drift
    fake = _FakeSB()
    fake.tables["ranked_points"] = [_points("A", 125), _points("B", 95)]
    fake.tables["ranked_results"] = [_result("R1", 1, {"A": 150, "B": 120})]
    fake.tables["ranked_rooms"] = [_room("R1", "ended", 25,
                                         [{"discord_id": "A"}, {"discord_id": "B"}])]
    rc, rep = sweep(fake)
    assert rep["counts"][reconcile.STATUS_OK] == 2
    assert rep["attribution"] == {"A": "entry fee attributed (25)",
                                  "B": "entry fee attributed (25)"}
    assert rep["context"]["fees_attributed"] == 2


def test_fee_clamped_no_overshoot(sweep):
    # deficit 300, fee 500 -> exactly 300 attributed, never 500
    # (over-attribution would flip the residual positive / hide real drift)
    fake = _FakeSB()
    fake.tables["ranked_points"] = [_points("A", 700)]
    fake.tables["ranked_results"] = [_result("R1", 1, {"A": 1000})]
    fake.tables["ranked_rooms"] = [_room("R1", "ended", 500, [{"discord_id": "A"}])]
    rc, rep = sweep(fake)
    assert rep["counts"][reconcile.STATUS_OK] == 1
    assert rep["counts"][reconcile.STATUS_CRITICAL] == 0
    assert rep["attribution"] == {"A": "entry fee attributed (300)"}


def test_fee_partial_leaves_tier_band_drift(sweep):
    # deficit 800, fee 500 -> residual -300 stays negative, lands in the
    # tier-fee explanation band (<= fee + 300) as minor drift, not critical
    fake = _FakeSB()
    fake.tables["ranked_points"] = [_points("A", 200)]
    fake.tables["ranked_results"] = [_result("R1", 1, {"A": 1000})]
    fake.tables["ranked_rooms"] = [_room("R1", "ended", 500, [{"discord_id": "A"}])]
    rc, rep = sweep(fake)
    row = rep["players"][0]
    assert row["delta"] == -300
    assert row["status"] == reconcile.STATUS_DRIFT and row["grade"] == "minor"
    assert rep["counts"][reconcile.STATUS_CRITICAL] == 0
    assert rep["attribution"] == {"A": "entry fee attributed (500)"}


def test_deficit_beyond_fees_is_critical(sweep):
    # deficit 2500; fee+max-tier explain 1750 max -> residual -2000 critical
    fake = _FakeSB()
    fake.tables["ranked_points"] = [_points("A", 100)]
    fake.tables["ranked_results"] = [_result("R1", 1, {"A": 2600})]
    fake.tables["ranked_rooms"] = [_room("R1", "ended", 500, [{"discord_id": "A"}])]
    rc, rep = sweep(fake)
    row = rep["players"][0]
    assert row["delta"] == -2000 and row["status"] == reconcile.STATUS_CRITICAL
    assert rep["counts"][reconcile.STATUS_CRITICAL] == 1
    assert rep["attribution"] == {"A": "entry fee attributed (500)"}


def test_positive_drift_reported(sweep):
    fake = _FakeSB()
    fake.tables["ranked_points"] = [_points("A", 140)]
    fake.tables["ranked_results"] = [_result("R1", 1, {"A": 100})]
    rc, rep = sweep(fake)
    assert rep["counts"][reconcile.STATUS_DRIFT] == 1
    row = rep["players"][0]
    assert row["store"] == 140 and row["ledger"] == 100 and row["delta"] == 40
    assert row["grade"] == "material"
    assert rep["totals"]["delta"] == 40


def test_minor_drift_grade(sweep):
    fake = _FakeSB()
    fake.tables["ranked_points"] = [_points("A", 104)]
    fake.tables["ranked_results"] = [_result("R1", 1, {"A": 100})]
    rc, rep = sweep(fake)
    assert rep["players"][0]["grade"] == "minor"


def test_reset_survivor_drains_not_critical(sweep):
    # most-recent store row sits at 0 (the wipe); A still has a pre-reset
    # ledger row the reset failed to drain
    fake = _FakeSB()
    fake.tables["ranked_points"] = [_points("A", 0, updated_at=T0)]
    fake.tables["ranked_results"] = [_result("R1", 9, {"A": 30},
                                             sealed_at=T_BEFORE)]
    rc, rep = sweep(fake)
    assert rep["context"]["reset_epoch"] == _epoch(T0)
    row = rep["players"][0]
    assert row["discord_id"] == "A" and row["delta"] == -30
    assert row["status"] == reconcile.STATUS_DRIFT
    assert row["actions"] == ["drain_ledger_rows"]


def test_post_reset_ledger_not_flagged_as_survivor(sweep):
    # ledger rows sealed AFTER the wipe are this season's legit income
    fake = _FakeSB()
    fake.tables["ranked_points"] = [_points("A", 100, updated_at=T_BEFORE)]
    fake.tables["ranked_results"] = [_result("R1", 1, {"A": 100}, sealed_at=T0)]
    rc, rep = sweep(fake)
    assert rep["counts"][reconcile.STATUS_OK] == 1


def test_paid_tourney_payout_context(sweep):
    fake = _FakeSB()
    fake.tables["ranked_points"] = [_points("A", 100)]
    fake.tables["ranked_results"] = [_result("R1", 1, {"A": 100})]
    fake.tables["bngc_ranked_tourneys"] = [{"id": 7, "points_paid": True}]
    fake.tables["bngc_ranked_tourney_registrations"] = [
        {"tourney_id": 7, "member_ids": ["A"], "status": "accepted"}]
    rc, rep = sweep(fake)
    assert rep["context"]["tourneys_paid_recent"] == 1
    assert rep["counts"][reconcile.STATUS_OK] == 1


def test_sweep_never_writes(sweep):
    # any write attempt raises inside the fake; a clean run proves read-only
    fake = _FakeSB()
    fake.tables["ranked_points"] = [_points("A", 100)]
    fake.tables["ranked_results"] = [_result("R1", 1, {"A": 100})]
    rc, rep = sweep(fake)
    assert rc == 0 and rep["ok"] is True
    assert set(fake.selects) <= {
        "ranked_points", "ranked_results", "ranked_rooms",
        "bngc_ranked_tourneys", "bngc_ranked_tourney_registrations"}


def test_read_failure_still_exits_zero(sweep):
    # ledger read dies -> report degrades gracefully (ledger empty), rc 0
    class _Boom(_FakeSB):
        def _run(self, q):
            if q.table == "ranked_results":
                raise RuntimeError("db down")
            return super()._run(q)
    rc, rep = sweep(_Boom())
    assert rc == 0
    assert any("ranked_results" in e for e in rep.get("read_errors", []))


def test_total_crash_still_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(rr, "_read_state",
                        lambda: (_ for _ in ()).throw(RuntimeError("nope")))
    rc = rr.main()
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is False
    assert "nothing was written" in out["note"]


def test_alert_only_when_drift(monkeypatch):
    calls = []
    monkeypatch.setattr(rr.urllib.request, "urlopen",
                        lambda req, timeout=5: calls.append(req) or _Noop())
    monkeypatch.setenv("RECONCILE_WEBHOOK_URL", "http://hook.test/x")
    clean = {"counts": {reconcile.STATUS_DRIFT: 0, reconcile.STATUS_CRITICAL: 0}}
    rr._alert(clean)
    assert not calls
    dirty = {"counts": {reconcile.STATUS_DRIFT: 2, reconcile.STATUS_CRITICAL: 1}}
    rr._alert(dirty)
    assert len(calls) == 1


class _Noop:
    def read(self):
        return b""
