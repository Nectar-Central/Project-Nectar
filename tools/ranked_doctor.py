"""Ranked-tourney doctor: find codeless/ambiguous/stale tourneys (READ-ONLY).

Shows why auto-tag does (or doesn't) fire and why a room code binds (or not).

Usage (run from the repo root):
    python tools/ranked_doctor.py                  # overview: all open tourneys, codeless spotlight
    python tools/ranked_doctor.py --did 12954786   # simulate auto-tag for one host
    python tools/ranked_doctor.py --code SKLH9     # who claims this room code?
    python tools/ranked_doctor.py --ended          # also list ended tourneys

Config: SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY, loaded from server/.env
(or root .env). Never writes — every query is a SELECT.

Exit codes: 0 ok, 1 findings need attention, 2 config/connection problem.
"""
import os
import sys
from pathlib import Path


def _load_env():
    try:
        from dotenv import load_dotenv
    except Exception:
        print("python-dotenv is not installed (pip install python-dotenv).")
        return False
    root = Path(__file__).resolve().parent.parent
    for p in (root / "server" / ".env", root / ".env"):
        try:
            if p.exists():
                load_dotenv(p, override=False)
        except Exception:
            pass
    return True


def _client():
    try:
        from supabase import create_client
    except Exception:
        print("supabase-py is not installed in this Python "
              "(use the project .venv: .venv\\Scripts\\python tools/ranked_doctor.py).")
        return None
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        print("Missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY "
              "(checked server/.env and root .env).")
        return None
    try:
        return create_client(url, key)
    except Exception as exc:
        print(f"Supabase connect failed: {exc}")
        return None


def _tourneys(sb, include_ended=False, limit=200):
    q = sb.table("bngc_ranked_tourneys").select(
        "id,code,name,status,hosts,created_by,series_len,created_at"
    ).order("id", desc=True).limit(limit)
    if not include_ended:
        q = q.neq("status", "ended")
    res = q.execute()
    return res.data or []


def _match_stats(sb, tids):
    """tourney_id -> {open, sealed, manual, max_version} (one query per table)."""
    out = {}
    if not tids:
        return out
    try:
        res = sb.table("bngc_ranked_tourney_matches").select(
            "tourney_id,status,version").in_("tourney_id", tids[:200]).limit(
            5000).execute()
    except Exception as exc:
        print(f"(match stats unavailable: {exc})")
        return out
    for m in (res.data or []):
        try:
            tid = int((m or {}).get("tourney_id"))
        except Exception:
            continue
        s = out.setdefault(tid, {"open": 0, "sealed": 0, "manual": 0,
                                 "max_version": -1})
        st = str((m or {}).get("status") or "")
        if st in s:
            s[st] += 1
        try:
            v = int((m or {}).get("version", -1))
            s["max_version"] = max(s["max_version"], v)
        except Exception:
            pass
    return out


def _fmt_code(code):
    c = str(code or "").strip()
    return c if c else "— codeless —"


def _show_tourney(t, ms):
    s = ms.get(int(t.get("id")), {})
    print(f"  #{t.get('id')}  {t.get('name') or '(no name)'}")
    print(f"      status={t.get('status')}  code={_fmt_code(t.get('code'))}  "
          f"series_len={t.get('series_len')}")
    print(f"      created_by={t.get('created_by')}  hosts={t.get('hosts')}")
    print(f"      matches: open={s.get('open', '?')} sealed={s.get('sealed', '?')} "
          f"manual={s.get('manual', '?')} max_version={s.get('max_version', '?')}")


def _mine(tourneys, did):
    did = str(did)
    out = []
    for t in tourneys:
        try:
            hosts = {str(h) for h in ((t or {}).get("hosts") or []) if h}
            if str((t or {}).get("created_by")) == did or did in hosts:
                out.append(t)
        except Exception:
            continue
    return out


def main() -> int:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0
    if not _load_env():
        return 2
    sb = _client()
    if sb is None:
        return 2

    did = None
    code = None
    include_ended = "--ended" in args
    try:
        if "--did" in args:
            did = str(args[args.index("--did") + 1])
        if "--code" in args:
            code = str(args[args.index("--code") + 1]).strip().upper()
    except Exception:
        print("Usage: --did <discord_id> | --code <ROOMCODE>")
        return 2

    try:
        tourneys = _tourneys(sb, include_ended)
    except Exception as exc:
        print(f"Tourney read failed: {exc}")
        return 2
    ms = _match_stats(sb, [t.get("id") for t in tourneys])

    problems = 0

    if code:
        print(f"Room code {code}:")
        claim = [t for t in tourneys
                 if str(t.get("code") or "").strip().upper() == code]
        if not claim:
            print("  nobody claims it (no open tourney tagged) -> ranked null, "
                  "auto-tag may fill a codeless one.")
            problems += 1
        elif len(claim) > 1:
            print(f"  CLAIMED BY {len(claim)} tourneys -> ambiguous, never binds:")
            problems += 1
        else:
            print("  claimed by exactly one tourney -> binds:")
        for t in claim:
            _show_tourney(t, ms)
        # ladder rooms with the same code (classic ranked path)
        try:
            rres = sb.table("ranked_rooms").select(
                "id,short_code,status,host_discord_id,match_version").eq(
                "short_code", code).in_("status", ["lobby", "live"]).limit(
                5).execute()
            rows = rres.data or []
            print(f"  ranked_rooms open rows: {len(rows)}")
            for r in rows:
                print(f"    {r.get('id')} status={r.get('status')} "
                      f"host={r.get('host_discord_id')} "
                      f"v={r.get('match_version')}")
        except Exception as exc:
            print(f"  (ranked_rooms read failed: {exc})")
        return 1 if problems else 0

    if did:
        mine = _mine(tourneys, did)
        print(f"Host {did} owns {len(mine)} open tourney(s):")
        for t in mine:
            _show_tourney(t, ms)
        codeless = [t for t in mine
                    if not str(t.get("code") or "").strip()]
        print()
        if len(codeless) == 1:
            print(f"Verdict: auto-tag WILL fire -> tid={codeless[0].get('id')} "
                  f"gets your next created room's code.")
        elif not codeless:
            print("Verdict: nothing to tag (all yours have codes). "
                  "New rooms bind only via matching code.")
            problems += 1
        else:
            print(f"Verdict: auto-tag STAYS MANUAL — {len(codeless)} codeless "
                  f"tourney(s): {[t.get('id') for t in codeless]}. "
                  f"End the stale one(s) or tag by hand.")
            problems += 1
        return 1 if problems else 0

    # overview
    print(f"Open tourneys: {len(tourneys)}")
    codeless = [t for t in tourneys
                if not str(t.get("code") or "").strip()]
    print(f"Codeless: {len(codeless)}")
    print()
    for t in tourneys:
        _show_tourney(t, ms)
    # duplicate codes across open tourneys
    by_code: dict = {}
    for t in tourneys:
        c = str(t.get("code") or "").strip().upper()
        if c:
            by_code.setdefault(c, []).append(t.get("id"))
    dupes = {c: ids for c, ids in by_code.items() if len(ids) > 1}
    print()
    if dupes:
        print("DUPLICATE codes (ambiguous, never bind):")
        for c, ids in dupes.items():
            print(f"  {c} -> tourneys {ids}")
        problems += 1
    else:
        print("No duplicate codes.")
    if codeless:
        print(f"Codeless tourney ids: {[t.get('id') for t in codeless]} "
              f"(auto-tag needs exactly one of these per host).")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
