"""
coins.py — GET/POST /api/coins/* — Nectar Coins economy.

  GET  /api/coins/balance?discord_id=xxx
  POST /api/coins/daily        {discord_id}
  POST /api/coins/flip         {discord_id, amount, choice: Heads|Tails}
  POST /api/coins/slots        {discord_id, amount}
  POST /api/coins/dice         {discord_id, amount, choice: Over|Under}
  POST /api/coins/pay          {from_discord_id, to_discord_id, amount}
  GET  /api/coins/ledger?discord_id=xxx&limit=25
  GET  /api/coins/leaderboard?limit=10

Daily claims advance the shared daily_streak / last_daily_claim columns
(the same streak /streaks shows) and pay a random 1-5 coins.
All gambling resolves server-side (no client-picked outcome).
Every mutation appends to bngc_coin_ledger. Never raises to the client:
missing tables/columns degrade to 500 with a migration hint.
"""

from __future__ import annotations

import secrets
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase, db_io
from ..lib.logger import log_sys, log_err
from ..lib import coins as coins_lib
from ..lib import streaks as streaks_lib

router = APIRouter()

_MISSING_HINT = "Database migration pending — ask a host to apply add_nectar_coins_v252.sql."


def _missing_table(exc: Exception) -> bool:
    try:
        s = str(exc or "")
    except Exception:
        return False
    sl = s.lower()
    return ("nectar_coins" in sl or "bngc_coin_ledger" in sl
            or "42703" in s or "pgrst205" in s or "does not exist" in sl)


def _user_sync(discord_id: str) -> dict | None:
    res = supabase.table("bngc_users").select(
        "discord_id,discord_username,nectar_coins,nectar_total_earned,"
        "nectar_total_wagered,daily_streak,last_daily_claim"
    ).eq("discord_id", str(discord_id)).limit(1).execute()
    rows = res.data or []
    return rows[0] if rows else None


# Ledger reason sets: wins credit nectar_total_earned, every gamble
# (win, loss or push/refund) counts its stake toward total_wagered.
# Match-bet reasons live here too so bets share one accounting brain;
# bet_won passes earned_delta=profit (payout includes the returned stake).
_EARN_REASONS = frozenset({
    "daily", "flip_win", "pay_received", "admin_adjust",
    "slots_win", "dice_win", "bet_won",
})
_WAGER_REASONS = frozenset({
    "flip_win", "flip_loss",
    "slots_win", "slots_loss", "slots_refund",
    "dice_win", "dice_loss", "dice_push",
    "bet_placed", "bet_won",
})


def _ledger_insert_sync(discord_id: str, delta: int, balance_after: int,
                        reason: str, meta: dict | None = None) -> None:
    try:
        supabase.table("bngc_coin_ledger").insert({
            "discord_id": str(discord_id),
            "delta": int(delta),
            "balance_after": int(balance_after),
            "reason": str(reason),
            "meta": dict(meta or {}),
        }).execute()
    except Exception:
        pass  # ledger is best-effort; balance already applied


def _apply_delta_sync(discord_id: str, delta: int, reason: str,
                      meta: dict | None = None,
                      earned_delta: int | None = None) -> dict:
    """Apply a signed delta; returns fresh {balance, earned, wagered}.

    earned_delta overrides the earned credit (match-bet wins: payout
    includes the returned stake, so only the profit counts as earned).
    None keeps the legacy behavior (full positive delta).
    """
    user = _user_sync(discord_id)
    if not user:
        raise LookupError("user not found")
    bal = int(user.get("nectar_coins") or 0) + int(delta)
    if bal < 0:
        raise ValueError("Insufficient balance.")
    earned = int(user.get("nectar_total_earned") or 0)
    wagered = int(user.get("nectar_total_wagered") or 0)
    if int(delta) > 0 and reason in _EARN_REASONS:
        try:
            earned += int(delta) if earned_delta is None else int(earned_delta)
        except Exception:
            earned += int(delta)
    meta = dict(meta or {})
    if earned_delta is not None:
        # Stamp profit for the daily wager-earnings cap (ledger queryable).
        try:
            meta.setdefault("earned", int(earned_delta))
        except Exception:
            pass
    if reason in _WAGER_REASONS:
        # amount risked counts as wagered even on a win
        try:
            wagered += int((meta or {}).get("bet") or abs(int(delta)))
        except Exception:
            wagered += abs(int(delta))
    supabase.table("bngc_users").update({
        "nectar_coins": bal,
        "nectar_total_earned": earned,
        "nectar_total_wagered": wagered,
    }).eq("discord_id", str(discord_id)).execute()
    _ledger_insert_sync(discord_id, int(delta), bal, reason, meta)
    return {"balance": bal, "earned": earned, "wagered": wagered}


@router.get("/api/coins/balance")
async def coins_balance(request: Request):
    did = (request.query_params.get("discord_id")
           or request.query_params.get("did") or "").strip()
    if not did:
        return JSONResponse({"error": "discord_id required"}, status_code=400)
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    try:
        user = await db_io(_user_sync, did)
    except Exception as e:
        if _missing_table(e):
            return JSONResponse({"error": _MISSING_HINT}, status_code=500)
        return JSONResponse({"error": str(e)[:160]}, status_code=500)
    if not user:
        return JSONResponse({"error": "user not found"}, status_code=404)
    return JSONResponse({
        "discord_id": user.get("discord_id"),
        "balance": int(user.get("nectar_coins") or 0),
        "total_earned": int(user.get("nectar_total_earned") or 0),
        "total_wagered": int(user.get("nectar_total_wagered") or 0),
        "daily_streak": int(user.get("daily_streak") or 0),
        "last_daily_claim": user.get("last_daily_claim"),
    })


@router.post("/api/coins/daily")
async def coins_daily(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    did = str((body or {}).get("discord_id") or (body or {}).get("did") or "").strip()
    if not did:
        return JSONResponse({"error": "discord_id required"}, status_code=400)
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    try:
        def _claim():
            user = _user_sync(did)
            if not user:
                raise LookupError("user not found")
            today = date.today()
            verdict = streaks_lib.daily_update(user.get("last_daily_claim"), today)
            _sig, _bonus, claimed = verdict
            if not claimed:
                return {"already": True, "user": user}
            if _sig == -1:  # continue: caller increments
                new_streak, _arena_bonus = streaks_lib.daily_continue(user.get("daily_streak"))
            else:
                new_streak = int(_sig or 1)
            payout = coins_lib.daily_coins_for(new_streak)
            supabase.table("bngc_users").update({
                "daily_streak": new_streak,
                "last_daily_claim": today.isoformat(),
            }).eq("discord_id", str(did)).execute()
            applied = _apply_delta_sync(did, payout, "daily",
                                        {"streak": new_streak})
            return {"already": False, "streak": new_streak,
                    "payout": payout, **applied}
        out = await db_io(_claim)
    except LookupError:
        return JSONResponse({"error": "user not found"}, status_code=404)
    except Exception as e:
        if _missing_table(e):
            return JSONResponse({"error": _MISSING_HINT}, status_code=500)
        return JSONResponse({"error": str(e)[:160]}, status_code=500)
    if out.get("already"):
        u = out["user"]
        return JSONResponse({"claimed": False,
                             "balance": int(u.get("nectar_coins") or 0),
                             "daily_streak": int(u.get("daily_streak") or 0),
                             "message": "Already claimed today — come back tomorrow."})
    try:
        log_sys(f"[coins] daily did={did[:6]} streak={out['streak']} +{out['payout']}")
    except Exception:
        pass
    return JSONResponse({"claimed": True, "balance": out["balance"],
                         "payout": out["payout"], "streak": out["streak"]})


@router.post("/api/coins/flip")
async def coins_flip(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    did = str((body or {}).get("discord_id") or "").strip()
    choice = coins_lib.normalize_choice((body or {}).get("choice"))
    try:
        amount = int((body or {}).get("amount") or 0)
    except Exception:
        amount = 0
    if not did:
        return JSONResponse({"error": "discord_id required"}, status_code=400)
    if not choice:
        return JSONResponse({"error": "choice must be Heads or Tails"}, status_code=400)
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    try:
        def _flip():
            user = _user_sync(did)
            if not user:
                raise LookupError("user not found")
            ok, err = coins_lib.validate_bet(user.get("nectar_coins"), amount)
            if not ok:
                raise ValueError(err or "Invalid bet.")
            result = secrets.choice(["Heads", "Tails"])
            delta = coins_lib.resolve_flip(amount, choice, result)
            applied = _apply_delta_sync(did, delta,
                                        "flip_win" if delta > 0 else "flip_loss",
                                        {"bet": int(amount), "choice": choice,
                                         "result": result})
            return {"result": result, "delta": delta, **applied}
        out = await db_io(_flip)
    except LookupError:
        return JSONResponse({"error": "user not found"}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)[:160]}, status_code=400)
    except Exception as e:
        if _missing_table(e):
            return JSONResponse({"error": _MISSING_HINT}, status_code=500)
        return JSONResponse({"error": str(e)[:160]}, status_code=500)
    out.update({"choice": choice, "bet": int(amount), "won": bool(out["delta"] > 0)})
    return JSONResponse(out)


@router.post("/api/coins/slots")
async def coins_slots(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    did = str((body or {}).get("discord_id") or "").strip()
    try:
        amount = int((body or {}).get("amount") or 0)
    except Exception:
        amount = 0
    if not did:
        return JSONResponse({"error": "discord_id required"}, status_code=400)
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    try:
        def _spin():
            user = _user_sync(did)
            if not user:
                raise LookupError("user not found")
            ok, err = coins_lib.validate_bet(user.get("nectar_coins"), amount)
            if not ok:
                raise ValueError(err or "Invalid bet.")
            symbols = coins_lib.spin_slots()
            kind = coins_lib.slots_kind(symbols)
            delta = coins_lib.slots_delta(amount, symbols)
            reason = {"trips": "slots_win", "pair": "slots_refund",
                      "loss": "slots_loss"}[kind]
            applied = _apply_delta_sync(did, delta, reason,
                                        {"bet": int(amount), "symbols": symbols,
                                         "kind": kind})
            return {"symbols": symbols, "kind": kind,
                    "delta": delta, **applied}
        out = await db_io(_spin)
    except LookupError:
        return JSONResponse({"error": "user not found"}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)[:160]}, status_code=400)
    except Exception as e:
        if _missing_table(e):
            return JSONResponse({"error": _MISSING_HINT}, status_code=500)
        return JSONResponse({"error": str(e)[:160]}, status_code=500)
    out.update({"bet": int(amount), "won": bool(out["delta"] > 0),
                "refund": bool(out["delta"] == 0)})
    return JSONResponse(out)


@router.post("/api/coins/dice")
async def coins_dice(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    did = str((body or {}).get("discord_id") or "").strip()
    choice = coins_lib.normalize_dice_choice((body or {}).get("choice"))
    try:
        amount = int((body or {}).get("amount") or 0)
    except Exception:
        amount = 0
    if not did:
        return JSONResponse({"error": "discord_id required"}, status_code=400)
    if not choice:
        return JSONResponse({"error": "choice must be Over or Under"}, status_code=400)
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    try:
        def _roll():
            user = _user_sync(did)
            if not user:
                raise LookupError("user not found")
            ok, err = coins_lib.validate_bet(user.get("nectar_coins"), amount)
            if not ok:
                raise ValueError(err or "Invalid bet.")
            roll = coins_lib.roll_dice()
            delta = coins_lib.dice_delta(amount, choice, roll)
            reason = ("dice_win" if delta > 0
                      else "dice_push" if delta == 0 else "dice_loss")
            applied = _apply_delta_sync(did, delta, reason,
                                        {"bet": int(amount), "choice": choice,
                                         "roll": roll})
            return {"roll": roll, "delta": delta, **applied}
        out = await db_io(_roll)
    except LookupError:
        return JSONResponse({"error": "user not found"}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)[:160]}, status_code=400)
    except Exception as e:
        if _missing_table(e):
            return JSONResponse({"error": _MISSING_HINT}, status_code=500)
        return JSONResponse({"error": str(e)[:160]}, status_code=500)
    out.update({"choice": choice, "bet": int(amount),
                "won": bool(out["delta"] > 0),
                "push": bool(out["delta"] == 0)})
    return JSONResponse(out)


@router.post("/api/coins/pay")
async def coins_pay(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    frm = str((body or {}).get("from_discord_id") or (body or {}).get("from") or "").strip()
    to = str((body or {}).get("to_discord_id") or (body or {}).get("to") or "").strip()
    try:
        amount = int((body or {}).get("amount") or 0)
    except Exception:
        amount = 0
    if not frm or not to:
        return JSONResponse({"error": "from_discord_id and to_discord_id required"},
                            status_code=400)
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    try:
        def _pay():
            sender = _user_sync(frm)
            if not sender:
                raise LookupError("sender not found")
            dest = _user_sync(to)
            if not dest:
                raise LookupError("recipient not found")
            ok, err = coins_lib.validate_transfer(sender.get("nectar_coins"),
                                                  amount, frm, to)
            if not ok:
                raise ValueError(err or "Invalid transfer.")
            _apply_delta_sync(frm, -int(amount), "pay_sent", {"to": to})
            applied = _apply_delta_sync(to, int(amount), "pay_received", {"from": frm})
            fresh = _user_sync(frm)
            return {"sent": int(amount), "to": to,
                    "sender_balance": int((fresh or {}).get("nectar_coins") or 0),
                    "recipient_balance": applied["balance"]}
        out = await db_io(_pay)
    except LookupError as e:
        return JSONResponse({"error": str(e)[:160]}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)[:160]}, status_code=400)
    except Exception as e:
        if _missing_table(e):
            return JSONResponse({"error": _MISSING_HINT}, status_code=500)
        return JSONResponse({"error": str(e)[:160]}, status_code=500)
    return JSONResponse(out)


@router.get("/api/coins/ledger")
async def coins_ledger(request: Request):
    did = (request.query_params.get("discord_id")
           or request.query_params.get("did") or "").strip()
    try:
        limit = max(1, min(100, int(request.query_params.get("limit") or 25)))
    except Exception:
        limit = 25
    if not did:
        return JSONResponse({"error": "discord_id required"}, status_code=400)
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    try:
        def _q():
            return supabase.table("bngc_coin_ledger").select(
                "delta,balance_after,reason,meta,created_at").eq(
                "discord_id", str(did)).order(
                "created_at", desc=True).limit(limit).execute()
        res = await db_io(_q)
        return JSONResponse({"discord_id": did, "entries": res.data or []})
    except Exception as e:
        if _missing_table(e):
            return JSONResponse({"error": _MISSING_HINT}, status_code=500)
        return JSONResponse({"error": str(e)[:160]}, status_code=500)


@router.get("/api/coins/leaderboard")
async def coins_leaderboard(request: Request):
    try:
        limit = max(1, min(25, int(request.query_params.get("limit") or 10)))
    except Exception:
        limit = 10
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    try:
        def _q():
            return supabase.table("bngc_users").select(
                "discord_id,discord_username,game_username,nectar_coins"
            ).order("nectar_coins", desc=True).limit(limit).execute()
        res = await db_io(_q)
        return JSONResponse({"entries": res.data or []})
    except Exception as e:
        if _missing_table(e):
            return JSONResponse({"error": _MISSING_HINT}, status_code=500)
        return JSONResponse({"error": str(e)[:160]}, status_code=500)
