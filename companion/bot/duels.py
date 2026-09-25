"""PvP Nectar-Coin duel wagers — targeted (@opponent) + open (first-join-locks 1v1).

Economy is reused from the existing coins system: balances live on
``bngc_users.nectar_coins`` and every escrow/refund/payout flows through
``database.add_coin_delta`` (ledger reasons duel_escrow / duel_refund /
duel_win). This module only adds wager state in ``bngc_duel_wagers``
(see supabase/add_duel_wagers_v256.sql) plus threads, embeds and a
60s auto-resolver that reads match proof from ``bngc_game_events``.

Rules (locked spec):
- Private rooms only: a match counts only when event_data carries a
  short_code/code AND gm matches the wager mode AND both duelists appear
  in the same (room_id, match_version).
- zw_lg / bf -> first to X match wins. DF -> first to X kills (cumulative).
- Eligibility ("everything good"): registered, game_username set, not
  locked, active session newer than 2 days, balance covers stake, no
  other open/pending/active wager.
- Open wagers expire 15 min after creation; targeted challenges expire
  15 min after creation; active duels stall-refund after 24h.
- Mobile duels: players without a companion (no session/online) may JOIN
  open wagers or ACCEPT targeted challenges when the creator is live +
  tracked; scored by game name against the tracked side's room events
  (most kills takes each sealed match).
- Threads are always private (duelists + mods with Manage Threads).
  Public fallback (unboosted servers) stays spectator read-only via
  message deletion.
- Room codes are typed in the thread by a duelist (whole message is the
  5-char code) — re-typable until the first match seals.

Wiring: bot.py calls ``register(bot.tree, bot)`` at import time, adds
``DuelView()`` as a persistent view in on_ready, and starts
``duel_expiry_loop`` + ``duel_resolver_loop``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import discord
from discord import app_commands

import database as db

# The bot process may load env from the repo root — read our own .env too so
# WAGER_CHANNEL_ID (and friends) resolve no matter the working directory.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except Exception:
    pass

print("[duels] module loaded", flush=True)

# ---------------------------------------------------------------------------
# Config (env, never hardcoded IDs except guild fallback shared with bot.py)
# ---------------------------------------------------------------------------

def _wager_channel_id() -> str:
    """Wager channel id, read at call time (env may load after import). Never raises."""
    try:
        return str(os.getenv("WAGER_CHANNEL_ID", "") or "").strip()
    except Exception:
        return ""


# Channel holding the duel rules every wager embed links to.
WAGER_RULES_CHANNEL_ID = "1552686996420427786"


def _rules_channel_id() -> str:
    """Rules channel id, read at call time; blank env falls back to the default."""
    try:
        return (str(os.getenv("WAGER_RULES_CHANNEL_ID", "") or "").strip()
                or WAGER_RULES_CHANNEL_ID)
    except Exception:
        return WAGER_RULES_CHANNEL_ID


WAGER_MIN_STAKE = int(os.getenv("WAGER_MIN_STAKE", "1") or 1)
WAGER_MAX_STAKE = int(os.getenv("WAGER_MAX_STAKE", "10000") or 10000)
OPEN_EXPIRY_SEC = int(os.getenv("WAGER_OPEN_EXPIRY_SEC", "900") or 900)  # 15 min
ACCEPT_TIMEOUT_SEC = int(os.getenv("WAGER_ACCEPT_TIMEOUT_SEC", "900") or 900)
STALL_AFTER_SEC = int(os.getenv("WAGER_STALL_SEC", "86400") or 86400)  # 24h
SESSION_TTL_SEC = 2 * 86400  # same 2-day session rule as the rest of the bot
WAGER_ONLINE_SEC = int(os.getenv("WAGER_ONLINE_SEC", "300") or 300)  # must be in-game to wager
WAGER_EARN_CAP = int(os.getenv("WAGER_EARN_CAP", "50") or 50)  # max duel/bet profit per day
# How often live duels are re-scored. This is the whole match-end -> wager
# resolved latency: the processor writes the win row within a second, then the
# board/thread update lands on the next resolver tick. 60s felt like a bug to
# players, so it defaults to 20s (env-tunable).
SCORE_INTERVAL_SEC = int(os.getenv("WAGER_SCORE_INTERVAL_SEC", "20") or 20)

ACTIVE_STATUSES = ("open", "pending", "active")

# In-memory spectator enforcement (hydrated by the loops, restart-safe via DB).
PUBLIC_THREADS: set[str] = set()
THREAD_PAIRS: dict[str, tuple[str, str]] = {}

# ---------------------------------------------------------------------------
# Pure helpers (no I/O — unit-testable, never raise)
# ---------------------------------------------------------------------------

MODE_CHOICES = (
    ("Late Game Zonewars", "zw_lg"),
    ("Boxfights", "bf"),
    ("Free Mode", "df"),
)


def normalize_mode(raw) -> str | None:
    """zw_lg | bf | df or None. bf covers bf/bf2v2/BF2V2 family. Never raises."""
    try:
        m = str(raw or "").strip().lower()
    except Exception:
        return None
    if m in ("zw_lg", "zwlg", "late game zonewars"):
        return "zw_lg"
    if m in ("bf", "bf2v2", "bf_2v2", "boxfights", "boxfights 2v2"):
        return "bf"
    if m in ("df", "free mode"):
        return "df"
    return None


def race_for_mode(mode: str) -> str:
    """wins for zw_lg/bf, kills for df. Never raises."""
    try:
        return "kills" if str(mode or "").lower() == "df" else "wins"
    except Exception:
        return "wins"


def gm_matches(mode: str, gm) -> bool:
    """True when a game event's gm belongs to the wager mode. Never raises."""
    try:
        g = str(gm or "").strip().lower()
        m = str(mode or "").strip().lower()
        if m == "zw_lg":
            return g == "zw_lg"
        if m == "bf":
            return g in ("bf", "bf2v2", "bf_2v2")
        if m == "df":
            return g == "df"
        return False
    except Exception:
        return False


def validate_stake(amount) -> tuple[bool, str | None]:
    """0 = for-fun duel (no escrow, no pot); otherwise 1..MAX. Never raises."""
    try:
        amt = int(amount)
    except Exception:
        return False, "Stake must be a whole number."
    if amt < 0:
        return False, "Stake can't be negative (0 = for fun)."
    if amt > WAGER_MAX_STAKE:
        return False, f"Maximum stake is {WAGER_MAX_STAKE:,} coins."
    return True, None


def validate_first_to(n) -> tuple[bool, str | None]:
    """first_to must be a positive whole number (no upper cap). Never raises."""
    try:
        v = int(n)
    except Exception:
        return False, "first_to must be a whole number."
    if v < 1:
        return False, "first_to must be at least 1."
    return True, None


def validate_room_code(code) -> tuple[bool, str | None]:
    """Optional 5-char pID. Returns (ok, error). Never raises."""
    try:
        if code is None or str(code).strip() == "":
            return True, None
        c = str(code).strip().upper()
        if len(c) == 5 and c.isalnum():
            return True, None
        return False, "Room code must be 5 letters/digits (your private pID)."
    except Exception:
        return False, "Room code must be 5 letters/digits."


def short_wager_id(wager_id: str) -> str:
    """First 8 chars for display. Never raises."""
    try:
        return str(wager_id or "")[:8]
    except Exception:
        return "?"


def score_match(kills_c: int, kills_o: int, win_id, creator_id, opponent_id,
                race: str = "wins", opponent_win: bool = False):
    """Decide one sealed (room_id, version).

    race="wins" (zw_lg/bf): ONLY a win row decides the match. A version with
    no win row is a draw — kills must NEVER hand out a match win in a wins
    race (kills are a tiebreak display, not the objective; falling back to
    them let a kills-decided seal inflate a first-to-N match-wins score).
    opponent_win=True is the mobile path: the opponent has no capture, so the
    server never wrote a win row for them. The tracked player's side is
    authoritative (no win row = they did not win); when their last roster
    combat event in that version is "opponent eliminated tracked player", the
    round is the opponent's. Ignored entirely in a kills race.
    race="kills" (df): higher kills wins, equal kills is a draw.
    Returns the winner's discord_id or None for a draw. Never raises.
    """
    try:
        if str(race or "wins") == "kills":
            kc, ko = int(kills_c or 0), int(kills_o or 0)
            if kc > ko:
                return str(creator_id)
            if ko > kc:
                return str(opponent_id)
            return None
        # wins race
        if win_id and str(win_id) == str(creator_id):
            return str(creator_id)
        if win_id and str(win_id) == str(opponent_id):
            return str(opponent_id)
        if opponent_win:
            return str(opponent_id)
        return None
    except Exception:
        return None


def _wdebug() -> bool:
    """Verbose per-tick wager tracing (WAGER_DEBUG=1). Anomaly lines are
    always printed; this only adds the noisy per-tick detail. Never raises."""
    try:
        return str(os.getenv("WAGER_DEBUG", "") or "").strip() in ("1", "true", "yes")
    except Exception:
        return False


def _wlog(msg):
    """Debug-gated wager line. Never raises."""
    if _wdebug():
        try:
            print(f"[duels] {msg}")
        except Exception:
            pass


def _match_sealable(m: dict, race: str = "wins") -> bool:
    """A grouped version worth sealing.

    wins race: only a real win row seals (a kills-only or leave-noise group
    is a Draw at best — invisible, so kills can never hand out a match win),
    plus the mobile opponent_win inference (their win row can never exist).
    kills race: real combat seals.
    Never raises.
    """
    try:
        if str(race or "wins") == "kills":
            return (int((m or {}).get("kills_c") or 0) + int((m or {}).get("kills_o") or 0)) > 0
        return bool((m or {}).get("win_id")) or bool((m or {}).get("opponent_win"))
    except Exception:
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value) -> datetime | None:
    try:
        if not value:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Supabase access (via database._sb_call so the event loop never blocks)
# ---------------------------------------------------------------------------

def _sb():
    if not db.USE_SUPABASE or not db.supabase:
        return None
    return db.supabase


async def _sb_first(table: str, select: str, filters: list[tuple[str, str, object]]):
    """First matching row or None. Never raises."""
    sb = _sb()
    if sb is None:
        return None
    try:
        q = sb.table(table).select(select)
        for col, op, val in filters:
            if op == "eq":
                q = q.eq(col, val)
            elif op == "like":
                q = q.like(col, val)
            elif op == "in":
                q = q.in_(col, val)
        r = await db._sb_call(lambda: q.limit(1).execute())
        rows = (r.data if hasattr(r, "data") else []) or []
        return rows[0] if rows else None
    except Exception as e:
        print(f"[duels] _sb_first {table} failed: {e}")
        return None


async def get_wager(wager_id: str, include_ended: bool = False):
    """Fetch one wager by full id or id prefix. Never raises (None on miss).

    NOTE: ``id`` is uuid — Postgres has no LIKE/~~ operator for uuid, so
    prefix matching is done in Python over recent non-terminal rows (small
    table, single round trip) instead of a server-side like().

    include_ended also searches cancelled/expired rows, which the default
    deliberately skips: /wager reset is the one command whose whole job is a
    closed wager, and an 8-char id would otherwise never resolve.
    """
    sb = _sb()
    if sb is None or not wager_id:
        return None
    try:
        wid = str(wager_id).strip().lower()
        if len(wid) >= 36:
            return await _sb_first("bngc_duel_wagers", "*",
                                   [("id", "eq", wid)])
        statuses = list(ACTIVE_STATUSES) + ["resolved"]
        if include_ended:
            statuses = list(ENDED_STATUSES) + statuses
        r = await db._sb_call(lambda: sb.table("bngc_duel_wagers").select("*")
                              .in_("status", statuses)
                              .order("created_at", desc=True).limit(100).execute())
        rows = (r.data if hasattr(r, "data") else []) or []
        hits = [x for x in rows if str((x or {}).get("id") or "").lower().startswith(wid)]
        return hits[0] if hits else None
    except Exception as e:
        print(f"[duels] get_wager failed: {e}")
        return None


async def find_wager_by_thread(thread_id: str):
    """Newest non-terminal wager bound to a thread. Never raises."""
    sb = _sb()
    if sb is None or not thread_id:
        return None
    try:
        r = await db._sb_call(lambda: sb.table("bngc_duel_wagers").select("*")
                              .eq("thread_id", str(thread_id))
                              .order("created_at", desc=True).limit(1).execute())
        rows = (r.data if hasattr(r, "data") else []) or []
        return rows[0] if rows else None
    except Exception as e:
        print(f"[duels] find_wager_by_thread failed: {e}")
        return None


async def find_wager_by_message(message_id: str):
    """Open/pending wager for a board message (Join button). Never raises."""
    sb = _sb()
    if sb is None or not message_id:
        return None
    try:
        r = await db._sb_call(lambda: sb.table("bngc_duel_wagers").select("*")
                              .eq("message_id", str(message_id))
                              .in_("status", ("open", "pending"))
                              .order("created_at", desc=True).limit(1).execute())
        rows = (r.data if hasattr(r, "data") else []) or []
        return rows[0] if rows else None
    except Exception as e:
        print(f"[duels] find_wager_by_message failed: {e}")
        return None


def is_busy_row(row, uid: str, ignore_id: str = "") -> bool:
    """Does this wager row block `uid` from starting/joining another duel?

    An incoming PENDING challenge (someone challenged YOU, it is not yours
    yet) is NOT a conflict — otherwise accepting it fails the very eligibility
    check it has to pass, and the challenged player cannot run their own duel
    while an offer is pending. Busy = your own open/pending wager, or one you
    have already joined. `ignore_id` skips the wager being acted on (Accept).
    Never raises.
    """
    try:
        if not isinstance(row, dict):
            return False
        status = str(row.get("status") or "").lower()
        if status not in ACTIVE_STATUSES:
            return False
        rid = str(row.get("id") or "")
        if ignore_id and rid and rid == str(ignore_id):
            return False
        me = str(uid or "")
        if str(row.get("creator_id") or "") == me:
            return True
        if str(row.get("opponent_id") or "") == me:
            # Pending = an offer waiting for their answer, not their duel.
            return status != "pending"
        return False
    except Exception:
        return False


def challenge_dm_text(creator_name, opponent_name, first_to, pot, stake,
                       board_mention, wid_short) -> str:
    """DM sent to a targeted player instead of a thread. Never raises."""
    try:
        cname = str(creator_name or "Someone")
        oname = str(opponent_name or "you")
        try:
            ft = int(first_to or 1)
        except Exception:
            ft = 1
        try:
            p = int(pot or 0)
            st = int(stake or 0)
        except Exception:
            p = st = 0
        money = (f"**{p:,}** Nectar Coins pot ({st:,} each)" if p > 0
                 else "for fun — no pot, no stakes")
        return (f"⚔️ **{cname}** challenged you to a Nectar duel "
                f"(first to {ft}, {money}).\n"
                f"Hit **Accept** on the wager in {board_mention} to lock it in "
                f"(`{wid_short}`) — the duel thread opens after you accept. "
                f"You have 15 minutes; declining refunds the creator.")
    except Exception:
        return "You have been challenged to a Nectar duel — check the wager channel."


async def get_user_active(discord_id: str, *, ignore_id: str = ""):
    """A wager that blocks this user from starting/joining another duel.

    Uses is_busy_row, so an incoming PENDING challenge (they were challenged,
    it is not their duel yet) does not count — otherwise accepting the
    challenge fails the very eligibility check it has to pass. `ignore_id`
    skips the wager being acted on. Never raises.
    """
    sb = _sb()
    if sb is None or not discord_id:
        return None
    try:
        uid = str(discord_id)
        r = await db._sb_call(lambda: sb.table("bngc_duel_wagers")
                              .select("id,status,creator_id,opponent_id")
                              .in_("status", list(ACTIVE_STATUSES))
                              .or_(f"creator_id.eq.{uid},opponent_id.eq.{uid}")
                              .order("created_at", desc=True).limit(20).execute())
        for row in ((r.data if hasattr(r, "data") else []) or []):
            if is_busy_row(row, uid, ignore_id):
                return row
        return None
    except Exception as e:
        print(f"[duels] get_user_active failed: {e}")
        return None


async def list_open_wagers(guild_id: str, limit: int = 10):
    """Open board wagers for /wager-board. Never raises."""
    sb = _sb()
    if sb is None:
        return []
    try:
        r = await db._sb_call(lambda: sb.table("bngc_duel_wagers").select("*")
                              .eq("guild_id", str(guild_id or ""))
                              .eq("kind", "open").eq("status", "open")
                              .order("created_at", desc=False).limit(int(limit)).execute())
        return (r.data if hasattr(r, "data") else []) or []
    except Exception as e:
        print(f"[duels] list_open_wagers failed: {e}")
        return []


async def create_wager_row(payload: dict):
    """Insert a wager row. Returns (row, None) or (None, err). Never raises."""
    sb = _sb()
    if sb is None:
        return None, "Database not configured — Supabase is required for wagers."
    try:
        r = await db._sb_call(lambda: sb.table("bngc_duel_wagers").insert(payload).execute())
        rows = (r.data if hasattr(r, "data") else []) or []
        if not rows:
            return None, "Could not create the wager — try again."
        print(f"[duels] created {rows[0].get('id', '?')[:8]} "
              f"{payload.get('kind')} {payload.get('stake')} stake")
        return rows[0], None
    except Exception as e:
        print(f"[duels] create_wager_row failed: {e}")
        return None, "Could not create the wager — try again."


async def update_wager(wager_id: str, fields: dict) -> bool:
    """Patch a wager row. Returns ok. Never raises."""
    sb = _sb()
    if sb is None or not wager_id:
        return False
    try:
        await db._sb_call(lambda: sb.table("bngc_duel_wagers").update(fields)
                          .eq("id", str(wager_id)).execute())
        return True
    except Exception as e:
        print(f"[duels] update_wager failed: {e}")
        return False


async def active_wagers_for_loop(limit: int = 50):
    """Active duels needing scoring. Never raises."""
    sb = _sb()
    if sb is None:
        return []
    try:
        r = await db._sb_call(lambda: sb.table("bngc_duel_wagers").select("*")
                              .eq("status", "active")
                              .order("active_since", desc=False).limit(int(limit)).execute())
        return (r.data if hasattr(r, "data") else []) or []
    except Exception as e:
        print(f"[duels] active_wagers_for_loop failed: {e}")
        return []


async def expirable_wagers():
    """Open/pending wagers whose expires_at passed. Never raises."""
    sb = _sb()
    if sb is None:
        return []
    try:
        now = _now_iso()
        r = await db._sb_call(lambda: sb.table("bngc_duel_wagers").select("*")
                              .in_("status", ("open", "pending"))
                              .lt("expires_at", now).limit(50).execute())
        return (r.data if hasattr(r, "data") else []) or []
    except Exception as e:
        print(f"[duels] expirable_wagers failed: {e}")
        return []


async def user_sessions_recent(discord_id: str):
    """Active sessions newer than SESSION_TTL (the 'last join' proof). Never raises."""
    sb = _sb()
    if sb is None:
        return []
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=SESSION_TTL_SEC)).isoformat()
        r = await db._sb_call(lambda: sb.table("bngc_sessions")
                              .select("session_id,created_at")
                              .eq("discord_id", str(discord_id))
                              .eq("is_active", True)
                              .gte("created_at", cutoff)
                              .order("created_at", desc=True).limit(5).execute())
        return (r.data if hasattr(r, "data") else []) or []
    except Exception as e:
        print(f"[duels] user_sessions_recent failed: {e}")
        return []


async def duel_events(creator_id: str, opponent_id: str, since_iso: str, limit: int = 2000):
    """Kill/death/win/leave rows for both duelists since active_since. Never raises."""
    sb = _sb()
    if sb is None:
        return []
    try:
        q = (sb.table("bngc_game_events")
             .select("discord_id,session_id,event_type,event_data,created_at")
             .in_("discord_id", [str(creator_id), str(opponent_id)])
             .in_("event_type", ["kill", "death", "win", "defeat", "leave", "killfeed"]))
        if since_iso:
            q = q.gte("created_at", since_iso)
        r = await db._sb_call(lambda: q.order("created_at", desc=False)
                              .limit(int(limit)).execute())
        return (r.data if hasattr(r, "data") else []) or []
    except Exception as e:
        print(f"[duels] duel_events failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Eligibility gate ("everything good")
# ---------------------------------------------------------------------------

def _norm_duel_name(name) -> str:
    """casefold+strip for killfeed name matching. Never raises."""
    try:
        return str(name or "").strip().casefold()
    except Exception:
        return ""


async def _creator_live(creator_id: str):
    """Creator online + tracked right now (mobile joins need this).

    Strict: last_online must exist and be fresh, plus a recent session.
    Returns (ok, reason). Never raises.
    """
    uid = str(creator_id)
    if _sb() is None:
        return False, "Database not configured — try again later."
    try:
        row = await db.get_by_discord(uid)
    except Exception:
        row = None
    if not row or not (row.get("game_username") or "").strip():
        return False, "The creator has no linked game — mobile joins need a tracked creator."
    try:
        locked = row.get("is_locked")
        locked = bool(locked) if isinstance(locked, bool) else bool(locked)
        scope = str(row.get("lock_scope") or "default").lower()
        if locked or bool(row.get("is_admin_locked")) or scope == "admin":
            return False, "The creator's account is locked."
    except Exception:
        return False, "Could not verify the creator — try again."
    try:
        online, _ts = db.online_status(row.get("last_online"), WAGER_ONLINE_SEC)
    except Exception:
        online, _ts = False, None
    if not online:
        return False, "The creator is offline — mobile joins need them in game right now."
    try:
        sessions = await user_sessions_recent(uid)
    except Exception:
        sessions = []
    if not sessions:
        return False, "The creator has no live session — ask them to play first."
    return True, ""


async def _set_mobile_flag(wager_id: str):
    """Best-effort is_mobile_duel=true (tolerates pre-migration DBs). Never raises."""
    sb = _sb()
    if sb is None:
        return
    try:
        await db._sb_call(lambda: sb.table("bngc_duel_wagers").update(
            {"is_mobile_duel": True}).eq("id", str(wager_id)).execute())
    except Exception as e:
        if "is_mobile" in str(e) or "42703" in str(e):
            print("[duels] is_mobile_duel column missing — apply add_duel_wagers_v259_mobile.sql")
        else:
            print(f"[duels] mobile flag failed: {e}")


WAGER_BANS_TABLE = "bngc_wager_bans"


async def wager_ban_reason(discord_id: str) -> str:
    """Reason this user is banned from wagers, or "" when not banned.

    Fails OPEN: a database hiccup must not freeze the whole wager system.
    Never raises.
    """
    try:
        uid = str(discord_id or "").strip()
        sb = _sb()
        if not uid or sb is None:
            return ""
        r = await db._sb_call(lambda: sb.table(WAGER_BANS_TABLE)
                              .select("discord_id,reason")
                              .eq("discord_id", uid).limit(1).execute())
        rows = (r.data if hasattr(r, "data") else []) or []
        if not rows:
            return ""
        return str((rows[0] or {}).get("reason") or "").strip()
    except Exception as e:
        print(f"[duels] wager_ban_reason failed: {e}")
        return ""


async def is_wager_banned(discord_id: str) -> bool:
    """True when the user may not create/join/accept wagers. Never raises."""
    return bool(await wager_ban_reason(discord_id))


async def wager_ban(discord_id: str, reason: str = "", banned_by: str = "") -> tuple:
    """Ban a user from wagers. Returns (ok, error). Re-banning updates. Never raises."""
    try:
        uid = str(discord_id or "").strip()
        if not uid:
            return False, "Need a user."
        sb = _sb()
        if sb is None:
            return False, "Database not configured — Supabase is required for wagers."
        await db._sb_call(lambda: sb.table(WAGER_BANS_TABLE).upsert({
            "discord_id": uid,
            "reason": str(reason or "").strip()[:300],
            "banned_by": str(banned_by or ""),
            "created_at": _now_iso(),
        }, on_conflict="discord_id").execute())
        print(f"[duels] WAGER BAN {uid} by {banned_by or '?'} "
              f"reason={reason or '(none)'}")
        return True, ""
    except Exception as e:
        print(f"[duels] wager_ban failed: {e}")
        return False, ("Could not save the ban — is "
                       "add_wager_bans_v260.sql applied?")


async def wager_unban(discord_id: str) -> tuple:
    """Lift a wager ban. Returns (ok, error). Never raises."""
    try:
        uid = str(discord_id or "").strip()
        if not uid:
            return False, "Need a user."
        sb = _sb()
        if sb is None:
            return False, "Database not configured — Supabase is required for wagers."
        await db._sb_call(lambda: sb.table(WAGER_BANS_TABLE)
                          .delete().eq("discord_id", uid).execute())
        print(f"[duels] WAGER UNBAN {uid}")
        return True, ""
    except Exception as e:
        print(f"[duels] wager_unban failed: {e}")
        return False, "Could not remove the ban — try again."


def _is_guild_owner(interaction) -> bool:
    """Guild owner only (wager moderation). Never raises."""
    try:
        guild = getattr(interaction, "guild", None)
        return bool(guild is not None
                    and str(guild.owner_id) == str(interaction.user.id))
    except Exception:
        return False


def kills_race_timeline(events, creator_id: str, opponent_id: str,
                        opp_name: str, target: int, creator_name: str = "") -> dict:
    """Cumulative kills for a first-to-N-kills duel, in kill order.

    Free Mode never ends a match (stG stays at 1), so the whole duel is one
    growing match. Counts only roster kills — the creator's self-credited
    `kill` rows on the opponent, and `killfeed` rows where the opponent killed
    the creator — and reports the FIRST duelist to reach `target`, because the
    bot polls long after the deciding kill (whoever is merely ahead at poll
    time must not win). Never raises.
    """
    out = {"kills_c": 0, "kills_o": 0, "winner": None}
    try:
        try:
            tgt = int(target or 0)
        except Exception:
            tgt = 0
        me = str(creator_id or "")
        opp = _norm_duel_name(opp_name)
        mine = _norm_duel_name(creator_name)
        if tgt <= 0 or not me or not opp or not mine:
            return out
        ordered = []
        for ev in (events or []):
            if not isinstance(ev, dict):
                continue
            try:
                ordered.append((str(ev.get("created_at") or ""), ev))
            except Exception:
                continue
        for _ts, ev in sorted(ordered, key=lambda p: p[0]):
            try:
                et = str(ev.get("event_type") or "")
                if et == "leave":
                    continue
                gd = ev.get("event_data") or {}
                if et == "kill":
                    # self-credited row: only counts when the victim is the opponent
                    if str(ev.get("discord_id") or "") != me:
                        continue
                    if _norm_duel_name(gd.get("victim") or gd.get("instigated")) != opp:
                        continue
                    out["kills_c"] += 1
                    if out["winner"] is None and out["kills_c"] >= tgt:
                        out["winner"] = me
                elif et == "killfeed":
                    if str(gd.get("kill_type") or "") != "killed":
                        continue
                    if _norm_duel_name(gd.get("instigator")) != opp:
                        continue
                    if _norm_duel_name(gd.get("instigated") or gd.get("victim")) != mine:
                        continue
                    out["kills_o"] += 1
                    if out["winner"] is None and out["kills_o"] >= tgt:
                        out["winner"] = str(opponent_id or "")
            except Exception:
                continue
        return out
    except Exception:
        return out


def kill_race_seal(room_id, match_version, kills_c, kills_o, winner) -> dict:
    """The single cumulative seal entry for a kills race. Never raises."""
    try:
        return {"room_id": str(room_id or ""), "match_version": int(match_version or 0),
                "kills_c": int(kills_c or 0), "kills_o": int(kills_o or 0),
                "winner": str(winner) if winner else None, "winner_name": None,
                "race": "kills", "gm": "df"}
    except Exception:
        return {"room_id": "", "match_version": 0, "kills_c": 0, "kills_o": 0,
                "winner": None, "winner_name": None, "race": "kills", "gm": "df"}


def room_evidence(events, since_iso: str = "") -> list:
    """Private rooms with duel evidence, freshest first.

    [{room_id, code, last_ts, count, gm}] — public rooms (no code) are excluded,
    and rows older than the duel started are ignored. This is what a room
    rebind decides on, and gm is carried so a wager can never bind a room
    whose game mode is not its own (grouping would drop every row of the
    real room as "gm != mode"). Never raises.
    """
    out: dict = {}
    try:
        since_ts = (_parse_ts(since_iso).timestamp() if since_iso and _parse_ts(since_iso)
                    else None)
    except Exception:
        since_ts = None
    for ev in (events or []):
        if not isinstance(ev, dict):
            continue
        try:
            gd = ev.get("event_data") or {}
            rid = str(gd.get("room_id") or "").strip()
            code = str(gd.get("short_code") or gd.get("code") or "").strip().upper()
            if not rid or rid == "lobby" or not code:
                continue
            ts = str(ev.get("created_at") or "")
            if since_ts is not None:
                t = _parse_ts(ts)
                if t is not None and t.timestamp() < since_ts:
                    continue
            row = out.setdefault(rid, {"room_id": rid, "code": code,
                                       "last_ts": "", "count": 0, "gm": ""})
            row["count"] += 1
            gm = str(gd.get("gm") or "").strip()
            if ts > str(row["last_ts"]):
                row["last_ts"] = ts
                if gm:
                    row["gm"] = gm
            elif gm and not row["gm"]:
                # newest row carried no gm: keep the mode this room did show
                row["gm"] = gm
        except Exception:
            continue
    rows = list(out.values())
    rows.sort(key=lambda r: str(r.get("last_ts") or ""), reverse=True)
    return rows


def _rebind_idle_sec() -> int:
    """Seconds a bound room may be quiet before the duel may follow a new one."""
    try:
        return int(os.getenv("WAGER_REBIND_IDLE_SEC", "180") or 180)
    except Exception:
        return 180


def _room_mode_ok(mode: str, room) -> bool:
    """Is this room a candidate for a wager on `mode`?

    A room with no gm on any row is kept: unknown mode must never strand a
    wager that is otherwise working. Never raises.
    """
    try:
        if not mode:
            return True
        gm = str((room or {}).get("gm") or "").strip()
        if not gm:
            return True
        return gm_matches(mode, gm)
    except Exception:
        return True


def set_code_patch(rooms, code, bound_room: str = "") -> dict:
    """Fields that pin a wager to the room running under `code`.

    `/wager set-code` exists because setting the code alone is not enough: if
    the wager is still bound to an abandoned room, every row of the real room
    is filtered as `room != bound` and the duel tracks nothing while looking
    perfectly alive. So the code takes the BINDING with it - the freshest
    room carrying that code wins - and an as-yet-unseen code releases the
    binding instead, letting the resolver bind when that room appears.

    rooms: `room_evidence` rows. Returns {"room_code", "room_id"}; room_id
    None means "re-bind on the next tick". Never raises.
    """
    try:
        want = str(code or "").strip().upper()
        if not want:
            return {"room_code": None, "room_id": None}
        try:
            fresh = sorted([r for r in (rooms or []) if isinstance(r, dict)],
                           key=lambda r: str(r.get("last_ts") or ""), reverse=True)
        except Exception:
            fresh = []
        for r in fresh:
            if str(r.get("code") or "").strip().upper() == want:
                rid = str(r.get("room_id") or "").strip()
                return {"room_code": want, "room_id": rid or None}
        return {"room_code": want, "room_id": None}
    except Exception:
        return {"room_code": None, "room_id": None}


def reset_needs_escrow(wager) -> bool:
    """True when reviving this wager has to re-take the stakes.

    A cancel refunds BOTH escrows, so a revived wager's pot is unfunded and
    settling it would pay a winner out of nothing. Re-escrowing first makes
    the ledger net back to the pre-cancel state. A live wager still holds its
    stakes, and a 0-stake (for-fun) duel never escrowed anything. Never raises.
    """
    try:
        if int(wager.get("stake", 0) or 0) <= 0:
            return False
        return str(wager.get("status") or "") not in ACTIVE_STATUSES
    except Exception:
        return True  # unknown state: assume money moved, be safe


def reset_refusal(wager, *, is_duelist: bool, is_admin: bool) -> str:
    """Why a reset must not run; "" means it may. Never raises.

    Two contracts in one command. A LIVE wager being pointed at the wrong
    room is the duelist's own problem, so a duelist may fix it and no money
    moves. Reviving a CANCELLED/EXPIRED one is a re-buy - their stakes were
    already refunded - so it is admin-only.
    """
    try:
        w = wager or {}
        status = str(w.get("status") or "")
        if status == "resolved" or str(w.get("winner_id") or ""):
            return "That wager is already settled - the pot is final."
        if status in ("open", "pending"):
            return ("That wager has not started yet, so there is nothing to "
                    "reset - use /wager create or wait for a joiner.")
        if not str(w.get("opponent_id") or ""):
            return "That wager has no opponent, so it is not tracking anything."
        if status in ("cancelled", "expired"):
            if not is_admin:
                return ("That wager was closed and both stakes were refunded, "
                        "so reviving it re-buys the duel - an admin has to do "
                        "it with /wager reset.")
            return ""
        if status == "active":
            if not (is_duelist or is_admin):
                return "Only the duelists can reset their own wager."
            return ""
        return f"That wager is {status or 'in an unknown state'} - nothing to reset."
    except Exception:
        return "Couldn't read that wager - try again."


def reset_patch(wager, *, now_iso: str = "", code: str = "", rooms=None) -> dict:
    """Fields that put a backed-out wager back onto a live room. Never raises.

    `active_since` is the quiet one: every filter is a `>=` on it, so an old
    stamp does not hide new rows - it instead leaves the AFK grace and the
    24h stall clock already spent, so the first tick after a reset can cancel
    or expire the wager. Moving it restarts both and shrinks the event window
    to the new room.

    Deliberately NOT touched: `sealed` (it IS the score), `score_*` /
    `kills_*` (the victory check - clearing them erases a loss), `voided`
    (the anti-farm list) and every term of the duel (`stake`, `pot`,
    `first_to`, `mode`, `race`).

    `code` is optional: given, it is applied through set_code_patch so the
    binding moves with it; omitted, the binding is dropped and the resolver
    rebinds to the freshest room itself.
    """
    try:
        want = str(code or "").strip().upper()
        if want:
            sub = set_code_patch(rooms, want, "")
        else:
            # no code given: take the freshest room ourselves, or drop the
            # binding entirely and let the resolver rebind on its next tick
            fresh = sorted([r for r in (rooms or []) if isinstance(r, dict)
                            and str(r.get("room_id") or "")],
                           key=lambda r: str(r.get("last_ts") or ""), reverse=True)
            sub = ({"room_code": str(fresh[0].get("code") or "").strip().upper() or None,
                    "room_id": str(fresh[0].get("room_id") or "")}
                   if fresh else {"room_code": None, "room_id": None})
        return {"active_since": str(now_iso or "") or None,
                "room_id": sub.get("room_id"),
                "room_code": sub.get("room_code"),
                "resolved_at": None}
    except Exception:
        return {"active_since": str(now_iso or "") or None, "room_id": None,
                "room_code": None, "resolved_at": None}


def choose_bound_room(bound_room: str, rooms, now_iso: str,
                      bound_code: str = "", mode: str = "") -> tuple:
    """Which room is this duel in now? (room_id, code, action).

    bind    — nothing bound yet: take the room with the FRESHEST evidence
              (binding matches[0] took the OLDEST room ever seen).
    keep    — the bound room is still being played: it wins, so both duelists
              cannot farm unrelated rooms.
    rebind  -bound room has been quiet for WAGER_REBIND_IDLE_SEC, so the
              duel moved, OR the bound room is a different game mode than
              this wager (see the mode filter below).
    none    — no usable private room.
    Never raises.
    """
    try:
        rooms = [r for r in (rooms or []) if isinstance(r, dict) and r.get("room_id")]
        # A room of another game mode can never be this wager's room, and
        # binding one is fatal: grouping drops every row of the REAL room as
        # "gm != mode", so the wager tracks no kills and never seals (no
        # thread embed). This is the second-wager case - after a finished one
        # the previous room is often still the freshest evidence.
        rooms = [r for r in rooms if _room_mode_ok(mode, r)]
        if not rooms:
            return (str(bound_room or ""), str(bound_code or ""),
                    "keep" if bound_room else "none")
        # Freshest first, whatever order the caller passed them in.
        rooms.sort(key=lambda r: str(r.get("last_ts") or ""), reverse=True)
        now = _parse_ts(now_iso) or datetime.now(timezone.utc)
        idle = _rebind_idle_sec()
        if not bound_room:
            best = rooms[0]
            return str(best["room_id"]), str(best.get("code") or ""), "bind"
        by_id = {str(r["room_id"]): r for r in rooms}
        cur = by_id.get(str(bound_room))
        if cur is not None:
            # The bound room's OWN rows outrank the stored code: the stored one
            # is whatever a duelist typed earlier, possibly for a room they
            # abandoned, and grouping drops every row that disagrees with it -
            # a silently dead wager. The room pins the wager, so its code is
            # the truth; the stored one is only a fallback.
            _code = str(cur.get("code") or "") or str(bound_code or "")
            try:
                last = _parse_ts(cur.get("last_ts"))
                if last is not None and (now - last).total_seconds() <= idle:
                    return str(bound_room), _code, "keep"
            except Exception:
                return str(bound_room), _code, "keep"
        # Bound room is quiet (or no longer has evidence): follow the live one.
        best = rooms[0]
        if str(best["room_id"]) == str(bound_room):
            return str(bound_room), str(bound_code or best.get("code") or ""), "keep"
        return str(best["room_id"]), str(best.get("code") or ""), "rebind"
    except Exception:
        return (str(bound_room or ""), str(bound_code or ""),
                "keep" if bound_room else "none")


def totals_from_seals(sealed, race: str, creator_id, opponent_id) -> dict:
    """Running score/kill totals derived from the seal history. Never raises.

    The seal list is the source of truth, so recomputing from it (instead of
    adding up deltas over time) can never drift — which is what makes removing
    a sealed round safe.
    """
    out = {"score_c": 0, "score_o": 0, "kills_c": 0, "kills_o": 0}
    try:
        me, opp = str(creator_id or ""), str(opponent_id or "")
        for s in (sealed or []):
            if not isinstance(s, dict):
                continue
            try:
                kc = int(s.get("kills_c") or 0)
                ko = int(s.get("kills_o") or 0)
            except Exception:
                kc = ko = 0
            out["kills_c"] += kc
            out["kills_o"] += ko
            winner = s.get("winner")
            if winner is None:
                continue
            winner = str(winner)
            if winner == me:
                out["score_c"] += 1
            elif winner == opp:
                out["score_o"] += 1
        return out
    except Exception:
        return out


def is_voided(room_id, match_version, voided) -> bool:
    """True when this (room, version) was voided by the creator. Never raises."""
    try:
        rid, ver = str(room_id or ""), int(match_version)
        for v in (voided or []):
            if not isinstance(v, dict):
                continue
            if str(v.get("room_id") or "") == rid and int(v.get("match_version") or 0) == ver:
                return True
        return False
    except Exception:
        return False


async def current_room_version(discord_id: str, room_id: str, since_iso: str = ""):
    """Highest match_version the tracked player has rows for in this room.

    The "current match" for a duel is the newest version with any evidence —
    Free Mode never bumps the version, so this is also the whole session.
    Returns (version, error). Never raises.
    """
    try:
        sb = _sb()
        if sb is None or not room_id:
            return None, "no room"
        q = (sb.table("bngc_game_events")
             .select("event_data")
             .eq("discord_id", str(discord_id or ""))
             .in_("event_type", ["kill", "killfeed", "death", "win"])
             .order("id", desc=True).limit(200))
        r = await db._sb_call(lambda: q.execute())
        best = None
        for row in ((r.data if hasattr(r, "data") else []) or []):
            gd = (row or {}).get("event_data") or {}
            if str(gd.get("room_id") or "") != str(room_id):
                continue
            try:
                ver = int(gd.get("match_version"))
            except Exception:
                continue
            if best is None or ver > best:
                best = ver
        if best is None:
            return None, "no match evidence in that room yet"
        return best, ""
    except Exception as e:
        print(f"[duels] current_room_version failed: {e}")
        return None, "could not read match history"


def void_round(w: dict, room_id, match_version, *, by: str,
               reason: str = "") -> tuple:
    """Remove one match from a wager's history. (ok, note, fields_to_persist).

    Grants nobody anything: the round is dropped from the seals (or, if it never
    sealed, recorded so it can never seal) and the running totals are
    recomputed from what remains. Creator-only, once per match, and refused
    while the creator is behind — the one button that edits history.
    Never raises.
    """
    try:
        if not isinstance(w, dict) or not w.get("id"):
            return False, "No wager found.", {}
        if str(by or "") != str(w.get("creator_id") or ""):
            return False, "Only the wager creator can void a round.", {}
        rid, ver = str(room_id or ""), int(match_version or 0)
        if not rid:
            return False, "This duel has no bound room yet.", {}
        voided = list(w.get("voided") or [])
        if is_voided(rid, ver, voided):
            return False, "That match was already voided.", {}
        race = str(w.get("race") or "wins")
        sc = int(w.get("score_creator") or 0)
        so = int(w.get("score_opponent") or 0)
        if race == "kills":
            behind = int(w.get("kills_creator") or 0) < int(w.get("kills_opponent") or 0)
        else:
            behind = sc < so
        if behind:
            return False, ("You are behind — a void is only allowed while level "
                           "or ahead, so it can never be used to erase a loss."), {}
        sealed = [s for s in (w.get("sealed") or [])
                  if isinstance(s, dict)
                  and not (str(s.get("room_id") or "") == rid
                           and int(s.get("match_version") or 0) == ver)]
        voided.append({"room_id": rid, "match_version": ver, "by": str(by),
                       "reason": str(reason or "")[:200], "at": _now_iso()})
        t = totals_from_seals(sealed, race, w.get("creator_id"), w.get("opponent_id"))
        fields = {"sealed": sealed, "voided": voided,
                  "score_creator": t["score_c"], "score_opponent": t["score_o"],
                  "kills_creator": t["kills_c"], "kills_opponent": t["kills_o"]}
        dropped = len(w.get("sealed") or []) - len(sealed)
        note = (f"room {rid[:8]} v{ver} voided"
                + (f" ({dropped} sealed result removed)" if dropped else
                   " (nothing was scored yet)"))
        return True, note, fields
    except Exception as e:
        print(f"[duels] void_round failed: {e}")
        return False, "Could not void that round.", {}


def summarize_wagers(rows, uid) -> dict:
    """Fold a player's wager rows (either side) into one record. Never raises.

    `rows` are bngc_duel_wagers rows; the viewer may be the creator or the
    opponent, so kills and results are mapped through that. Cancelled/expired
    wagers are refunds: they count as duels but never as a win, a loss or
    coins. Streaks use resolved_at (falling back to created_at) so the order is
    chronological even when the caller passed newest-first rows.
    """
    out = {
        "total": 0, "resolved": 0, "cancelled": 0, "expired": 0, "live": 0,
        "wins": 0, "losses": 0, "draws": 0, "win_rate": 0,
        "streak": 0, "best_streak": 0,
        "coins_won": 0, "coins_lost": 0, "coins_net": 0,
        "kills": 0, "kills_against": 0, "match_wins": 0, "match_losses": 0,
        "modes": {},
    }
    try:
        uid = str(uid or "")
        scored: list = []
        for w in (rows or []):
            if not isinstance(w, dict):
                continue
            out["total"] += 1
            status = str(w.get("status") or "").lower()
            if status in ACTIVE_STATUSES:
                out["live"] += 1
            elif status == "cancelled":
                out["cancelled"] += 1
            elif status == "expired":
                out["expired"] += 1
            elif status == "resolved":
                out["resolved"] += 1
            is_creator = str(w.get("creator_id") or "") == uid
            # sealed matches: kills and per-match wins, viewer-side
            seals = w.get("sealed")
            if isinstance(seals, list):
                for s in seals:
                    if not isinstance(s, dict):
                        continue
                    try:
                        kc = int(s.get("kills_c") or 0)
                        ko = int(s.get("kills_o") or 0)
                    except Exception:
                        continue
                    if is_creator:
                        out["kills"] += kc
                        out["kills_against"] += ko
                    else:
                        out["kills"] += ko
                        out["kills_against"] += kc
                    try:
                        mw = str(s.get("winner") or "")
                    except Exception:
                        mw = ""
                    if mw and mw != "None":
                        if mw == uid:
                            out["match_wins"] += 1
                        else:
                            out["match_losses"] += 1
            if status != "resolved":
                continue
            winner = str(w.get("winner_id") or "")
            try:
                pot = int(w.get("pot") or 0)
                stake = int(w.get("stake") or 0)
            except Exception:
                pot = stake = 0
            if not winner:
                out["draws"] += 1
                continue
            if winner == uid:
                out["wins"] += 1
                out["coins_won"] += pot
            else:
                out["losses"] += 1
                out["coins_lost"] += stake
            mode = str(w.get("mode") or "?")
            m = out["modes"].setdefault(mode, {"w": 0, "l": 0})
            m["w" if winner == uid else "l"] += 1
            scored.append((str(w.get("resolved_at") or w.get("created_at") or ""), uid == winner))
        # Streaks: chronological wins/losses only.
        for _ts, won in sorted(scored, key=lambda p: p[0]):
            if won:
                out["streak"] += 1
                out["best_streak"] = max(out["best_streak"], out["streak"])
            else:
                out["streak"] = 0
        decided = out["wins"] + out["losses"]
        out["win_rate"] = int(round(100.0 * out["wins"] / decided)) if decided else 0
        out["coins_net"] = out["coins_won"] - out["coins_lost"]
        return out
    except Exception:
        return out


def wager_stats_embed(name: str, s: dict, balance=None) -> discord.Embed:
    """Render a wager record. Never raises."""
    try:
        s = s if isinstance(s, dict) else {}
        e = discord.Embed(title=f"🎲 Wager stats — {name or 'unknown'}",
                          color=discord.Color.gold())
        e.add_field(
            name="Record",
            value=(f"**{s.get('wins', 0)}W - {s.get('losses', 0)}L"
                   + (f" - {s.get('draws', 0)}D" if s.get("draws") else "")
                   + f"** · {s.get('win_rate', 0)}% win rate"),
            inline=True)
        e.add_field(
            name="Duels",
            value=(f"{s.get('total', 0)} total · {s.get('live', 0)} live\n"
                   f"{s.get('cancelled', 0)} cancelled · {s.get('expired', 0)} expired"),
            inline=True)
        e.add_field(
            name="Streak",
            value=f"**{s.get('streak', 0)}** now · best **{s.get('best_streak', 0)}**",
            inline=True)
        e.add_field(
            name="Coins",
            value=(f"won **{int(s.get('coins_won', 0) or 0):,}** · "
                   f"lost **{int(s.get('coins_lost', 0) or 0):,}**\n"
                   f"net **{int(s.get('coins_net', 0) or 0):+,}**"),
            inline=True)
        e.add_field(
            name="Matches",
            value=(f"match wins **{s.get('match_wins', 0)}** · "
                   f"match losses **{s.get('match_losses', 0)}**"),
            inline=True)
        try:
            if balance is not None:
                e.add_field(name="Balance",
                            value=f"**{int(balance):,}** Nectar Coins", inline=True)
        except Exception:
            pass
        modes = s.get("modes") or {}
        if isinstance(modes, dict) and modes:
            lines = []
            for mode in sorted(modes, key=lambda k: -(modes[k].get("w", 0) + modes[k].get("l", 0))):
                m = modes[mode] or {}
                lines.append(f"{_mode_label(mode)}: **{m.get('w', 0)}W - {m.get('l', 0)}L**")
            e.add_field(name="By mode", value="\n".join(lines[:6]) or "—", inline=False)
        e.set_footer(text="Wagers only — duels you created or joined.")
        return e
    except Exception as e:
        print(f"[duels] stats embed failed: {e}")
        return discord.Embed(title="🎲 Wager stats",
                             description="Stats unavailable right now.",
                             color=discord.Color.gold())


async def user_wagers(discord_id: str, limit: int = 500) -> list:
    """Every wager row involving this user (either side), newest first. Never raises."""
    sb = _sb()
    if sb is None or not discord_id:
        return []
    try:
        uid = str(discord_id)
        r = await db._sb_call(lambda: sb.table("bngc_duel_wagers").select("*")
                              .or_(f"creator_id.eq.{uid},opponent_id.eq.{uid}")
                              .order("created_at", desc=True).limit(int(limit)).execute())
        return (r.data if hasattr(r, "data") else []) or []
    except Exception as e:
        print(f"[duels] user_wagers failed: {e}")
        return []


async def check_eligibility(discord_id: str, stake: int, *, mobile: bool = False,
                            ignore_wager_id: str = ""):
    """(ok, reason). Never raises — reason is user-facing on failure.

    mobile=True skips the session + online checks (joiners without a
    companion); the caller must ALSO pass _creator_live for the creator.
    A wager ban (/wager ban, owner only) is checked FIRST so a banned player
    is told exactly that instead of a session or balance message.
    """
    uid = str(discord_id)
    if _sb() is None:
        return False, "Database not configured — try again later."
    try:
        _ban = await wager_ban_reason(uid)
    except Exception:
        _ban = ""
    if _ban:
        return False, ("You are banned from Nectar Coin wagers"
                       + (f": **{_ban}**" if _ban else ".")
                       + " Ask a server owner if you think this is a mistake.")
    try:
        row = await db.get_by_discord(uid)
    except Exception:
        row = None
    if not row:
        return False, "You need **/register** first."
    try:
        if not (row.get("game_username") or "").strip():
            return False, ("No game name linked — run **/fix-empty** with your "
                            "in-game name first. Wagers need identity to score.")
        locked = row.get("is_locked")
        locked = bool(locked) if isinstance(locked, bool) else bool(locked)
        scope = str(row.get("lock_scope") or "default").lower()
        if locked or bool(row.get("is_admin_locked")) or scope == "admin":
            return False, "Your account is locked — resolve that before wagering."
    except Exception:
        return False, "Could not verify your account — try again."
    try:
        bal = int(row.get("nectar_coins") or 0)
    except Exception:
        return False, "Coin migration pending — ask a host to apply add_nectar_coins_v252.sql."
    if bal < int(stake):
        return False, f"Insufficient balance ({bal:,} Nectar Coins for a {int(stake):,} stake)."
    if mobile:
        try:
            busy = await get_user_active(uid, ignore_id=ignore_wager_id)
        except Exception:
            busy = None
        if busy:
            return False, "You already have an open wager — finish it first."
        return True, ""
    try:
        sessions = await user_sessions_recent(uid)
    except Exception:
        sessions = []
    if not sessions:
        return False, ("No recent game session found — play a tracked match first "
                        "(sessions expire after 2 days).")
    try:
        busy = await get_user_active(uid, ignore_id=ignore_wager_id)
    except Exception:
        busy = None
    if busy:
        return False, "You already have an open wager — finish it first."
    try:
        _lo = row.get("last_online")
    except Exception:
        _lo = None
    if _lo is not None:
        # Column present: require a live companion (fail-open when unknown).
        online, _ts = db.online_status(_lo, WAGER_ONLINE_SEC)
        if not online:
            try:
                _rel = f"<t:{int(_ts.timestamp())}:R>" if _ts is not None else "a while"
            except Exception:
                _rel = "a while"
            return False, ("You appear offline — open the game so your companion "
                           f"reconnects (last seen {_rel}). Wagers need both duelists online.")
    return True, ""


# ---------------------------------------------------------------------------
# Embeds
# ---------------------------------------------------------------------------

MODE_LABELS = {"zw_lg": "Late Game Zonewars", "bf": "Boxfights", "df": "Free Mode"}


def _mode_label(mode: str) -> str:
    return MODE_LABELS.get(str(mode or "").lower(), str(mode or "?"))


def _room_code_line(w) -> str:
    """Thread line for a live duel: just the room code (or the bind hint).

    Never raises.
    """
    try:
        code = str((w or {}).get("room_code") or "").strip().upper()
        if code:
            return f"Room code: `{code}`"
        return ("No code pre-set — play a private room together, "
                "first shared private room binds the duel.")
    except Exception:
        return ""


ENDED_STATUSES = ("resolved", "cancelled", "expired")


def _is_ended(w) -> bool:
    """True once the wager is over: resolved (incl. forfeit), cancelled, expired.

    A finished wager must not keep a live-looking title or an "Accept or
    Decline below" footer. Never raises.
    """
    try:
        return str((w or {}).get("status") or "").strip().lower() in ENDED_STATUSES
    except Exception:
        return False


WAGER_RULES_FIELD_NAME = "📜 Rules"


def _add_rules_field(e) -> None:
    """Link the rules channel and tell players to read it BEFORE playing.

    On the board embed and on the thread's duel embed, so the pointer is
    impossible to miss before anyone stakes coins. Never raises.
    """
    try:
        cid = _rules_channel_id()
        if not cid:
            return
        e.add_field(name=WAGER_RULES_FIELD_NAME,
                    value=(f"Read the duel rules in <#{cid}> **before** you "
                           "create, join or accept a wager."),
                    inline=False)
    except Exception:
        pass


def _score_counts(w) -> tuple:
    """(creator, opponent) progress numbers for the wager's race. Never raises."""
    try:
        if str((w or {}).get("race") or "") == "kills":
            return int(w.get("kills_creator") or 0), int(w.get("kills_opponent") or 0)
        return int(w.get("score_creator") or 0), int(w.get("score_opponent") or 0)
    except Exception:
        return 0, 0


def _add_count_fields(e, w) -> None:
    """Score field, only once there is something to show.

    Score only. A kills race (df) reports its kill totals HERE, because that is
    the objective it scores on; there is no separate "Kills" row — a bf/zw_lg
    duel's kill count decides nothing, and showing it as if it did confused
    players into reading it as the score. Never raises.
    """
    try:
        started = str((w or {}).get("status") or "") in ("active", "resolved") or bool(
            (w or {}).get("sealed"))
        if not started:
            return
        c, o = _score_counts(w)
        e.add_field(name="Score", value=f"{c} - {o}", inline=True)
    except Exception:
        pass


def challenge_embed(w, creator_name: str, opponent_name: str | None) -> discord.Embed:
    wid = short_wager_id(w.get("id"))
    race = "match wins" if w.get("race") == "wins" else "points"
    try:
        stake = int(w.get("stake", 0) or 0)
    except Exception:
        stake = 0
    for_fun = stake <= 0
    ended = _is_ended(w)
    title = ("Wager ended" if ended else
             (f"For fun duel `{wid}`" if for_fun
              else f"Duel wager `{wid}` — {stake:,} coins each"))
    stakes_line = ("For fun — no stakes, no pot" if for_fun
                   else f"Pot: **{int(w.get('pot', 0)):,}** Nectar Coins")
    mobile_line = ("\n📱 Mobile duel — opponent verified by game name." if w.get("is_mobile_duel") else "")
    e = discord.Embed(
        title=title,
        description=(f"**{creator_name}** vs **{opponent_name or 'OPEN — anyone can join'}**\n"
                     f"Mode: **{_mode_label(w.get('mode'))}** · "
                     f"First to **{int(w.get('first_to', 1))}** {race} · "
                     f"{stakes_line}\n"
                     f"Private rooms only · sealed per match.{mobile_line}"),
        color=discord.Color.gold(),
    )
    if str(w.get("status")) in ("open", "pending"):
        e.add_field(name="Expires", value=_expires_rel(w), inline=True)
    else:
        e.add_field(name="Expires", value="—", inline=True)
    if ended:
        e.set_footer(text="Wager ended — this duel is closed.")
    elif str(w.get("status")) == "open":
        e.set_footer(text="Hit Join below.")
    elif str(w.get("status")) == "pending":
        e.set_footer(text="Accept or Decline below.")
    elif str(w.get("status")) == "active":
        e.set_footer(text="Duel live — sealed results post in the thread.")
    elif str(w.get("kind")) == "open":
        e.set_footer(text="Locked — duel live. Sealed results post in the thread.")
    else:
        e.set_footer(text="Targeted challenge — Accept or Decline below.")
    _add_count_fields(e, w)
    _add_rules_field(e)
    return e


def score_embed(w, creator_name: str, opponent_name: str) -> discord.Embed:
    wid = short_wager_id(w.get("id"))
    if w.get("race") == "kills":
        c_pts = int(w.get("kills_creator", 0) or 0)
        o_pts = int(w.get("kills_opponent", 0) or 0)
        target = f"first to {int(w.get('first_to', 1))} points"
    else:
        c_pts = int(w.get("score_creator", 0) or 0)
        o_pts = int(w.get("score_opponent", 0) or 0)
        target = f"first to {int(w.get('first_to', 1))} wins"
    try:
        pot = int(w.get("pot", 0) or 0)
    except Exception:
        pot = 0
    title = ("Wager ended" if _is_ended(w) else
             (f"For fun duel `{wid}`" if pot <= 0
              else f"Nectar Coins Wager `{wid}`"))
    mobile_line = (" · 📱 mobile duel" if w.get("is_mobile_duel") else "")
    e = discord.Embed(title=title,
                      description=(f"Mode **{_mode_label(w.get('mode'))}** · "
                                   + (f"Pot **{pot:,}** Nectar Coins" if pot > 0
                                      else "For fun — no pot") + mobile_line),
                      color=discord.Color.gold())
    try:
        e.add_field(name="Score",
                    value=(f"{creator_name} ({c_pts}) - {opponent_name} ({o_pts})\n"
                           f"_{target}_"),
                    inline=False)
    except Exception:
        pass
    try:
        st = str(w.get("status") or "").lower()
        winner_id = str(w.get("winner_id") or "")
        if winner_id:
            try:
                if winner_id == str((w or {}).get("creator_id")):
                    winner = creator_name
                elif winner_id == str((w or {}).get("opponent_id") or ""):
                    winner = opponent_name
                else:
                    winner = f"<@{winner_id}>"
            except Exception:
                winner = f"<@{winner_id}>"
        elif st == "active":
            if c_pts > o_pts:
                winner = f"{creator_name} leads"
            elif o_pts > c_pts:
                winner = f"{opponent_name} leads"
            else:
                winner = "Tied"
        else:
            winner = "—"
        e.add_field(name="Winner", value=winner, inline=True)
    except Exception:
        pass
    try:
        st = str(w.get("status") or "").lower()
        if st == "active":
            status = "Started — duel live"
        elif st == "resolved":
            status = "Ended"
        elif st == "expired":
            status = "Expired"
        elif st == "cancelled":
            status = "Cancelled"
        else:
            status = "Created — waiting"
        e.add_field(name="Status", value=status, inline=True)
    except Exception:
        pass
    seals = w.get("sealed") or []
    if seals:
        lines = []
        for i, s in enumerate(seals[-8:], 1):
            try:
                win = s.get("winner_name") or "draw"
                lines.append(f"Match {i}: **{win}**")
            except Exception:
                continue
        e.add_field(name="Sealed matches", value="\n".join(lines) or "—", inline=False)
    _add_rules_field(e)
    return e


def sealed_match_embed(n: int, seal: dict, creator_name: str,
                       opponent_name: str) -> discord.Embed:
    try:
        win = seal.get("winner_name") or "Draw"
        desc = f"Match {n} sealed 🔒 — **{win}**"
    except Exception:
        desc = f"Match {n} sealed 🔒"
    return discord.Embed(title=f"🔒 Sealed — Match {n}", description=desc,
                         color=discord.Color.green())


# ---------------------------------------------------------------------------
# Thread helpers
# ---------------------------------------------------------------------------

async def _make_thread(message: discord.Message, name: str) -> tuple[discord.Thread | None, bool]:
    """Private duel thread (duelists + mods with Manage Threads see it).

    Private threads are standalone — Discord forbids attaching them to a
    message — so they are created on the channel. The public fallback stays
    message-attached. (discord.py 2.7 dropped the `type` kwarg on
    Message/PartialMessage.create_thread, which made EVERY duel thread fail
    with `... got an unexpected keyword argument 'type'`.) Falls back to
    public on servers that can't do private (spectator read-only is then
    enforced by deletion). Returns (thread, is_private). Never raises.
    """
    name = (name or "duel")[:100]
    ch = getattr(message, "channel", None)
    if ch is not None and hasattr(ch, "create_thread"):
        try:
            thread = await ch.create_thread(
                name=name, auto_archive_duration=1440,
                type=discord.ChannelType.private_thread)
            print(f"[duels] thread {getattr(thread, 'id', '?')} (private) for {name[:40]}")
            return thread, True
        except Exception as e:
            print(f"[duels] private thread failed ({e}); trying public")
    try:
        thread = await message.create_thread(
            name=name, auto_archive_duration=1440)
        print(f"[duels] thread {getattr(thread, 'id', '?')} (PUBLIC fallback) for {name[:40]}")
        return thread, False
    except Exception as e:
        print(f"[duels] thread creation failed: {e}")
        return None, False


async def _post_thread(thread: discord.Thread, content=None, embed=None,
                       view=None) -> bool:
    try:
        await thread.send(content=content, embed=embed, view=view)
        return True
    except Exception as e:
        print(f"[duels] thread post failed: {e}")
        return False


async def _close_thread(thread: discord.Thread | None):
    try:
        if thread is None:
            return
        await thread.edit(archived=True, locked=True)
    except Exception as e:
        print(f"[duels] thread close failed: {e}")


async def _delete_thread(thread: discord.Thread | None):
    """Delete a duel thread outright (forfeit / cancel): no archive ghost left.

    The board message keeps the record. Never raises.
    """
    try:
        if thread is None:
            return
        await thread.delete()
        print(f"[duels] thread {getattr(thread, 'id', '?')} deleted")
    except Exception as e:
        print(f"[duels] thread delete failed: {e}")
        try:
            await thread.edit(archived=True, locked=True)
        except Exception:
            pass


def _resolve_thread(bot, thread_id: str):
    try:
        if not bot or not thread_id:
            return None
        return bot.get_channel(int(thread_id))
    except Exception:
        return None


async def _ensure_duel_thread(bot, w: dict) -> tuple["discord.Thread | None", dict]:
    """Resolve the wager's thread, creating + inviting when missing.

    Repairs wagers that went live while thread creation was broken (empty
    thread_id): fetches the board message, makes the thread, stores it,
    invites both duelists, posts the room-code line. Returns (thread, wager).
    Never raises.
    """
    try:
        w = w or {}
        thread = _resolve_thread(bot, str(w.get("thread_id") or ""))
        if thread is not None:
            return thread, w
        if bot is None or not w.get("channel_id") or not w.get("message_id"):
            return None, w
        try:
            ch = bot.get_channel(int(w["channel_id"]))
            if ch is None:
                ch = await bot.fetch_channel(int(w["channel_id"]))
            msg = await ch.fetch_message(int(w["message_id"]))
        except Exception as e:
            print(f"[duels] thread repair: board fetch failed: {e}")
            return None, w
        wid_short = short_wager_id(w.get("id"))
        try:
            _g = getattr(ch, "guild", None)
            cname, oname = await _resolve_names(_g, w)
        except Exception:
            cname, oname = "p1", "p2"
        try:
            thread, is_priv = await _make_thread(
                msg, _thread_title(wid_short, cname, oname))
        except Exception as e:
            print(f"[duels] thread repair failed: {e}")
            return None, w
        if thread is None:
            return None, w
        try:
            await update_wager(str(w.get("id")), {
                "thread_id": str(thread.id),
                "visibility": "private" if is_priv else "public",
            })
        except Exception:
            pass
        try:
            w = await get_wager(str(w.get("id"))) or w
        except Exception:
            pass
        _track_duel_thread(w, str(thread.id))
        if is_priv:
            for uid in (str(w.get("creator_id") or ""), str(w.get("opponent_id") or "")):
                if not uid:
                    continue
                try:
                    guild = getattr(ch, "guild", None)
                    m = guild.get_member(int(uid)) if guild else None
                    if m is None and guild:
                        m = await guild.fetch_member(int(uid))
                    if m is not None:
                        await thread.add_user(m)
                except Exception as e:
                    print(f"[duels] thread repair invite failed for {uid}: {e}")
        try:
            mnames = _mention_names(w)
            await _post_thread(
                thread,
                content=(f"<@{w.get('creator_id')}> <@{w.get('opponent_id')}>\n"
                         f"{_room_code_line(w)}"),
                embed=score_embed(w, mnames[0], mnames[1]),
                view=DuelThreadView())
        except Exception:
            pass
        return thread, w
    except Exception as e:
        print(f"[duels] _ensure_duel_thread failed: {e}")
        return None, w or {}


# ---------------------------------------------------------------------------
# Escrow / payout (thin wrappers over the existing coin ledger)
# ---------------------------------------------------------------------------

async def _escrow(discord_id: str, stake: int, wager_short: str):
    if int(stake or 0) <= 0:
        return 0  # for-fun duel: nothing to lock
    return await db.add_coin_delta(str(discord_id), -int(stake), "duel_escrow",
                                   {"wager": wager_short, "stake": int(stake)})


async def _refund(discord_id: str, stake: int, wager_short: str, why: str):
    if int(stake or 0) <= 0:
        return 0  # for-fun duel: nothing was locked
    try:
        return await db.add_coin_delta(str(discord_id), int(stake), "duel_refund",
                                       {"wager": wager_short, "why": why})
    except Exception as e:
        print(f"[duels] refund failed for {discord_id}: {e}")
        return None


async def _payout(discord_id: str, pot: int, wager_short: str, earned: int | None = None):
    meta = {"wager": wager_short, "pot": int(pot)}
    if earned is not None:
        # Profit stamp for the daily wager-earnings cap (ledger queryable).
        try:
            meta["earned"] = int(earned)
        except Exception:
            pass
    return await db.add_coin_delta(str(discord_id), int(pot), "duel_win", meta)


# ---------------------------------------------------------------------------
# Scoring — shared private rooms from bngc_game_events
# ---------------------------------------------------------------------------

def _event_room(ev: dict):
    """(room_id, version, gm, code) or None. Never raises.

    Version 0 is a real match version (a room's first match plays at v0 —
    the bump lands on stG=3, i.e. AFTER the match). Only the lobby bucket
    and missing/negative versions are rejected.
    """
    try:
        gd = (ev or {}).get("event_data") or {}
        rid = str(gd.get("room_id") or "").strip()
        ver = int(gd.get("match_version"))
        if not rid or rid == "lobby" or ver < 0:
            return None
        gm = gd.get("gm")
        code = str(gd.get("short_code") or gd.get("code") or "").strip()
        return rid, ver, gm, code
    except Exception:
        return None


def group_duel_matches(events: list[dict], creator_id: str, opponent_id: str,
                       mode: str, room_id: str | None, room_code: str | None,
                       since_iso: str, trace: list | None = None,
                       opp_name: str | None = None,
                       creator_name: str | None = None):
    """Group both duelists' events into shared private matches.

    Returns [{room_id, match_version, gm, code, kills_c, kills_o, win_id}]
    ordered by version. Only versions where BOTH players appear, the room
    carries a code (private), and gm fits the wager mode. Never raises.
    ``trace`` (optional list) collects per-row rejection reasons for logs.
    ``opp_name`` / ``creator_name`` (optional) are the duelists' game
    usernames: with them, only kills whose VICTIM is the other duelist count
    (roster gate); without them, self-credited kills count and the blind spot
    is logged.
    """
    _opp_name = _norm_duel_name(opp_name) if opp_name else ""
    _cre_name = _norm_duel_name(creator_name) if creator_name else ""
    try:
        since_ts = (_parse_ts(since_iso) or datetime.now(timezone.utc)).timestamp()
    except Exception:
        since_ts = 0.0
    groups: dict[tuple[str, int], dict] = {}

    def _why(reason, ev=None, rid=None, ver=None, gm=None, code=None):
        if trace is None:
            return
        try:
            who = "creator" if str((ev or {}).get("discord_id") or "") == str(creator_id) else "opponent"
            trace.append(f"{who} {str((ev or {}).get('event_type') or '?')} "
                         f"room={str(rid or '-')[:8]} v={ver} gm={gm} code={code or '-'} "
                         f"-> {reason}")
        except Exception:
            pass

    try:
        for ev in (events or []):
            try:
                did = str((ev or {}).get("discord_id") or "")
                if did not in (str(creator_id), str(opponent_id)):
                    continue
                et = str((ev or {}).get("event_type") or "")
                if et == "leave":
                    _why("leave-row (not evidence)", ev)
                    continue  # leaving is not match evidence (it seeded
                    # phantom Draw seals from post-match leave rows)
                if et == "killfeed":
                    try:
                        _gd0 = (ev or {}).get("event_data") or {}
                        if str(_gd0.get("kill_type") or "") == "left":
                            _why("killfeed=left (not combat)", ev)
                            continue  # leave notification, not combat
                    except Exception:
                        pass
                key = _event_room(ev)
                if key is None:
                    _why("no room/version (lobby or unversioned)", ev)
                    continue
                rid, ver, gm, code = key
                if et == "win" and ver > 0:
                    # Wins are written post-bump (stG=3 increments the version
                    # first, the processor drains after): file under the
                    # version the match was actually played at, or kills and
                    # wins can never share a group and wins-races never seal.
                    ver -= 1
                try:
                    ts = _parse_ts((ev or {}).get("created_at"))
                    if ts is not None and ts.timestamp() < since_ts:
                        _why("before active_since", ev, rid, ver, gm, code)
                        continue
                except Exception:
                    pass
                if room_id and rid != str(room_id):
                    _why(f"room != bound {str(room_id)[:8]}", ev, rid, ver, gm, code)
                    continue
                if room_code and code.upper() != str(room_code).upper():
                    _why(f"code != bound {room_code}", ev, rid, ver, gm, code)
                    continue
                if not code:  # private rooms only
                    _why("pub/no code", ev, rid, ver, gm, code)
                    continue
                if not gm_matches(mode, gm):
                    _why(f"gm != mode {mode}", ev, rid, ver, gm, code)
                    continue
                g = groups.setdefault((rid, ver), {
                    "room_id": rid, "match_version": ver, "gm": gm, "code": code,
                    "kills_c": 0, "kills_o": 0, "win_id": None, "death_c": 0,
                    "seen_c": False, "seen_o": False,
                })
                _gd = (ev or {}).get("event_data") or {}
                if did == str(creator_id):
                    g["seen_c"] = True
                else:
                    g["seen_o"] = True
                if et == "kill":
                    # Roster gate: a private room is full of bystanders, so a
                    # kill only counts when the VICTIM is the OTHER duelist —
                    # the creator's kills must land on the opponent and vice
                    # versa. Kills on strangers are ignored entirely.
                    try:
                        _v = _norm_duel_name(_gd.get("victim") or _gd.get("instigated"))
                    except Exception:
                        _v = ""
                    _target = _opp_name if did == str(creator_id) else _cre_name
                    if not _target:
                        # Roster unresolvable: count the self-credited kill
                        # (legacy) but make the blind spot loud.
                        if did == str(creator_id):
                            g["kills_c"] += 1
                        else:
                            g["kills_o"] += 1
                        _why("opponent game name unknown — kill counted WITHOUT "
                             "roster check", ev, rid, ver, gm, code)
                    elif _v == _target:
                        if did == str(creator_id):
                            g["kills_c"] += 1
                        else:
                            g["kills_o"] += 1
                    else:
                        _why(f"kill victim={_v or '?'} is not the other duelist "
                             f"({_target})", ev, rid, ver, gm, code)
                if et == "win" and not g["win_id"]:
                    g["win_id"] = did
                elif et == "death" and did == str(creator_id):
                    # Zone / fall damage: an elimination with no killfeed.
                    # This death IS the round for a wins race, so count it as
                    # combat evidence (kills_c/kills_o stay honest at 0).
                    g["death_c"] += 1
            except Exception:
                continue
    except Exception:
        return []
    out = []
    for g in groups.values():
        if g["seen_c"] and g["seen_o"]:
            out.append(g)
        else:
            if trace is not None:
                try:
                    _miss = [n for n, ok in (("creator", g["seen_c"]),
                                             ("opponent", g["seen_o"])) if not ok]
                    trace.append(f"group room={g['room_id'][:8]} v={g['match_version']} "
                                 f"incomplete (missing {','.join(_miss)} presence) — dropped")
                except Exception:
                    pass
    out.sort(key=lambda g: (g["room_id"], g["match_version"]))
    return out


_MOBILE_NAME_KEYS = ("instigator", "instigated", "victim", "killer",
                     "player", "player_name")


def group_mobile_matches(events: list[dict], creator_id: str, opp_name_norm: str,
                         mode: str, room_id: str | None, room_code: str | None,
                         since_iso: str, trace: list | None = None):
    """Matches from the TRACKED side's rows only; opponent proven by name.

    Same output shape as group_duel_matches. Co-presence: the opponent's
    game name appears in killfeed names (instigator/instigated/...) of that
    (room_id, match_version), OR the tracked player has a `death` row in that
    version — zone damage and fall damage never reach a killfeed, so a death
    is the only evidence such a room ever produces. kills_o counts killfeed
    'killed' rows by that name; kills_c counts the creator's self-credited
    'kill' rows. win_id carries the creator's win row if any. The opponent has
    no capture at all, so the server never writes a win row for them —
    opponent_win infers their round win from the LAST roster combat event in
    the version being the tracked player's elimination (killed by the opponent,
    or killed by the zone with no killfeed at all). Kills are a tiebreak
    display, not the objective, so kills never decide a wins race.
    Never raises.
    """
    try:
        since_ts = (_parse_ts(since_iso) or datetime.now(timezone.utc)).timestamp()
    except Exception:
        since_ts = 0.0
    groups: dict[tuple[str, int], dict] = {}

    def _why(reason, ev=None, rid=None, ver=None, gm=None, code=None):
        if trace is None:
            return
        try:
            trace.append(f"tracked {str((ev or {}).get('event_type') or '?')} "
                         f"room={str(rid or '-')[:8]} v={ver} gm={gm} code={code or '-'} "
                         f"-> {reason}")
        except Exception:
            pass

    try:
        if not opp_name_norm:
            return []
        for ev in (events or []):
            try:
                did = str((ev or {}).get("discord_id") or "")
                if did != str(creator_id):
                    continue  # tracked side only
                et = str((ev or {}).get("event_type") or "")
                if et == "leave":
                    _why("leave-row (not evidence)", ev)
                    continue  # leaving is not match evidence
                if et == "killfeed":
                    try:
                        _gd0 = (ev or {}).get("event_data") or {}
                        if str(_gd0.get("kill_type") or "") == "left":
                            _why("killfeed=left (not combat)", ev)
                            continue  # leave notification, not combat
                    except Exception:
                        pass
                key = _event_room(ev)
                if key is None:
                    _why("no room/version (lobby or unversioned)", ev)
                    continue
                rid, ver, gm, code = key
                if et == "win" and ver > 0:
                    # Same post-bump filing as group_duel_matches: the win
                    # belongs to the version the match was played at.
                    ver -= 1
                try:
                    ts = _parse_ts((ev or {}).get("created_at"))
                    if ts is not None and ts.timestamp() < since_ts:
                        _why("before active_since", ev, rid, ver, gm, code)
                        continue
                except Exception:
                    pass
                if room_id and rid != str(room_id):
                    _why(f"room != bound {str(room_id)[:8]}", ev, rid, ver, gm, code)
                    continue
                if room_code and code.upper() != str(room_code).upper():
                    _why(f"code != bound {room_code}", ev, rid, ver, gm, code)
                    continue
                if not code:  # private rooms only
                    _why("pub/no code", ev, rid, ver, gm, code)
                    continue
                if not gm_matches(mode, gm):
                    _why(f"gm != mode {mode}", ev, rid, ver, gm, code)
                    continue
                g = groups.setdefault((rid, ver), {
                    "room_id": rid, "match_version": ver, "gm": gm, "code": code,
                    "kills_c": 0, "kills_o": 0, "win_id": None, "death_c": 0,
                    "seen_o": False, "_strikes": [],
                })
                gd = (ev or {}).get("event_data") or {}
                # created_at of this row, used to order strikes.
                try:
                    _ts = _parse_ts((ev or {}).get("created_at"))
                    _ts_key = _ts.timestamp() if _ts is not None else 0.0
                except Exception:
                    _ts_key = 0.0

                def _strike(side):
                    try:
                        g["_strikes"].append((_ts_key, side))
                    except Exception:
                        pass

                if et == "kill":
                    # Roster gate: a private zw_lg room is full of strangers,
                    # so a self-credited kill only counts when the VICTIM is the
                    # opponent. Kills on bystanders are ignored entirely.
                    try:
                        _vic = _norm_duel_name(gd.get("victim") or gd.get("instigated"))
                    except Exception:
                        _vic = ""
                    if opp_name_norm and _vic == opp_name_norm:
                        g["kills_c"] += 1
                        _strike("c")
                    else:
                        _why(f"kill victim={_vic or '?'} not in wager roster "
                             f"(opponent={opp_name_norm or '?'})", ev, rid, ver, gm, code)
                elif et == "win" and not g["win_id"]:
                    g["win_id"] = did
                elif et == "death":
                    # Zone / fall damage (killer=None, via="alv") or a killfeed
                    # death: either way the tracked player is out. With no
                    # killfeed there is no opponent name in this room, so this
                    # death is also the co-presence proof.
                    g["death_c"] += 1
                    _strike("d")
                elif et == "killfeed":
                    try:
                        names = {_norm_duel_name(gd.get(k))
                                 for k in _MOBILE_NAME_KEYS if gd.get(k)}
                    except Exception:
                        names = set()
                    if opp_name_norm in names:
                        g["seen_o"] = True
                        try:
                            if (str(gd.get("kill_type") or "") == "killed"
                                    and _norm_duel_name(gd.get("instigator")) == opp_name_norm):
                                # Roster gate (mobile side): only count when
                                # the victim is the tracked player.
                                _v = _norm_duel_name(gd.get("instigated") or gd.get("victim"))
                                _c = _norm_duel_name(gd.get("instigator") or gd.get("killer"))
                                if _v and _c and _v != _c and _c == opp_name_norm:
                                    g["kills_o"] += 1
                                    _strike("o")
                        except Exception:
                            pass
            except Exception:
                continue
    except Exception:
        return []
    out = []
    for g in groups.values():
        # opponent_win: the LAST roster combat event in this version was the
        # tracked player's elimination — killed by the opponent ("o"), or the
        # zone/fall damage that never reaches a killfeed ("d"). Their win row
        # can never exist (no capture on their side), and the tracked player's
        # side is authoritative — no win row for them means they did not win.
        try:
            _st = sorted(g.get("_strikes") or [], key=lambda p: p[0])
            g["opponent_win"] = bool(_st) and _st[-1][1] in ("o", "d")
        except Exception:
            g["opponent_win"] = False
        try:
            g.pop("_strikes", None)
        except Exception:
            pass
        if g["seen_o"] or int(g.get("death_c") or 0) > 0:
            out.append(g)
        elif trace is not None:
            try:
                trace.append(f"group room={g['room_id'][:8]} v={g['match_version']} "
                             f"opponent '{opp_name_norm}' never appears in killfeed "
                             f"names — no co-presence proof, dropped")
            except Exception:
                pass
    out.sort(key=lambda g: (g["room_id"], g["match_version"]))
    return out


# ---------------------------------------------------------------------------
# Persistent view (fixed custom_ids -> restart-safe, wager resolved at click)
# ---------------------------------------------------------------------------

class DuelView(discord.ui.View):
    """One persistent view for every wager message/thread. Never raises."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _wager_for(self, interaction: discord.Interaction):
        try:
            channel_id = str(getattr(interaction.channel, "id", "") or "")
            w = await find_wager_by_thread(channel_id)
            if w:
                return w
            msg = getattr(interaction, "message", None)
            if msg is not None and getattr(msg, "id", None):
                return await find_wager_by_message(str(msg.id))
            return None
        except Exception as e:
            print(f"[duels] view lookup failed: {e}")
            return None

    async def _refresh_board(self, interaction: discord.Interaction, w: dict):
        try:
            mnames = _mention_names(w)
            e = challenge_embed(w, mnames[0], mnames[1])
            if getattr(interaction, "message", None) is not None:
                # Buttons live only while joinable/answerable; once locked
                # the message is a record (prevents dead Join/Accept clicks).
                keep = str(w.get("status")) in ("open", "pending")
                await interaction.message.edit(embed=e, view=self if keep else None)
        except Exception as ex:
            print(f"[duels] board refresh failed: {ex}")

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success,
                       custom_id="duel:join")
    async def join_btn(self, interaction: discord.Interaction,
                       button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            w = await self._wager_for(interaction)
            if not w or str(w.get("status")) != "open" or str(w.get("kind")) != "open":
                return await interaction.followup.send(
                    "That wager is no longer open.", ephemeral=True)
            me = str(interaction.user.id)
            if me == str(w.get("creator_id")):
                return await interaction.followup.send(
                    "You created this wager — wait for someone to join.", ephemeral=True)
            ok, reason = await check_eligibility(me, int(w.get("stake", 0)))
            _mobile = False
            if not ok:
                # Mobile path: no companion, no captures — allowed only when
                # the creator is live + tracked right now (their client
                # verifies the room; the joiner is scored by game name).
                mok, mreason = await check_eligibility(me, int(w.get("stake", 0)),
                                                       mobile=True)
                if not mok:
                    return await interaction.followup.send(mreason, ephemeral=True)
                cok, creason = await _creator_live(str(w.get("creator_id")))
                if not cok:
                    return await interaction.followup.send(
                        creason + " Mobile joins need the creator in game right now.",
                        ephemeral=True)
                _mobile = True
            # Re-check: may have filled while we verified.
            fresh = await get_wager(str(w.get("id")))
            if not fresh or str(fresh.get("status")) != "open":
                return await interaction.followup.send(
                    "Someone just took that slot.", ephemeral=True)
            wid_short = short_wager_id(w.get("id"))
            try:
                await _escrow(me, int(w.get("stake", 0)), wid_short)
            except Exception as e:
                return await interaction.followup.send(str(e)[:160], ephemeral=True)
            ok_up = await update_wager(str(w.get("id")), {
                "opponent_id": me, "status": "active",
                "pot": int(w.get("stake", 0)) * 2,
                "active_since": _now_iso(),
            })
            if not ok_up:
                await _refund(me, int(w.get("stake", 0)), wid_short, "lock_failed")
                return await interaction.followup.send(
                    "Could not lock the wager — stake refunded.", ephemeral=True)
            if _mobile:
                await _set_mobile_flag(str(w.get("id")))
            w2 = await get_wager(str(w.get("id")))
            # Thread for the duel (private: invite both duelists, mods see it
            # via Manage Threads).
            board_msg = getattr(interaction, "message", None)
            thread = None
            is_priv = False
            if board_msg is not None:
                try:
                    names = await _display_names(interaction, w2 or w)
                    thread, is_priv = await _make_thread(
                        board_msg, _thread_title(wid_short, names[0], names[1]))
                except Exception as e:
                    print(f"[duels] duel thread failed: {e}")
            if thread is not None:
                await update_wager(str(w.get("id")), {
                    "thread_id": str(thread.id),
                    "visibility": "private" if is_priv else "public",
                })
                if is_priv:
                    for uid in (str((w2 or w).get("creator_id")), me):
                        try:
                            m = interaction.guild.get_member(int(uid)) if interaction.guild else None
                            if m is None and interaction.guild:
                                m = await interaction.guild.fetch_member(int(uid))
                            if m is not None:
                                await thread.add_user(m)
                        except Exception:
                            pass
                w2 = await get_wager(str(w.get("id")))
                _track_duel_thread(w2 or w, str(thread.id))
                try:
                    mnames = _mention_names(w2 or w)
                    await _post_thread(
                        thread,
                        content=(f"<@{w2.get('creator_id')}> <@{me}>\n"
                                 f"{_room_code_line(w2 or w)}"),
                        embed=score_embed(w2 or w, mnames[0], mnames[1]),
                        view=DuelThreadView())
                except Exception:
                    pass
            print(f"[duels] START {wid_short} JOIN creator={w.get('creator_id')} "
                  f"opponent={me} mode={w.get('mode')} race={w.get('race')} "
                  f"first_to={w.get('first_to')} stake={w.get('stake')} pot={w.get('pot')}"
                  + (" (mobile duel)" if _mobile else "")
                  + f" thread={getattr(thread, 'id', None) or '-'}")
            try:
                await self._refresh_board(interaction, w2 or {**w, "status": "active",
                                                              "opponent_id": me})
            except Exception:
                pass
            _locked = (f"Locked in — for fun duel, good luck!"
                       if int(w.get("stake", 0) or 0) <= 0
                       else f"Locked in for **{int(w.get('stake', 0)):,}** Nectar Coins — good luck!")
            if thread is None:
                _locked += ("\n⚠️ Results thread could not be created — "
                            "ask a mod to check my thread permissions.")
            await interaction.followup.send(_locked, ephemeral=True)
            try:
                _thr = ""
                try:
                    _tid = str((w2 or w).get("thread_id") or "")
                    _thr = f" Thread: <#{_tid}>" if _tid else ""
                except Exception:
                    pass
                await _dm(interaction.client, str((w2 or w).get("creator_id")),
                          f"<@{me}> joined your open wager (`{wid_short}`) — duel is live.{_thr}")
            except Exception as e:
                print(f"[duels] join dm failed: {e}")
        except Exception as e:
            print(f"[duels] join failed: {e}")
            try:
                await interaction.followup.send("Something went wrong — try again.",
                                                ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success,
                       custom_id="duel:accept")
    async def accept_btn(self, interaction: discord.Interaction,
                         button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            w = await self._wager_for(interaction)
            if not w or str(w.get("status")) != "pending":
                return await interaction.followup.send(
                    "Nothing to accept here.", ephemeral=True)
            me = str(interaction.user.id)
            if me != str(w.get("opponent_id")):
                return await interaction.followup.send(
                    "Only the challenged player can accept.", ephemeral=True)
            # The challenge being accepted must not count as "you already have
            # an open wager" — that is this very row.
            _ignore = str(w.get("id") or "")
            ok, reason = await check_eligibility(me, int(w.get("stake", 0)),
                                                 ignore_wager_id=_ignore)
            _mobile = False
            if not ok:
                # Mobile path: same deal as open joins — creator must be live.
                mok, mreason = await check_eligibility(me, int(w.get("stake", 0)),
                                                       mobile=True,
                                                       ignore_wager_id=_ignore)
                if not mok:
                    return await interaction.followup.send(mreason, ephemeral=True)
                cok, creason = await _creator_live(str(w.get("creator_id")))
                if not cok:
                    return await interaction.followup.send(
                        creason + " Mobile accepts need the creator in game right now.",
                        ephemeral=True)
                _mobile = True
            wid_short = short_wager_id(w.get("id"))
            try:
                await _escrow(me, int(w.get("stake", 0)), wid_short)
            except Exception as e:
                return await interaction.followup.send(str(e)[:160], ephemeral=True)
            await update_wager(str(w.get("id")), {
                "status": "active", "pot": int(w.get("stake", 0)) * 2,
                "active_since": _now_iso(),
            })
            print(f"[duels] START {wid_short} ACCEPT creator={w.get('creator_id')} "
                  f"opponent={me} mode={w.get('mode')} race={w.get('race')} "
                  f"first_to={w.get('first_to')} stake={w.get('stake')} pot={w.get('pot')}"
                  + (" (mobile duel)" if _mobile else "")
                  + f" thread={str(w.get('thread_id') or '-')}")
            if _mobile:
                await _set_mobile_flag(str(w.get("id")))
            w2 = await get_wager(str(w.get("id")))
            thread = _resolve_thread(interaction.client, str((w2 or w).get("thread_id")))
            if thread is None and getattr(interaction, "message", None) is not None:
                # Repair: accepted while thread creation was broken.
                try:
                    _anames = await _display_names(interaction, w2 or w)
                    _athread, _apriv = await _make_thread(
                        interaction.message,
                        _thread_title(wid_short, _anames[0], _anames[1]))
                    if _athread is not None:
                        await update_wager(str(w.get("id")), {
                            "thread_id": str(_athread.id),
                            "visibility": "private" if _apriv else "public",
                        })
                        w2 = await get_wager(str(w.get("id")))
                        thread = _athread
                        print(f"[duels] {wid_short} thread {thread.id} created on accept")
                except Exception as e:
                    print(f"[duels] accept thread repair failed: {e}")
            if thread is not None:
                _track_duel_thread(w2 or w, str(thread.id))
                # Belt-and-suspenders: both duelists in the (private) thread.
                for _uid in (str((w2 or w).get("creator_id")), me):
                    try:
                        _guild = interaction.guild
                        _m = _guild.get_member(int(_uid)) if _guild else None
                        if _m is None and _guild:
                            _m = await _guild.fetch_member(int(_uid))
                        if _m is not None:
                            await thread.add_user(_m)
                    except Exception:
                        pass
                try:
                    mnames = _mention_names(w2 or w)
                    await _post_thread(
                        thread,
                        content=(f"<@{w2.get('creator_id')}> <@{me}>\n"
                                 f"{_room_code_line(w2 or w)}"),
                        embed=score_embed(w2 or w, mnames[0], mnames[1]),
                        view=DuelThreadView())
                except Exception:
                    pass
            # Board lives in the wager channel: refresh to locked (no buttons).
            try:
                await refresh_board_embed(interaction.client, w2 or w)
            except Exception as e:
                print(f"[duels] accept board refresh failed: {e}")
            await interaction.followup.send("Accepted — duel is live.", ephemeral=True)
            try:
                _thr = ""
                try:
                    _tid = str((w2 or w).get("thread_id") or "")
                    _thr = f" Thread: <#{_tid}>" if _tid else ""
                except Exception:
                    pass
                await _dm(interaction.client, str((w2 or w).get("creator_id")),
                          f"<@{me}> accepted your challenge (`{wid_short}`) — duel is live.{_thr}")
            except Exception as e:
                print(f"[duels] accept dm failed: {e}")
        except Exception as e:
            print(f"[duels] accept failed: {e}")
            try:
                await interaction.followup.send("Something went wrong — try again.",
                                                ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger,
                       custom_id="duel:decline")
    async def decline_btn(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            w = await self._wager_for(interaction)
            if not w or str(w.get("status")) not in ("open", "pending"):
                return await interaction.followup.send(
                    "Nothing to decline here.", ephemeral=True)
            me = str(interaction.user.id)
            if me not in (str(w.get("creator_id")), str(w.get("opponent_id") or "")):
                return await interaction.followup.send(
                    "Only the duelists can decline this.", ephemeral=True)
            wid_short = short_wager_id(w.get("id"))
            await _refund(str(w.get("creator_id")), int(w.get("stake", 0)),
                          wid_short, "declined")
            await update_wager(str(w.get("id")), {"status": "cancelled",
                                                  "resolved_at": _now_iso()})
            print(f"[duels] END {wid_short} DECLINED by {me} — stake refunded")
            await refresh_board_embed(interaction.client, w)
            try:
                _stake = int(w.get("stake", 0) or 0)
                _opp = str(w.get("opponent_id") or "")
                if me == _opp:
                    # Opponent declined -> creator gets the news + refund note.
                    await _dm(interaction.client, str(w.get("creator_id")),
                              f"<@{me}> declined your wager (`{wid_short}`). "
                              + (f"**{_stake:,}** Nectar Coins refunded." if _stake > 0
                                 else "It was a for-fun duel — nothing to refund."))
                elif me == str(w.get("creator_id")) and _opp:
                    # Creator cancelled a targeted challenge -> tell the opponent.
                    await _dm(interaction.client, _opp,
                              f"<@{me}> cancelled the wager (`{wid_short}`) "
                              f"you were challenged to.")
            except Exception as e:
                print(f"[duels] decline dm failed: {e}")
            thread = _resolve_thread(interaction.client, str(w.get("thread_id") or ""))
            if thread is not None:
                await _delete_thread(thread)
            _untrack_thread(str(w.get("thread_id") or ""))
            try:
                if getattr(interaction, "message", None) is not None:
                    e = discord.Embed(title="Wager cancelled",
                                      description="Stake refunded.",
                                      color=discord.Color.red())
                    await interaction.message.edit(embed=e, view=None)
            except Exception:
                pass
            await interaction.followup.send("Cancelled — creator refunded.", ephemeral=True)
        except Exception as e:
            print(f"[duels] decline failed: {e}")
            try:
                await interaction.followup.send("Something went wrong — try again.",
                                                ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Forfeit", style=discord.ButtonStyle.secondary,
                       custom_id="duel:forfeit")
    async def forfeit_btn(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            w = await self._wager_for(interaction)
            if not w:
                return await interaction.followup.send(
                    "No wager found here.", ephemeral=True)
            me = str(interaction.user.id)
            status = str(w.get("status"))
            if status in ("open", "pending") and me == str(w.get("creator_id")):
                # Creator cancels before it starts.
                wid_short = short_wager_id(w.get("id"))
                await _refund(me, int(w.get("stake", 0)), wid_short, "cancelled")
                await update_wager(str(w.get("id")), {"status": "cancelled",
                                                      "resolved_at": _now_iso()})
                await refresh_board_embed(interaction.client, w)
                thread = _resolve_thread(interaction.client,
                                         str(w.get("thread_id") or ""))
                if thread is not None:
                    await _delete_thread(thread)
                _untrack_thread(str(w.get("thread_id") or ""))
                return await interaction.followup.send(
                    "Wager cancelled — stake refunded.", ephemeral=True)
            if status != "active":
                return await interaction.followup.send(
                    "Only a live duel can be forfeited.", ephemeral=True)
            if me not in (str(w.get("creator_id")), str(w.get("opponent_id"))):
                return await interaction.followup.send(
                    "Only the duelists can forfeit.", ephemeral=True)
            winner = (str(w.get("opponent_id")) if me == str(w.get("creator_id"))
                      else str(w.get("creator_id")))
            await _settle(w, winner, interaction.client, how="forfeit")
            try:
                _pot = int(w.get("pot", 0) or 0)
                await _dm(interaction.client, winner,
                          f"<@{me}> forfeited wager `{short_wager_id(w.get('id'))}` — you take "
                          + (f"**{_pot:,}** Nectar Coins." if _pot > 0 else "the win."))
            except Exception as e:
                print(f"[duels] forfeit dm failed: {e}")
            await interaction.followup.send("Forfeited — opponent takes the pot.",
                                            ephemeral=True)
        except Exception as e:
            print(f"[duels] forfeit failed: {e}")
            try:
                await interaction.followup.send("Something went wrong — try again.",
                                                ephemeral=True)
            except Exception:
                pass


class DuelThreadView(discord.ui.View):
    """Forfeit / Void round / Cancel controls on the wager THREAD embed.

    The board strips its buttons once the duel goes live, so the thread is the
    only place a duelist can act. Fixed custom_ids make it restart-safe.
      Forfeit     — live duel: opponent takes the pot.
      Void round  — creator only: discard the current match (opponent crashed
                    or false start). Awards nobody; the round is removed from
                    the score and can never seal again.
      Cancel      — any state: refund BOTH escrows, no winner, close out.
    Never raises.
    """

    def __init__(self):
        super().__init__(timeout=None)

    async def _wager(self, interaction):
        try:
            tid = str(getattr(interaction.channel, "id", "") or "")
            w = await find_wager_by_thread(tid)
            if w:
                return w
            mid = str(getattr(getattr(interaction, "message", None), "id", "") or "")
            return await find_wager_by_message(mid) if mid else None
        except Exception as e:
            print(f"[duels] thread view lookup failed: {e}")
            return None

    @discord.ui.button(label="Forfeit", style=discord.ButtonStyle.danger,
                       custom_id="duel:thread_forfeit")
    async def thread_forfeit_btn(self, interaction: discord.Interaction,
                                 button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            w = await self._wager(interaction)
            if not w:
                return await interaction.followup.send("No wager found here.",
                                                        ephemeral=True)
            me = str(interaction.user.id)
            creator = str(w.get("creator_id"))
            opponent = str(w.get("opponent_id") or "")
            if me not in (creator, opponent):
                return await interaction.followup.send("Only the duelists can forfeit.",
                                                        ephemeral=True)
            status = str(w.get("status"))
            wid_short = short_wager_id(w.get("id"))
            stake = int(w.get("stake", 0) or 0)
            if status in ("open", "pending"):
                # Not started — forfeiting = cancelling, no winner.
                await _refund(creator, stake, wid_short, "forfeited_pre_start")
                await update_wager(str(w.get("id")), {"status": "cancelled",
                                                      "resolved_at": _now_iso()})
                await refresh_board_embed(interaction.client, w)
                await _close_thread(_resolve_thread(interaction.client,
                                                    str(w.get("thread_id") or "")))
                _untrack_thread(str(w.get("thread_id") or ""))
                return await interaction.followup.send(
                    "Wager cancelled before it started — refunded.", ephemeral=True)
            if status != "active":
                return await interaction.followup.send("This duel is already over.",
                                                        ephemeral=True)
            winner = opponent if me == creator else creator
            await _settle(w, winner, interaction.client, how="forfeit")
            try:
                _pot = int(w.get("pot", 0) or 0)
                await _dm(interaction.client, winner,
                          f"<@{me}> forfeited wager `{wid_short}` — you take "
                          + (f"**{_pot:,}** Nectar Coins." if _pot > 0 else "the win."))
            except Exception as e:
                print(f"[duels] thread forfeit dm failed: {e}")
            await interaction.followup.send("Forfeited — opponent takes the pot.",
                                            ephemeral=True)
        except Exception as e:
            print(f"[duels] thread forfeit failed: {e}")
            try:
                await interaction.followup.send("Something went wrong — try again.",
                                                ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Void round", style=discord.ButtonStyle.secondary,
                       custom_id="duel:thread_void")
    async def thread_void_btn(self, interaction: discord.Interaction,
                              button: discord.ui.Button):
        """Discard the current match (creator only).

        A crashed opponent produces no events at all, so that match can never
        be scored. Voiding awards nobody: the seal is removed and the
        (room, version) is remembered so it can never seal later.
        """
        await interaction.response.defer(ephemeral=True)
        try:
            w = await self._wager(interaction)
            if not w:
                return await interaction.followup.send("No wager found here.",
                                                        ephemeral=True)
            me = str(interaction.user.id)
            if str(w.get("status")) != "active":
                return await interaction.followup.send("This duel is not live.",
                                                        ephemeral=True)
            if me != str(w.get("creator_id")):
                return await interaction.followup.send(
                    "Only the wager creator can void a round.", ephemeral=True)
            room = str(w.get("room_id") or "")
            if not room:
                return await interaction.followup.send(
                    "No private room is bound to this duel yet.", ephemeral=True)
            ver, verr = await current_room_version(
                me, room, str(w.get("active_since") or ""))
            if ver is None:
                return await interaction.followup.send(
                    f"Can't tell which match this is — {verr}.", ephemeral=True)
            ok, note, fields = void_round(w, room, ver, by=me, reason="creator void")
            if not ok:
                return await interaction.followup.send(note, ephemeral=True)
            if not await update_wager(str(w.get("id")), fields):
                return await interaction.followup.send(
                    "Could not save the void — is add_duel_wagers_v261_voided.sql "
                    "applied?", ephemeral=True)
            wid_short = short_wager_id(w.get("id"))
            print(f"[duels] VOID {wid_short} room={room[:8]} v={ver} by={me} "
                  f"-> score {fields['score_creator']}-{fields['score_opponent']} "
                  f"kills {fields['kills_creator']}-{fields['kills_opponent']}")
            # The opponent is told, always.
            try:
                await _dm(interaction.client, str(w.get("opponent_id") or ""),
                          f"The creator voided match v{ver} of wager `{wid_short}` "
                          f"(crash / false start). It scored nobody. "
                          f"Current score: {fields['score_creator']}-"
                          f"{fields['score_opponent']}.")
            except Exception as e:
                print(f"[duels] void dm failed: {e}")
            w2 = await get_wager(str(w.get("id"))) or {**w, **fields}
            thread = _resolve_thread(interaction.client, str(w2.get("thread_id") or ""))
            if thread is not None:
                try:
                    e = discord.Embed(
                        title="⚠️ Match voided",
                        description=(f"<@{me}> voided match v{ver} — no score for "
                                     f"either duelist.\n_Score is now "
                                     f"{fields['score_creator']}-"
                                     f"{fields['score_opponent']}._"),
                        color=discord.Color.greyple())
                    await _post_thread(thread, embed=e)
                    cname, oname = _mention_names(w2)
                    await _post_thread(thread,
                                       embed=score_embed(w2, cname, oname))
                except Exception as ex:
                    print(f"[duels] void thread note failed: {ex}")
            await refresh_board_embed(interaction.client, w2)
            return await interaction.followup.send(
                f"Voided match v{ver} — it scored nobody. {note}.",
                ephemeral=True)
        except Exception as e:
            print(f"[duels] thread void failed: {e}")
            try:
                await interaction.followup.send("Something went wrong — try again.",
                                                ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary,
                       custom_id="duel:thread_cancel")
    async def thread_cancel_btn(self, interaction: discord.Interaction,
                                button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            w = await self._wager(interaction)
            if not w:
                return await interaction.followup.send("No wager found here.",
                                                        ephemeral=True)
            me = str(interaction.user.id)
            creator = str(w.get("creator_id"))
            opponent = str(w.get("opponent_id") or "")
            if me not in (creator, opponent):
                return await interaction.followup.send("Only the duelists can cancel.",
                                                        ephemeral=True)
            status = str(w.get("status"))
            if status in ("resolved", "cancelled", "expired"):
                return await interaction.followup.send("This wager is already closed.",
                                                        ephemeral=True)
            wid_short = short_wager_id(w.get("id"))
            stake = int(w.get("stake", 0) or 0)
            # Refund BOTH escrowed stakes — cancel pays nobody.
            await _refund(creator, stake, wid_short, "cancelled")
            if opponent:
                await _refund(opponent, stake, wid_short, "cancelled")
            await update_wager(str(w.get("id")), {"status": "cancelled",
                                                  "resolved_at": _now_iso()})
            await refresh_board_embed(interaction.client, w)
            thread = _resolve_thread(interaction.client, str(w.get("thread_id") or ""))
            if thread is not None:
                await _delete_thread(thread)
            _untrack_thread(str(w.get("thread_id") or ""))
            print(f"[duels] END {wid_short} CANCELLED by {me} — both stakes refunded, thread deleted")
            await interaction.followup.send("Duel cancelled — both stakes refunded.",
                                            ephemeral=True)
        except Exception as e:
            print(f"[duels] thread cancel failed: {e}")
            try:
                await interaction.followup.send("Something went wrong — try again.",
                                                ephemeral=True)
            except Exception:
                pass


def _mention_names(w: dict):
    """(<@creator>, <@opponent>|"TBD") for score embeds. Never raises."""
    try:
        cid = str((w or {}).get("creator_id") or "").strip()
        c = f"<@{cid}>" if cid else "?"
    except Exception:
        c = "?"
    try:
        oid = str((w or {}).get("opponent_id") or "").strip()
        o = f"<@{oid}>" if oid else "TBD"
    except Exception:
        o = "TBD"
    return c, o


async def _resolve_names(guild, w: dict):
    """(creator_name, opponent_name|None) from any guild-bearing source. Never raises.

    Real usernames only — thread titles must never read `user-129547…`.
    Order: guild member cache -> fetch_member (missed cache) -> linked game
    username -> Discord id as a last resort.
    """
    async def _one(discord_id):
        did = str(discord_id or "").strip()
        if not did:
            return None
        try:
            if guild is not None:
                m = guild.get_member(int(did))
                if m is None:
                    try:
                        m = await guild.fetch_member(int(did))
                    except Exception:
                        m = None
                if m is not None:
                    return (getattr(m, "display_name", None) or getattr(m, "name", None)
                            or None)
        except Exception:
            pass
        # Mobile/left-the-server duelists: their linked game name still reads
        # as a real person in a thread title.
        try:
            row = await db.get_by_discord(did)
            gn = str((row or {}).get("game_username") or "").strip()
            if gn:
                return gn
        except Exception:
            pass
        return f"user-{did[:6]}" if did else None

    try:
        cname = await _one((w or {}).get("creator_id")) or "?"
        oname = await _one((w or {}).get("opponent_id"))
        return cname, oname
    except Exception:
        return "?", None


async def _display_names(interaction: discord.Interaction, w: dict):
    """(creator_name, opponent_name|None) for thread names. Never raises."""
    try:
        return await _resolve_names(getattr(interaction, "guild", None), w)
    except Exception:
        return "?", None


def _thread_title(wid_short: str, cname, oname) -> str:
    """Thread name: `wager-<id>-<name>-vs-<name>` using REAL usernames."""
    c = _clean_name(cname) or "p1"
    o = _clean_name(oname) or "open"
    return f"wager-{wid_short}-{c}-vs-{o}"[:100]


def _clean_name(n) -> str:
    """Discord-safe thread-name fragment. Never raises."""
    try:
        s = str(n or "").strip()
        s = s.replace("<", "").replace(">", "").replace("@", "")
        s = s.replace("#", "").replace("|", "").replace("\n", " ").replace("\r", " ")
        return " ".join(s.split())[:24]
    except Exception:
        return ""


def _extract_typed_code(content) -> str | None:
    """5-char alnum code when the whole message is just the code. Never raises."""
    try:
        c = str(content or "").strip().upper()
        if len(c) == 5 and c.isalnum():
            return c
        return None
    except Exception:
        return None


def _track_duel_thread(w: dict, thread_id: str):
    """Remember a duel thread: pairs for code-binding always; delete-guard
    only for public threads (private threads are invite-only). Never raises."""
    try:
        if not thread_id:
            return
        THREAD_PAIRS[str(thread_id)] = (str((w or {}).get("creator_id")),
                                        str((w or {}).get("opponent_id") or ""))
        if str((w or {}).get("visibility", "private")).lower() == "public":
            PUBLIC_THREADS.add(str(thread_id))
    except Exception:
        pass


def _untrack_thread(thread_id: str):
    try:
        PUBLIC_THREADS.discard(str(thread_id))
        THREAD_PAIRS.pop(str(thread_id), None)
    except Exception:
        pass


async def refresh_board_embed(bot, w: dict) -> bool:
    """Re-render the board message from stored channel/message ids.

    The board lives in the wager channel, so every state change that a player
    would look for (lock-in, each sealed match, resolve/forfeit/cancel) has to
    be reflected there — the thread is not where people look afterwards. The
    row is re-fetched so the embed always shows the committed state. Buttons
    survive only while the wager is still joinable. Returns True when the
    message was edited. Never raises.
    """
    try:
        if bot is None or not w.get("channel_id") or not w.get("message_id"):
            return False
        try:
            fresh = await get_wager(str(w.get("id") or ""))
        except Exception:
            fresh = None
        w = fresh or w
        try:
            ch = bot.get_channel(int(w["channel_id"]))
            if ch is None:
                ch = await bot.fetch_channel(int(w["channel_id"]))
        except Exception:
            return False
        if ch is None:
            return False
        try:
            msg = await ch.fetch_message(int(w["message_id"]))
        except Exception:
            return False
        mnames = _mention_names(w)
        keep = str(w.get("status")) in ("open", "pending")
        await msg.edit(embed=challenge_embed(w, mnames[0], mnames[1]),
                       view=DuelView() if keep else None)
        return True
    except Exception as e:
        print(f"[duels] board refresh by ids failed: {e}")
        return False


def _expires_rel(w) -> str:
    """Discord relative timestamp for expiry (``<t:unix:R>``), or 'soon'. Never raises."""
    try:
        ts = _parse_ts((w or {}).get("expires_at"))
        if ts is None:
            return "soon"
        return f"<t:{int(ts.timestamp())}:R>"
    except Exception:
        return "soon"


async def _dm(bot, discord_id: str, text: str):
    """DM a user, best-effort (closed DMs just log). Never raises."""
    try:
        if not bot or not discord_id:
            return
        try:
            user = await bot.fetch_user(int(discord_id))
        except Exception:
            return
        try:
            await user.send(text)
        except Exception as e:
            print(f"[duels] dm to {str(discord_id)[:6]} failed: {e}")
    except Exception as e:
        print(f"[duels] _dm failed: {e}")


async def _delete_board_message_later(bot, w, delay: int = 15):
    """Delete the wager board embed after `delay`s (expiry cleanup). Never raises."""
    try:
        await asyncio.sleep(int(delay))
        if bot is None or not w.get("channel_id") or not w.get("message_id"):
            return
        try:
            ch = bot.get_channel(int(w["channel_id"]))
            if ch is None:
                ch = await bot.fetch_channel(int(w["channel_id"]))
            if ch is None:
                return
            msg = await ch.fetch_message(int(w["message_id"]))
            await msg.delete()
            print(f"[duels] {short_wager_id(w.get('id'))} board message deleted")
        except Exception as e:
            print(f"[duels] board delete failed: {e}")
    except Exception as e:
        print(f"[duels] _delete_board_message_later failed: {e}")


def payout_public_text(pay_amount, pot, capped: bool = False, new_bal=None) -> str:
    """The thread line for a resolved wager: the FACT, never the money detail.

    No mention and no balance — the winner is DMed instead, so a resolution
    never broadcasts somebody's coins (or pings them) in a channel other
    duelists can read. Never raises.
    """
    try:
        if int(pot or 0) > 0:
            line = (f"**{int(pay_amount or 0):,}** Nectar Coins paid to the winner"
                    + (" _(daily cap reached — trimmed)_" if capped else ""))
        else:
            line = "The win goes to the duel winner 🏆\n_For fun duel — no pot._"
        return line
    except Exception:
        return "The duel resolved."


def payout_dm_text(wid_short, pay_amount, new_bal=None, capped: bool = False,
                   how: str = "") -> str:
    """The winner's DM: what they won, their new balance, and the cap rule."""
    try:
        try:
            amt = int(pay_amount or 0)
        except Exception:
            amt = 0
        head = (f"🏆 Wager `{wid_short}` resolved — you won "
                f"**{amt:,}** Nectar Coins."
                if amt > 0 else
                f"🏆 Wager `{wid_short}` resolved — you won the duel (for fun, no pot).")
        try:
            bal = (f"\nNew balance: **{int(new_bal):,}** Nectar Coins."
                   if new_bal is not None else "")
        except Exception:
            bal = ""
        cap = ("\n_Note: wager profit is capped at 50 Nectar Coins per day._"
               if capped else "")
        why = (f"\n_{str(how).strip()}_" if str(how or "").strip() else "")
        return head + bal + cap + why
    except Exception:
        return "Your duel has been resolved."


async def _settle(w: dict, winner_id: str, bot, how: str):
    """Pay the pot, mark resolved, announce + close the thread. Never raises."""
    try:
        wid = str(w.get("id"))
        wid_short = short_wager_id(wid)
        pot = int(w.get("pot") or 0)
        try:
            stake = int(w.get("stake", 0) or 0)
        except Exception:
            stake = 0
        new_bal, capped = None, False
        pay_amount = pot
        if pot > 0:
            # Daily wager-earnings cap: profit (pot minus the winner's own
            # stake) is trimmed; the stake itself always returns.
            profit = max(0, pot - stake)
            try:
                earned_today = await db.wager_profit_today(str(winner_id))
            except Exception:
                earned_today = 0
            payable = db.cap_wager_profit(profit, earned_today, WAGER_EARN_CAP)
            capped = payable < profit
            pay_amount = stake + payable
            try:
                new_bal = await _payout(str(winner_id), pay_amount, wid_short,
                                        earned=payable)
            except Exception as e:
                print(f"[duels] payout failed for {wid_short}: {e}")
                new_bal = None
        await update_wager(wid, {"status": "resolved", "winner_id": str(winner_id),
                                 "resolved_at": _now_iso()})
        # Re-read so the log and the board show the committed score, not the
        # caller's stale snapshot (which is how a resolved 1-2 duel used to
        # print "final=1-0").
        try:
            w = await get_wager(wid) or w
        except Exception:
            pass
        print(f"[duels] END {wid_short} RESOLVED via {how} winner={winner_id} "
              f"pot={pot} paid={pay_amount} capped={capped} "
              f"final={w.get('score_creator')}-{w.get('score_opponent')} "
              f"mode={w.get('mode')} race={w.get('race')}")
        thread = _resolve_thread(bot, str(w.get("thread_id") or ""))
        if thread is not None:
            try:
                e = discord.Embed(
                    title=f"🏆 Wager `{wid_short}` resolved",
                    description=payout_public_text(pay_amount, pot, capped,
                                                   new_bal) + f"\n_{how}_",
                    color=discord.Color.green())
                await _post_thread(thread, embed=e)
            except Exception as ex:
                print(f"[duels] resolve announce failed: {ex}")
            # Forfeit/cancel are abandoned duels — the thread is deleted, not
            # left as an archived ghost. A completed duel keeps its record.
            if str(how) == "forfeit":
                await _delete_thread(thread)
            else:
                await _close_thread(thread)
        # The money detail is a DM, never a public mention.
        try:
            await _dm(bot, str(winner_id),
                      payout_dm_text(wid_short, pay_amount, new_bal, capped, how))
        except Exception as e:
            print(f"[duels] payout dm to {str(winner_id)[:6]} failed: {e}")
        _untrack_thread(str(w.get("thread_id") or ""))
        # The board embed in the wager channel is the record people screenshot
        # — it has to show the final score, winner and ended state too.
        await refresh_board_embed(bot, w)
    except Exception as e:
        print(f"[duels] _settle failed: {e}")


async def _expire_wager(bot, w: dict):
    """Refund creator stake, mark expired, DM creator, tidy up, delete the
    board embed 15s later. Never raises."""
    try:
        wid = str(w.get("id"))
        wid_short = short_wager_id(wid)
        status = str(w.get("status"))
        stake = int(w.get("stake", 0) or 0)
        creator = str(w.get("creator_id"))
        if status == "open":
            await _refund(creator, stake, wid_short, "open_expired")
            await update_wager(wid, {"status": "expired",
                                     "resolved_at": _now_iso()})
            print(f"[duels] END {wid_short} EXPIRED (open, no joiner) — creator refunded stake={stake}")
            await _dm(bot, creator,
                      f"Your open wager `{wid_short}` expired with no joiner. "
                      + (f"**{stake:,}** Nectar Coins refunded." if stake > 0
                         else "It was a for-fun duel — nothing to refund."))
        elif status == "pending":
            await _refund(creator, stake, wid_short, "challenge_expired")
            await update_wager(wid, {"status": "expired",
                                     "resolved_at": _now_iso()})
            print(f"[duels] END {wid_short} EXPIRED (challenge unanswered) — creator refunded stake={stake}")
            thread = _resolve_thread(bot, str(w.get("thread_id") or ""))
            if thread is not None:
                await _post_thread(thread, content="Challenge expired — stake refunded.")
                await _close_thread(thread)
            _untrack_thread(str(w.get("thread_id") or ""))
            opp = str(w.get("opponent_id") or "")
            await _dm(bot, creator,
                      f"Your challenge to {f'<@{opp}>' if opp else 'your opponent'} "
                      f"(`{wid_short}`) expired with no answer. "
                      + (f"**{stake:,}** Nectar Coins refunded." if stake > 0
                         else "It was a for-fun duel — nothing to refund."))
        # Best-effort board embed update (deleted 15s later below).
        try:
            if bot is not None and w.get("channel_id") and w.get("message_id"):
                ch = bot.get_channel(int(w["channel_id"]))
                if ch is not None:
                    try:
                        msg = await ch.fetch_message(int(w["message_id"]))
                        e = discord.Embed(title="Wager expired",
                                          description="Stake refunded.",
                                          color=discord.Color.red())
                        await msg.edit(embed=e, view=None)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            asyncio.create_task(_delete_board_message_later(bot, w))
        except Exception as e:
            print(f"[duels] board delete schedule failed: {e}")
    except Exception as e:
        print(f"[duels] _expire_wager failed: {e}")


async def _stall_refund(bot, w: dict):
    """No shared private match in 24h — refund both stakes. Never raises."""
    try:
        wid_short = short_wager_id(w.get("id"))
        await _refund(str(w.get("creator_id")), int(w.get("stake", 0)),
                      wid_short, "stall_refund")
        if w.get("opponent_id"):
            await _refund(str(w.get("opponent_id")), int(w.get("stake", 0)),
                          wid_short, "stall_refund")
        await update_wager(str(w.get("id")), {"status": "expired",
                                              "resolved_at": _now_iso()})
        print(f"[duels] END {wid_short} STALLED 24h — both stakes refunded "
              f"(score={w.get('score_creator')}-{w.get('score_opponent')}, "
              f"sealed={len(w.get('sealed') or [])})")
        thread = _resolve_thread(bot, str(w.get("thread_id") or ""))
        if thread is not None:
            await _post_thread(thread, content=(
                "No shared private match found in 24h — both stakes refunded."))
            await _close_thread(thread)
        _untrack_thread(str(w.get("thread_id") or ""))
    except Exception as e:
        print(f"[duels] _stall_refund failed: {e}")


async def recent_event_iso(discord_id: str) -> str | None:
    """Newest bngc_game_events timestamp for one player, or None. Never raises.

    Presence evidence the AFK guard can trust: a row only exists because this
    player's own companion reported it, so it beats a heartbeat that is
    written best-effort under a 6s deadline (and is NULL for anyone who has
    never been captured).
    """
    try:
        sb = _sb()
        if sb is None or not discord_id:
            return None
        r = await db._sb_call(lambda: sb.table("bngc_game_events").select(
            "created_at").eq("discord_id", str(discord_id)).order(
            "created_at", desc=True).limit(1).execute())
        rows = (r.data if hasattr(r, "data") else []) or []
        if not rows:
            return None
        return str((rows[0] or {}).get("created_at") or "") or None
    except Exception:
        return None


async def _afk_guard(bot, w: dict) -> bool:
    """Cancel + refund when a duelist has been offline too long.

    A live duel where one player walked away can never finish (the resolver
    waits for a shared match that will not happen), so the pot sat escrowed
    for the full stall window. Anyone offline longer than
    WAGER_AFK_CANCEL_SEC after the duel went live ends it for both, with a
    refund and a thread teardown. Returns True when the wager was cancelled.

    Fails OPEN, because this is the only thing in the resolver that moves
    real coins. `last_online` is stamped best-effort on every capture batch
    under a 6s deadline and is NULL for anyone never captured, so an unknown
    stamp (NULL, unparsable, failed read) is not evidence of absence. A
    duelist with their own duel rows inside the window is demonstrably
    playing and is never counted as offline, whatever the heartbeat says.
    The trade: a duelist whose stamp was NEVER written cannot trip this
    guard at all - `/wager expires` and the 24h stall refund still cover
    abandonment. Never raises.
    """
    try:
        grace = int(os.getenv("WAGER_AFK_GRACE_SEC", "900") or 900)   # ignore the first 15 min
        limit = int(os.getenv("WAGER_AFK_CANCEL_SEC", "3600") or 3600)  # then 1h offline = cancel
        since = _parse_ts(str(w.get("active_since") or w.get("created_at") or ""))
        if since is None:
            return False
        age = (datetime.now(timezone.utc) - since).total_seconds()
        if age < grace:
            return False  # still inside the grace window
        now = datetime.now(timezone.utc)
        rows = {}
        for uid in (str(w.get("creator_id") or ""), str(w.get("opponent_id") or "")):
            if not uid:
                continue
            try:
                rows[uid] = await db.get_by_discord(uid) or {}
            except Exception:
                rows[uid] = None      # read failed: unknown, never offline
        offline = []
        for uid, row in rows.items():
            try:
                if row is None:
                    continue             # unknown heartbeat: fail open
                online, ts = db.online_status(row.get("last_online"), limit)
            except Exception:
                continue
            if not online:
                if ts is None:
                    continue             # NULL/unparsable stamp: unknown
                # their own duel rows are the stronger presence signal
                try:
                    seen = _parse_ts(await recent_event_iso(uid))
                except Exception:
                    seen = None
                if seen is not None and (now - seen).total_seconds() <= limit:
                    continue
                try:
                    idle = int((now.timestamp()
                                - float(ts.timestamp())) if ts else 10 ** 9)
                except Exception:
                    idle = 10 ** 9
                offline.append((uid, idle))
        if not offline:
            return False
        wid = str(w.get("id"))
        wid_short = short_wager_id(wid)
        who = ", ".join(f"{u}(idle {int(i/60)}m)" for u, i in offline)
        print(f"[duels] AFK {wid_short} age={int(age/60)}m offline={who} "
              f"(limit {int(limit/60)}m) — auto-cancelling, both stakes refunded")
        stake = int(w.get("stake", 0) or 0)
        for uid in (str(w.get("creator_id") or ""), str(w.get("opponent_id") or "")):
            if uid:
                await _refund(uid, stake, wid_short, "afk_cancel")
        await update_wager(wid, {"status": "cancelled", "resolved_at": _now_iso()})
        await refresh_board_embed(bot, w)
        thread = _resolve_thread(bot, str(w.get("thread_id") or ""))
        if thread is not None:
            await _delete_thread(thread)
        _untrack_thread(str(w.get("thread_id") or ""))
        try:
            for uid in rows:
                await _dm(bot, uid,
                          f"Wager `{wid_short}` was cancelled — a duelist was offline "
                          f"too long. Your stake was refunded.")
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[duels] afk guard failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------

async def duel_expiry_loop(bot):
    """Expire open/pending wagers + hydrate spectator enforcement. Never raises out."""
    await asyncio.sleep(10)
    print("[duels] expiry loop started", flush=True)
    while True:
        try:
            for w in await expirable_wagers():
                try:
                    await _expire_wager(bot, w)
                except Exception as e:
                    print(f"[duels] expiry item failed: {e}")
            # Hydrate thread tracking (pairs for code-binding + delete-guard).
            try:
                for w in await trackable_wagers(100):
                    try:
                        if w.get("thread_id"):
                            _track_duel_thread(w, str(w.get("thread_id")))
                    except Exception:
                        continue
            except Exception:
                pass
        except Exception as e:
            print(f"[duels] expiry loop tick failed: {e}")
        await asyncio.sleep(30)


async def duel_resolver_loop(bot):
    """Score active duels from bngc_game_events every SCORE_INTERVAL_SEC.

    This loop IS the match-end -> wager-resolved latency (the server writes the
    win row within a second of stG=3), so keep the interval short. Never
    raises out.
    """
    await asyncio.sleep(15)
    print(f"[duels] resolver loop started (every {int(SCORE_INTERVAL_SEC)}s)", flush=True)
    while True:
        try:
            for w in await active_wagers_for_loop(50):
                try:
                    await _score_one(bot, w)
                except Exception as e:
                    print(f"[duels] score item failed: {e}")
        except Exception as e:
            print(f"[duels] resolver tick failed: {e}")
        await asyncio.sleep(max(5, int(SCORE_INTERVAL_SEC)))


async def _score_kills_race(bot, w: dict, events, matches, mode: str,
                            first_to: int, wid: str, wid_short: str):
    """Score a first-to-N-kills (Free Mode) duel cumulatively. Never raises.

    Free Mode has no phase end: every kill of the session lands in the same
    (room, version) group, so the totals grow in place and the duel resolves
    the moment a duelist REACHES the target — decided by kill order, not by
    who is ahead when the bot happens to poll.
    """
    try:
        creator = str(w.get("creator_id"))
        opponent = str(w.get("opponent_id") or "")
        if not opponent:
            return
        # names for the roster gate
        opp_name = cre_name = ""
        try:
            opp_name = _norm_duel_name(
                ((await db.get_by_discord(opponent)) or {}).get("game_username"))
        except Exception:
            opp_name = ""
        try:
            cre_name = _norm_duel_name(
                ((await db.get_by_discord(creator)) or {}).get("game_username"))
        except Exception:
            cre_name = ""
        if not opp_name or not cre_name:
            print(f"[duels] {wid_short} kills race degraded: missing game_username "
                  f"(creator={cre_name or '-'} opponent={opp_name or '-'}) — "
                  f"kill victims cannot be roster-checked")
        tl = kills_race_timeline(events, creator, opponent, opp_name, first_to,
                                 cre_name)
        kc, ko = int(tl["kills_c"]), int(tl["kills_o"])
        if kc <= 0 and ko <= 0:
            _wlog(f"{wid_short} kills race: no roster kills yet")
            return
        # Group totals are the roster-gated view; the timeline is what decides
        # the winner. Keep them in step (they are the same rows).
        try:
            gk_c = sum(int(m.get("kills_c") or 0) for m in matches)
            gk_o = sum(int(m.get("kills_o") or 0) for m in matches)
            if (gk_c, gk_o) != (kc, ko) and (gk_c or gk_o):
                kc, ko = max(kc, gk_c), max(ko, gk_o)
        except Exception:
            pass
        prev_c = int(w.get("kills_creator") or 0)
        prev_o = int(w.get("kills_opponent") or 0)
        room = str((w.get("room_id") or (matches[0].get("room_id") if matches else "")) or "")
        winner = tl.get("winner")
        try:
            w2 = await get_wager(wid) or w
        except Exception:
            w2 = w
        cname, oname = _mention_names(w2)
        entry = kill_race_seal(room, 0, kc, ko, winner)
        if winner:
            entry["winner_name"] = cname if str(winner) == creator else oname
        sealed = list(w.get("sealed") or [])
        # one growing entry for the whole duel
        replaced = False
        for i, s in enumerate(sealed):
            if isinstance(s, dict) and s.get("race") == "kills" and str(s.get("room_id") or "") == room:
                sealed[i] = entry
                replaced = True
                break
        if not replaced:
            sealed.append(entry)
        print(f"[duels] KILLS {wid_short} room={room[:8]} {kc}-{ko} "
              f"(first to {first_to}) "
              + (f"-> {winner} reached the target" if winner
                 else f"delta={kc - prev_c}/{ko - prev_o}"))
        await update_wager(wid, {"kills_creator": kc, "kills_opponent": ko,
                                 "sealed": sealed})
        thread = _resolve_thread(bot, str(w.get("thread_id") or ""))
        if thread is not None:
            try:
                await _post_thread(thread, embed=score_embed(w2, cname, oname))
            except Exception:
                pass
        await refresh_board_embed(bot, w2)
        if winner:
            return await _settle(w2, str(winner), bot,
                                 how=f"first to {first_to} kills")
        return "kills"
    except Exception as e:
        print(f"[duels] {wid_short} kills race failed: {e}")
        return None


async def _score_one(bot, w: dict):
    """Score a single active duel. Never raises."""
    try:
        wid = str(w.get("id"))
        wid_short = short_wager_id(wid)
        creator, opponent = str(w.get("creator_id")), str(w.get("opponent_id") or "")
        if not opponent:
            return
        mode = str(w.get("mode") or "zw_lg")
        race = str(w.get("race") or race_for_mode(mode))
        first_to = int(w.get("first_to") or 1)
        since_iso = str(w.get("active_since") or w.get("created_at") or "")
        # AFK guard first: a dead duel can never be scored, so don't keep
        # querying events for it.
        if await _afk_guard(bot, w):
            return
        # Stall: 24h with no winner.
        try:
            start = _parse_ts(since_iso)
            if start is not None and (datetime.now(timezone.utc) - start).total_seconds() > STALL_AFTER_SEC:
                return await _stall_refund(bot, w)
        except Exception:
            pass
        events = await duel_events(creator, opponent, since_iso)
        if not events:
            _wlog(f"{wid_short} tick rows=0 (no events since {since_iso})")
            return
        # Which room is this duel in NOW? A crash + void sends both players
        # back to the lobby and the next room has a new room_id (same or new
        # code), so a one-shot binding froze the score forever. Bind/rebind
        # BEFORE grouping, because grouping filters on room_id - and on the
        # wager's game mode, which is why the mode rides along here.
        try:
            _rooms = room_evidence(events, since_iso)
            _bound = str(w.get("room_id") or "")
            _bound_row = next((r for r in _rooms
                               if str(r.get("room_id")) == _bound), None)
            # why the old room lost the binding decides the log line
            _mode_lost = bool(_bound_row) and not _room_mode_ok(mode, _bound_row)
            _room, _code, _act = choose_bound_room(
                _bound, _rooms, _now_iso(), str(w.get("room_code") or ""), mode)
            if _act in ("bind", "rebind") and _room:
                if _act == "rebind":
                    print(f"[duels] {wid_short} REBIND room="
                          f"{_bound[:8] or '-'} -> {_room[:8]} "
                          f"code={_code or '-'} mode={mode} "
                          f"({'bound room was another game mode' if _mode_lost else 'bound room went quiet'})")
                else:
                    print(f"[duels] {wid_short} bound to room {_room[:8]}")
                _patch = {"room_id": _room}
                if _code and str(w.get("room_code") or "") != _code:
                    _patch["room_code"] = _code
                await update_wager(wid, _patch)
                w = {**w, **_patch}
        except Exception as e:
            print(f"[duels] {wid_short} room bind failed: {e}")
        mobile = bool(w.get("is_mobile_duel"))
        _trace = [] if _wdebug() else None
        if mobile:
            # Cross-verified: opponent has no rows — score by game name
            # against the tracked creator's room events.
            opp_norm = ""
            try:
                orow = await db.get_by_discord(opponent)
                opp_norm = _norm_duel_name((orow or {}).get("game_username"))
            except Exception:
                opp_norm = ""
            if not opp_norm:
                print(f"[duels] {wid_short} mobile duel, no opponent game name yet")
                return
            matches = group_mobile_matches(events, creator, opp_norm, mode,
                                           w.get("room_id"), w.get("room_code"),
                                           since_iso, trace=_trace)
        else:
            # Roster gate needs both duelists' game names: only kills on the
            # OTHER duelist count.
            _opp_name = _cre_name = ""
            try:
                _orow = await db.get_by_discord(opponent)
                _opp_name = _norm_duel_name((_orow or {}).get("game_username"))
            except Exception:
                _opp_name = ""
            try:
                _crow = await db.get_by_discord(creator)
                _cre_name = _norm_duel_name((_crow or {}).get("game_username"))
            except Exception:
                _cre_name = ""
            if not _opp_name or not _cre_name:
                print(f"[duels] {wid_short} roster gate degraded: missing "
                      f"game_username (creator={_cre_name or '-'} "
                      f"opponent={_opp_name or '-'}) — kill victims cannot be "
                      f"roster-checked")
            matches = group_duel_matches(events, creator, opponent, mode,
                                         w.get("room_id"), w.get("room_code"),
                                         since_iso, trace=_trace,
                                         opp_name=_opp_name,
                                         creator_name=_cre_name)
        if _trace is not None and _trace:
            _wlog(f"{wid_short} tick rows={len(events)} matches={len(matches)} "
                  f"drops={len(_trace)}")
            for _t in _trace[:25]:
                _wlog(f"  {wid_short} drop {_t}")
        # Creator-voided matches never score again (crash / false start).
        _voided = w.get("voided") or []
        if _voided:
            _before = len(matches)
            matches = [m for m in matches
                       if not is_voided(m.get("room_id"), m.get("match_version"),
                                        _voided)]
            if len(matches) != _before:
                print(f"[duels] {wid_short} skipped {len(_voided)} voided match(es)")
        if not matches:
            if any(str((e or {}).get("event_type") or "") in
                   ("kill", "killfeed", "win", "death") for e in (events or [])):
                print(f"[duels] {wid_short} NO MATCH: {len(events)} combat/win rows "
                      f"but 0 groupable matches (mode={mode} race={race} "
                      f"bound_room={str(w.get('room_id') or '-')[:8]} "
                      f"bound_code={w.get('room_code') or '-'}) — "
                      f"check pub-vs-private, mode mismatch, room binding, "
                      f"or a zone/fall death outside the bound room")
            else:
                _wlog(f"{wid_short} tick rows={len(events)} but no combat rows yet")
            return
        _summ = []
        for _m in matches:
            try:
                _summ.append("room={}/v{}/k{}-{}{}{}".format(
                    str(_m["room_id"])[0:8], _m["match_version"],
                    _m["kills_c"], _m["kills_o"],
                    "/WIN" if _m.get("win_id") else "/nowin",
                    "/OPPWIN" if _m.get("opponent_win") else ""))
            except Exception:
                pass
        _wlog("{} tick rows={} matches={} ({})".format(
            wid_short, len(events), len(matches), ", ".join(_summ)))
        # (room binding happens before grouping, from the freshest evidence)
        # Free Mode: stG never advances, so the duel is ONE growing match and
        # the sealed-once version logic below would freeze the score at the
        # first kill. Score it cumulatively instead.
        if race == "kills":
            return await _score_kills_race(
                bot, w, events, matches, mode, first_to, wid, wid_short)
        sealed = list(w.get("sealed") or [])
        seen = {(str(s.get("room_id")), int(s.get("match_version"))) for s in sealed
                if s.get("room_id") is not None}
        fresh = [m for m in matches
                 if (str(m["room_id"]), int(m["match_version"])) not in seen
                 and _match_sealable(m, race)]
        # Wins-race matches that had combat but no win row: the server denied
        # the phase-end win (see `PROCESSOR WIN denied`). Say so explicitly —
        # this is the "match not tracked / wins denied" symptom. Mobile groups
        # whose last strike was the opponent eliminating the tracked player are
        # NOT denials (their win row can never exist) — they seal above.
        if race != "kills":
            for m in matches:
                if (str(m["room_id"]), int(m["match_version"])) in seen:
                    continue
                if m.get("opponent_win"):
                    continue
                if not m.get("win_id") and (int(m["kills_c"]) + int(m["kills_o"])) > 0:
                    print(f"[duels] WIN-DENIED? {wid_short} room={str(m['room_id'])[:8]} "
                          f"v={m['match_version']} had {m['kills_c']}-{m['kills_o']} kills "
                          f"but NO win row — server may have denied the phase-end win "
                          f"(grep 'PROCESSOR WIN denied' in server logs); not sealed")
        if not fresh:
            _wlog(f"{wid_short} tick nothing new to seal "
                  f"(matches={len(matches)} all already sealed or not sealable)")
            return
        score_c = int(w.get("score_creator") or 0)
        score_o = int(w.get("score_opponent") or 0)
        kills_c = int(w.get("kills_creator") or 0)
        kills_o = int(w.get("kills_opponent") or 0)
        thread = _resolve_thread(bot, str(w.get("thread_id") or ""))
        if thread is None:
            # Repair: went live while thread creation was broken — create it
            # now so sealed results actually land in a thread.
            try:
                thread, _ww = await _ensure_duel_thread(bot, w)
                if _ww:
                    w = _ww
            except Exception as e:
                print(f"[duels] seal thread repair failed: {e}")
        cname, oname = _mention_names(w)
        for m in fresh:
            _wsafe = lambda s: str(s or "").replace("\r", " ").replace("\n", " ")
            print(f"[duels] MATCH {wid_short} room={str(m['room_id'])[:8]} "
                  f"v={m['match_version']} gm={m.get('gm')} code={m.get('code') or '-'} "
                  f"race={race} kills={m['kills_c']}-{m['kills_o']} "
                  f"win_id={_wsafe(m.get('win_id'))} "
                  f"opponent_win={bool(m.get('opponent_win'))} "
                  f"(creator={_wsafe(creator)} opponent={_wsafe(opponent)})")
            winner = score_match(m["kills_c"], m["kills_o"], m.get("win_id"),
                                 creator, opponent, race=race,
                                 opponent_win=bool(m.get("opponent_win")))
            # bf / zw_lg win validation: a win row only decides a match that
            # actually saw combat between the duelists. A win with zero
            # roster kills means the "winner" never fought (walkover, afk
            # quit, misattributed win) — that must never score. A tracked
            # death IS combat evidence: zone/fall damage kills arrive with no
            # killfeed and therefore no kill counts at all.
            _combat = (int(m["kills_c"]) + int(m["kills_o"])
                       + int(m.get("death_c") or 0))
            if race != "kills" and winner is not None and _combat == 0:
                print(f"[duels] WIN-REJECTED {wid_short} room={str(m['room_id'])[:8]} "
                      f"v={m['match_version']} winner={winner} but 0 roster kills — "
                      f"win without combat ignored (mode={mode})")
                winner = None
            kills_c += int(m["kills_c"])
            kills_o += int(m["kills_o"])
            if race == "kills":
                winner_name = cname if winner == creator else (
                    oname if winner == opponent else "Draw")
            else:
                if winner == creator:
                    score_c += 1
                    winner_name = cname
                elif winner == opponent:
                    score_o += 1
                    winner_name = oname
                else:
                    winner_name = "Draw"
            # WIN / DEFEAT / DRAW trace — the evidence behind every call.
            if winner == creator:
                print(f"[duels] WIN {wid_short} room={str(m['room_id'])[:8]} "
                      f"v={m['match_version']} -> {creator} ({_wsafe(cname)}) "
                      f"race={race} kills={m['kills_c']}-{m['kills_o']} "
                      f"score={score_c}-{score_o}/{first_to}")
            elif winner == opponent:
                print(f"[duels] DEFEAT {wid_short} room={str(m['room_id'])[:8]} "
                      f"v={m['match_version']} -> {opponent} ({_wsafe(oname)}) wins, "
                      f"{creator} ({_wsafe(cname)}) loses "
                      f"race={race} kills={m['kills_c']}-{m['kills_o']} "
                      f"score={score_c}-{score_o}/{first_to}")
            else:
                print(f"[duels] DRAW {wid_short} room={str(m['room_id'])[:8]} "
                      f"v={m['match_version']} race={race} "
                      f"kills={m['kills_c']}-{m['kills_o']} win_id="
                      f"{_wsafe(m.get('win_id'))} — no score awarded")
            seal = {"room_id": m["room_id"], "match_version": int(m["match_version"]),
                    "winner": winner, "winner_name": winner_name,
                    "kills_c": int(m["kills_c"]), "kills_o": int(m["kills_o"]),
                    "gm": m.get("gm"), "code": m.get("code")}
            sealed.append(seal)
            print(f"[duels] {wid_short} sealed v{seal['match_version']} winner={winner_name}")
            if thread is not None:
                try:
                    await _post_thread(
                        thread,
                        embed=sealed_match_embed(len(sealed), seal, cname, oname))
                except Exception:
                    pass
        await update_wager(wid, {"sealed": sealed, "score_creator": score_c,
                                 "score_opponent": score_o,
                                 "kills_creator": kills_c, "kills_opponent": kills_o})
        print(f"[duels] SCORE {wid_short} {score_c}-{score_o} (first to {first_to}, "
              f"race={race}) total-kills {kills_c}-{kills_o} sealed={len(sealed)}")
        # Victory check.
        champ = None
        if race == "kills":
            if kills_c >= first_to and kills_c > kills_o:
                champ = creator
            elif kills_o >= first_to and kills_o > kills_c:
                champ = opponent
        else:
            if score_c >= first_to and score_c > score_o:
                champ = creator
            elif score_o >= first_to and score_o > score_c:
                champ = opponent
        if champ is not None:
            w2 = await get_wager(wid)
            return await _settle(w2 or {**w, "sealed": sealed}, champ, bot,
                                 how=f"{_mode_label(mode)} first to {first_to}")
        # Live score update in thread.
        if thread is not None and fresh:
            try:
                w2 = await get_wager(wid)
                if w2:
                    names = cname, oname
                    await _post_thread(thread, embed=score_embed(w2, names[0], names[1]))
            except Exception:
                pass
        # ...and on the board, so the wager channel always shows the current
        # score instead of a stale "first to N" card.
        if fresh:
            await refresh_board_embed(bot, w)
    except Exception as e:
        print(f"[duels] _score_one failed: {e}")


# ---------------------------------------------------------------------------
# In-thread handling: typed room codes + public-fallback spectator guard
# ---------------------------------------------------------------------------

async def trackable_wagers(limit: int = 100):
    """Open/pending/active wagers that own a thread. Never raises."""
    sb = _sb()
    if sb is None:
        return []
    try:
        r = await db._sb_call(lambda: sb.table("bngc_duel_wagers").select(
            "id,thread_id,creator_id,opponent_id,visibility,status")
            .in_("status", list(ACTIVE_STATUSES))
            .neq("thread_id", "").order("created_at", desc=False)
            .limit(int(limit)).execute())
        return (r.data if hasattr(r, "data") else []) or []
    except Exception as e:
        print(f"[duels] trackable_wagers failed: {e}")
        return []


async def _duel_on_message(message: discord.Message):
    """Typed room codes (duelists) + spectator guard (public fallbacks).

    A duelist typing the whole 5-char code as a message binds it to the
    wager (re-typable until the first match seals). Duelists are never
    deleted; mods are never deleted. Never raises.
    """
    try:
        if getattr(message.author, "bot", False):
            return
        ch = getattr(message, "channel", None)
        if ch is None or not isinstance(ch, discord.Thread):
            return
        tid = str(getattr(ch, "id", ""))
        pair = THREAD_PAIRS.get(tid)
        if pair is None:
            # Restart race: resolve once from the DB.
            w0 = await find_wager_by_thread(tid)
            if not w0:
                return
            _track_duel_thread(w0, tid)
            pair = THREAD_PAIRS.get(tid)
            if pair is None:
                return
        author_id = str(getattr(message.author, "id", ""))
        is_duelist = author_id in (str(pair[0]), str(pair[1]))
        # 1. typed room code — duelists only.
        if is_duelist:
            code = _extract_typed_code(getattr(message, "content", ""))
            if code:
                w = await find_wager_by_thread(tid)
                if w is not None and str(w.get("status")) in ("open", "pending", "active"):
                    if (w.get("sealed") or []):
                        try:
                            await ch.send("Room code is locked — matches already sealed.",
                                          delete_after=10)
                        except Exception:
                            pass
                    elif str(w.get("room_code") or "").upper() != code:
                        await update_wager(str(w.get("id")), {"room_code": code})
                        print(f"[duels] {short_wager_id(w.get('id'))} code bound "
                              f"to {code} by {author_id[:6]}")
                        try:
                            await ch.send(f"Room code bound: `{code}` — "
                                          f"private-room matches there score the duel.")
                        except Exception:
                            pass
            return  # duelists' messages are never deleted.
        # 2. public-fallback threads stay spectator read-only (private
        # threads are invite-only, so this only ever fires on fallbacks).
        if tid not in PUBLIC_THREADS:
            return
        try:
            member = getattr(message, "author", None)
            perms = getattr(ch, "permissions_for", None)
            if perms is not None and member is not None:
                try:
                    p = ch.permissions_for(member)
                    if p.administrator or p.manage_threads:
                        return
                except Exception:
                    pass
        except Exception:
            pass
        try:
            await message.delete()
            warn = (f"{getattr(message.author, 'mention', 'Spectator')} — this duel "
                    f"thread is read-only for spectators.")
            try:
                await ch.send(warn, delete_after=10)
            except Exception:
                pass
            print(f"[duels] removed spectator msg in thread {tid[:8]}")
        except discord.Forbidden:
            pass
    except Exception as e:
        print(f"[duels] on_message guard failed: {e}")


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

def _board_channel(interaction: discord.Interaction):
    """Wager channel first, command channel as fallback. Never raises (logs)."""
    try:
        cid = _wager_channel_id()
        if cid and interaction.guild:
            if not cid.isdigit():
                print(f"[duels] WAGER_CHANNEL_ID={cid!r} is not numeric — "
                      f"falling back to command channel")
            else:
                ch = interaction.guild.get_channel(int(cid))
                if ch is not None:
                    return ch
                print(f"[duels] WAGER_CHANNEL_ID={cid} not found in guild "
                      f"{interaction.guild.id} — falling back to command channel")
        elif not cid:
            print("[duels] WAGER_CHANNEL_ID unset — posting in command channel")
    except Exception as e:
        print(f"[duels] board channel resolve failed: {e}")
    return interaction.channel


def register(tree: app_commands.CommandTree, bot=None):
    """Register /wager-* commands. Call once at import time in bot.py."""
    if bot is not None:
        try:
            bot.add_listener(_duel_on_message, "on_message")
            print("[duels] thread listener attached (typed codes + spectator guard)")
        except Exception as e:
            print(f"[duels] listener attach failed: {e}")

    wager_group = app_commands.Group(
        name="wager", description="Nectar-Coin duel wagers (targeted + open)")

    @wager_group.command(name="create",
                         description="Create a Nectar-Coin duel wager (targeted or open)")
    @app_commands.describe(
        stake="Coins each duelist stakes (0 = for fun, no pot)",
        first_to="First to this many match wins (zw_lg/bf) or points (DF), no cap",
        mode="Game mode for the duel",
        opponent="Challenge a specific player (omit for an open wager)")
    @app_commands.choices(
        mode=[app_commands.Choice(name=n, value=v) for n, v in MODE_CHOICES])
    async def wager_create_cmd(
            interaction: discord.Interaction,
            stake: app_commands.Range[int, 0, 100000],
            first_to: int,
            mode: str,
            opponent: discord.Member | None = None):
        await interaction.response.defer(ephemeral=True)
        try:
            nmode = normalize_mode(mode)
            if nmode is None:
                return await interaction.followup.send(
                    "Pick a mode: Late Game Zonewars, Boxfights or Free Mode.",
                    ephemeral=True)
            ok, err = validate_stake(stake)
            if not ok:
                return await interaction.followup.send(err, ephemeral=True)
            ok, err = validate_first_to(first_to)
            if not ok:
                return await interaction.followup.send(err, ephemeral=True)
            if opponent is not None:
                if opponent.bot:
                    return await interaction.followup.send(
                        "You can't duel bots.", ephemeral=True)
                if str(opponent.id) == str(interaction.user.id):
                    return await interaction.followup.send(
                        "You can't duel yourself.", ephemeral=True)
            me = str(interaction.user.id)
            ok, reason = await check_eligibility(me, int(stake))
            if not ok:
                return await interaction.followup.send(reason, ephemeral=True)
            if opponent is not None:
                orow = await db.get_by_discord(str(opponent.id))
                if not orow:
                    return await interaction.followup.send(
                        f"{opponent.mention} needs **/register** first.", ephemeral=True)
                busy = await get_user_active(str(opponent.id))
                if busy:
                    return await interaction.followup.send(
                        f"{opponent.display_name} already has an open wager.",
                        ephemeral=True)
            is_open = opponent is None
            wid_short_preview = uuid.uuid4().hex[:8]
            try:
                await _escrow(me, int(stake), wid_short_preview)
            except Exception as e:
                return await interaction.followup.send(str(e)[:160], ephemeral=True)
            payload = {
                "guild_id": str(interaction.guild.id) if interaction.guild else "",
                "kind": "open" if is_open else "targeted",
                "status": "open" if is_open else "pending",
                "channel_id": "",
                "message_id": "",
                "thread_id": "",
                "creator_id": me,
                "opponent_id": str(opponent.id) if opponent else None,
                "stake": int(stake),
                "pot": int(stake) if is_open else int(stake),
                "mode": nmode,
                "first_to": int(first_to),
                "race": race_for_mode(nmode),
                "visibility": "private",
                "room_code": None,  # typed in-thread by a duelist (see _duel_on_message)
                "expires_at": (datetime.now(timezone.utc)
                               + timedelta(seconds=OPEN_EXPIRY_SEC if is_open
                                           else ACCEPT_TIMEOUT_SEC)).isoformat(),
            }
            w, err = await create_wager_row(payload)
            if w is None:
                await _refund(me, int(stake), wid_short_preview, "create_failed")
                return await interaction.followup.send(err, ephemeral=True)
            wid_short = short_wager_id(w.get("id"))
            print(f"[duels] /wager-create {wid_short} by {me} "
                  f"{'open' if is_open else 'vs ' + str(opponent.id)} "
                  f"{nmode} first_to={first_to}")
            # Board message: EVERY wager posts in the wager channel
            # (WAGER_CHANNEL_ID, current channel as fallback). A targeted
            # challenge gets NO thread and pulls nobody in — the challenged
            # player is DM'd and the thread opens when they accept.
            board = _board_channel(interaction)
            print(f"[duels] {wid_short} board -> channel {getattr(board, 'id', '?')}")
            cname = interaction.user.display_name
            oname = opponent.display_name if opponent else None
            mname, moname = _mention_names({**w, "opponent_id": str(opponent.id) if opponent else None})
            view = DuelView()
            try:
                msg = await board.send(embed=challenge_embed(w, mname, moname),
                                       view=view)
            except Exception as e:
                print(f"[duels] board post failed: {e}")
                await _refund(me, int(stake), wid_short, "board_failed")
                await update_wager(str(w.get("id")), {"status": "cancelled",
                                                      "resolved_at": _now_iso()})
                return await interaction.followup.send(
                    "Could not post the wager — stake refunded.", ephemeral=True)
            # No duel thread is created at challenge time — for ANY kind. An
            # open wager gets its thread when someone joins, a targeted one
            # when the challenged player accepts. Creating it here and force-
            # adding the challenged player pulled them into a duel they had not
            # accepted (and made Accept fail its own eligibility check).
            thread_id = ""
            thread_priv = True
            await update_wager(str(w.get("id")), {
                "channel_id": str(getattr(board, "id", "") or ""),
                "message_id": str(getattr(msg, "id", "") or ""),
                "thread_id": thread_id,
                "visibility": "private" if thread_priv else "public",
                "pot": int(stake) * (1 if is_open else 1),
            })
            if opponent is not None:
                # DM only. The board message carries Accept / Decline.
                try:
                    await _dm(
                        interaction.client, str(opponent.id),
                        challenge_dm_text(cname, oname, int(first_to),
                                          int(stake) * 2, int(stake),
                                          f"<#{getattr(board, 'id', '')}>",
                                          wid_short))
                    print(f"[duels] {wid_short} challenge DM sent to "
                          f"{opponent.id} (no thread until accepted)")
                except Exception as e:
                    print(f"[duels] challenge dm failed: {e}")
            where = f"<#{getattr(board, 'id', '')}>"
            _st0 = int(stake or 0)
            await interaction.followup.send(
                f"Wager `{wid_short}` posted {where} — "
                + (f"for fun duel (no stakes)." if _st0 <= 0
                   else f"**{_st0:,}** Nectar Coins escrowed.")
                + (" DM sent — waiting for Accept." if opponent is not None
                   else " Open to anyone — first join locks it in."),
                ephemeral=True)
        except Exception as e:
            print(f"[duels] wager-create failed: {e}")
            try:
                await interaction.followup.send("Something went wrong — try again.",
                                                ephemeral=True)
            except Exception:
                pass

    @wager_group.command(name="board",
                         description="Open wagers waiting for a joiner")
    async def wager_board_cmd(interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            gid = str(interaction.guild.id) if interaction.guild else ""
            rows = await list_open_wagers(gid, 10)
            if not rows:
                return await interaction.followup.send(
                    "No open wagers — create one with **/wager create** (omit opponent).")
            lines = []
            for w in rows:
                try:
                    nmode = _mode_label(w.get("mode"))
                    race = "wins" if w.get("race") == "wins" else "points"
                    try:
                        _st = int(w.get("stake", 0) or 0)
                    except Exception:
                        _st = 0
                    _tail = ("For fun" if _st <= 0
                             else f"**{_st:,}** Nectar Coins · ")
                    lines.append(
                        f"`{short_wager_id(w.get('id'))}` — {_tail}"
                        f"{nmode} first to {int(w.get('first_to', 1))} {race}")
                except Exception:
                    continue
            e = discord.Embed(title="Open Nectar Coins Wagers",
                              description="\n".join(lines) or "—",
                              color=discord.Color.gold())
            e.set_footer(text="Join from the wager channel message (Join button). 15-min expiry.")
            await interaction.followup.send(embed=e)
        except Exception as e:
            print(f"[duels] wager-board failed: {e}")
            try:
                await interaction.followup.send("Something went wrong — try again.")
            except Exception:
                pass

    @wager_group.command(name="status",
                         description="Show a duel wager (score, seals, expiry)")
    @app_commands.describe(wager_id="Wager id (first 8 chars are enough)")
    async def wager_status_cmd(interaction: discord.Interaction, wager_id: str):
        await interaction.response.defer()
        try:
            w = await get_wager(wager_id)
            if not w:
                return await interaction.followup.send("Wager not found.")
            cname, oname = _mention_names(w)
            await interaction.followup.send(
                embed=score_embed(w, cname, oname))
        except Exception as e:
            print(f"[duels] wager-status failed: {e}")
            try:
                await interaction.followup.send("Something went wrong — try again.")
            except Exception:
                pass

    @wager_group.command(name="stats",
                         description="Wager record: wins, losses, coins, match score")
    @app_commands.describe(user="Player to look up (omit for yourself)")
    async def wager_stats_cmd(interaction: discord.Interaction,
                              user: discord.User | None = None):
        await interaction.response.defer()
        try:
            target = user or interaction.user
            uid = str(target.id)
            rows = await user_wagers(uid)
            if not rows:
                return await interaction.followup.send(
                    f"{target.mention} has no duels yet."
                    if user else "You have no duels yet — `/wager create` to start one.")
            balance = None
            try:
                info = await db.get_coin_balance(uid)
                balance = int((info or {}).get("balance") or 0)
            except Exception:
                balance = None
            try:
                name = getattr(target, "display_name", None) or str(target)
            except Exception:
                name = str(target)
            await interaction.followup.send(
                embed=wager_stats_embed(name, summarize_wagers(rows, uid), balance))
        except Exception as e:
            print(f"[duels] wager-stats failed: {e}")
            try:
                await interaction.followup.send("Something went wrong — try again.")
            except Exception:
                pass

    @wager_group.command(name="set-code",
                         description="Force the resolver onto a room code (duelist escape hatch)")
    @app_commands.describe(wager_id="Wager id (first 8 chars are enough)",
                           code="5-character room code (blank clears it)")
    async def wager_set_code_cmd(interaction: discord.Interaction, wager_id: str,
                                 code: str):
        await interaction.response.defer(ephemeral=True)
        try:
            w = await get_wager(wager_id)
            if not w:
                return await interaction.followup.send("Wager not found.", ephemeral=True)
            me = str(interaction.user.id)
            if me not in (str(w.get("creator_id")), str(w.get("opponent_id") or "")):
                return await interaction.followup.send(
                    "Only the duelists can set the room code.", ephemeral=True)
            if str(w.get("status")) not in ("open", "pending", "active"):
                return await interaction.followup.send(
                    "That wager is closed.", ephemeral=True)
            if (w.get("sealed") or []):
                return await interaction.followup.send(
                    "Room code is locked - matches already sealed.", ephemeral=True)
            ok, err = validate_room_code(code)
            if not ok:
                return await interaction.followup.send(
                    str(err or "Bad code."), ephemeral=True)
            want = str(code or "").strip().upper()
            # pull the binding across too, or a stale room keeps filtering
            rooms = []
            try:
                rows = await duel_events(str(w.get("creator_id")),
                                         str(w.get("opponent_id") or ""),
                                         str(w.get("active_since")
                                             or w.get("created_at") or ""))
                rooms = room_evidence(rows, str(w.get("active_since")
                                                or w.get("created_at") or ""))
            except Exception:
                rooms = []
            patch = set_code_patch(rooms, want, str(w.get("room_id") or ""))
            if not await update_wager(str(w.get("id")), patch):
                return await interaction.followup.send(
                    "Couldn't save that -try again.", ephemeral=True)
            short = short_wager_id(w.get("id"))
            print(f"[duels] {short} code set to {want or '-'} by {me[:6]} "
                  f"(room={str(patch.get('room_id') or '-')[:8]})")
            if not want:
                return await interaction.followup.send(
                    f"Cleared the room code on `{short}`.", ephemeral=True)
            if patch.get("room_id"):
                return await interaction.followup.send(
                    f"Room code set to `{want}` on `{short}` - now tracking "
                    f"room `{str(patch['room_id'])[:8]}`.", ephemeral=True)
            return await interaction.followup.send(
                f"Room code set to `{want}` on `{short}`. No room is running "
                f"that code yet - tracking starts as soon as it does.",
                ephemeral=True)
        except Exception as e:
            print(f"[duels] wager-set-code failed: {e}")
            try:
                await interaction.followup.send("Something went wrong -try again.",
                                                ephemeral=True)
            except Exception:
                pass

    @wager_group.command(name="reset",
                         description="Re-point a backed-out duel at your new room")
    @app_commands.describe(wager_id="Wager id (first 8 chars are enough)",
                           code="New room code (optional - skips the /wager set-code step)")
    async def wager_reset_cmd(interaction: discord.Interaction, wager_id: str,
                              code: str = ""):
        """Recover a duel after backing out to the lobby and rejoining.

        Backing out leaves the wager bound to a room nobody is in, and the
        `active_since` it was stamped with keeps two clocks already spent - the
        AFK grace and the 24h stall - so the first tick can cancel a duelist
        for being "offline" and the wager dies before it tracks anything. This
        moves the binding and the clock in one step, and takes the new code
        with it so it replaces /wager set-code.

        It never touches the score: `sealed`, `score_*`, `kills_*` and
        `voided` survive, so a duelist cannot reset their way out of a loss.

        A live wager is the duelists' own to fix. A CANCELLED or EXPIRED one
        is admin-only, because cancel already refunded both stakes and
        reviving it re-takes them.
        """
        await interaction.response.defer(ephemeral=True)
        try:
            w = await get_wager(wager_id, include_ended=True)
            if not w:
                return await interaction.followup.send("Wager not found.", ephemeral=True)
            me = str(interaction.user.id)
            is_duelist = me in (str(w.get("creator_id")),
                                str(w.get("opponent_id") or ""))
            try:
                is_admin = bool(interaction.user.guild_permissions.administrator)
            except Exception:
                is_admin = False
            why = reset_refusal(w, is_duelist=is_duelist, is_admin=is_admin)
            if why:
                return await interaction.followup.send(why, ephemeral=True)
            wid = str(w.get("id"))
            short = short_wager_id(wid)
            creator = str(w.get("creator_id") or "")
            opponent = str(w.get("opponent_id") or "")
            stake = int(w.get("stake", 0) or 0)

            ok, err = validate_room_code(code) if str(code or "").strip() else (True, None)
            if not ok:
                return await interaction.followup.send(str(err or "Bad code."),
                                                       ephemeral=True)

            # Room evidence, for both the code hand-off and the freshest-room
            # fallback. Best effort: with no evidence the patch just releases
            # the binding and the resolver rebinds when a room shows up.
            rooms = []
            try:
                rows = await duel_events(creator, opponent,
                                         str(w.get("active_since")
                                             or w.get("created_at") or ""))
                rooms = room_evidence(rows, str(w.get("active_since")
                                                or w.get("created_at") or ""))
            except Exception:
                rooms = []

            patch = reset_patch(w, now_iso=_now_iso(), code=code, rooms=rooms)

            # Reviving a closed wager: re-take BOTH stakes before the status
            # flips, or the pot is unfunded and settling it mints coins.
            re_escrowed = []
            if reset_needs_escrow(w):
                for uid in (creator, opponent):
                    busy = await get_user_active(uid, ignore_id=wid)
                    if busy:
                        return await interaction.followup.send(
                            f"<@{uid}> is already in another live wager "
                            f"(`{short_wager_id(busy.get('id'))}`) - they would be "
                            "in two duels at once.", ephemeral=True)
                    try:
                        row = await db.get_by_discord(uid)
                    except Exception:
                        row = None
                    if not row:
                        return await interaction.followup.send(
                            f"<@{uid}> has no account - cannot re-take the stake.",
                            ephemeral=True)
                    if int(row.get("nectar_coins", 0) or 0) < stake:
                        return await interaction.followup.send(
                            f"<@{uid}> has {row.get('nectar_coins', 0)} coins but the "
                            f"stake is {stake} - reviving this wager re-takes it, so "
                            "they need the balance first.", ephemeral=True)
                try:
                    for uid in (creator, opponent):
                        await _escrow(uid, stake, short)
                        re_escrowed.append(uid)
                except Exception as e:
                    for uid in re_escrowed:
                        await _refund(uid, stake, short, "reset_escrow_failed")
                    return await interaction.followup.send(
                        f"Couldn't re-take the stakes - nothing changed: "
                        f"{str(e)[:160]}", ephemeral=True)
                patch["status"] = "active"

            if not await update_wager(wid, patch):
                for uid in re_escrowed:
                    await _refund(uid, stake, short, "reset_update_failed")
                return await interaction.followup.send(
                    "Couldn't save that - try again.", ephemeral=True)

            try:
                await refresh_board_embed(interaction.client, w)
            except Exception:
                pass
            revived = "Revived" if re_escrowed else "Reset"
            where = (f"room `{str(patch.get('room_id') or '')[:8]}`"
                     if patch.get("room_id") else "the next room that shows up")
            line = (f"{revived} `{short}`: now tracking {where}"
                    + (f" under code `{patch['room_code']}`"
                       if patch.get("room_code") else "")
                    + ". Score and sealed matches were kept.")
            print(f"[duels] {short} reset by {me[:6]} "
                  f"room={str(patch.get('room_id') or '-')[:8]} "
                  f"code={patch.get('room_code') or '-'} "
                  f"revived={bool(re_escrowed)}")
            try:
                for uid in (creator, opponent):
                    if uid and uid != me:
                        await _dm(interaction.client, uid,
                                  f"Your duel `{short}` was reset by <@{me}> and is "
                                  f"live again - rejoin the room it names.")
            except Exception:
                pass
            return await interaction.followup.send(line, ephemeral=True)
        except Exception as e:
            print(f"[duels] wager-reset failed: {e}")
            try:
                await interaction.followup.send("Something went wrong - try again.",
                                                ephemeral=True)
            except Exception:
                pass

    @wager_group.command(name="forfeit",
                         description="Forfeit your live duel (opponent takes the pot)")
    @app_commands.describe(wager_id="Wager id (first 8 chars are enough)")
    async def wager_forfeit_cmd(interaction: discord.Interaction, wager_id: str):
        await interaction.response.defer(ephemeral=True)
        try:
            w = await get_wager(wager_id)
            if not w:
                return await interaction.followup.send("Wager not found.", ephemeral=True)
            me = str(interaction.user.id)
            if me not in (str(w.get("creator_id")), str(w.get("opponent_id") or "")):
                return await interaction.followup.send(
                    "Only the duelists can forfeit.", ephemeral=True)
            if str(w.get("status")) != "active":
                return await interaction.followup.send(
                    "Only a live duel can be forfeited.", ephemeral=True)
            winner = (str(w.get("opponent_id")) if me == str(w.get("creator_id"))
                      else str(w.get("creator_id")))
            await _settle(w, winner, interaction.client, how="forfeit")
            try:
                _pot = int(w.get("pot", 0) or 0)
                await _dm(interaction.client, winner,
                          f"<@{me}> forfeited wager `{short_wager_id(w.get('id'))}` — you take "
                          + (f"**{_pot:,}** Nectar Coins." if _pot > 0 else "the win."))
            except Exception as e:
                print(f"[duels] wager-forfeit dm failed: {e}")
            await interaction.followup.send("Forfeited — opponent takes the pot.",
                                            ephemeral=True)
        except Exception as e:
            print(f"[duels] wager-forfeit failed: {e}")
            try:
                await interaction.followup.send("Something went wrong — try again.",
                                                ephemeral=True)
            except Exception:
                pass

    @wager_group.command(name="resolve",
                         description="Force-resolve a duel (admin, dispute fallback)")
    @app_commands.describe(wager_id="Wager id (first 8 chars are enough)",
                           winner="Winner of the pot")
    @app_commands.default_permissions(administrator=True)
    async def wager_resolve_cmd(interaction: discord.Interaction, wager_id: str,
                                winner: discord.Member):
        try:
            if not interaction.user.guild_permissions.administrator:
                return await interaction.response.send_message(
                    "Administrator only.", ephemeral=True)
        except Exception:
            return await interaction.response.send_message(
                "Administrator only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            w = await get_wager(wager_id)
            if not w:
                return await interaction.followup.send("Wager not found.", ephemeral=True)
            if str(w.get("status")) != "active":
                return await interaction.followup.send(
                    "Only a live duel can be force-resolved.", ephemeral=True)
            if str(winner.id) not in (str(w.get("creator_id")),
                                      str(w.get("opponent_id") or "")):
                return await interaction.followup.send(
                    "Winner must be one of the duelists.", ephemeral=True)
            await _settle(w, str(winner.id), interaction.client, how="admin decision")
            await interaction.followup.send(
                f"Resolved — <@{winner.id}> takes the pot.", ephemeral=True)
        except Exception as e:
            print(f"[duels] wager-resolve failed: {e}")
            try:
                await interaction.followup.send("Something went wrong — try again.",
                                                ephemeral=True)
            except Exception:
                pass

    @wager_group.command(name="ban",
                         description="Ban a user from wagers (server owner)")
    @app_commands.describe(user="User to ban from wagers",
                           reason="Why they are banned (shown to them)")
    async def wager_ban_cmd(interaction: discord.Interaction,
                            user: discord.User,
                            reason: app_commands.Range[str, 1, 300] = "No reason"):
        if not _is_guild_owner(interaction):
            return await interaction.response.send_message(
                "Server owner only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        uid = str(user.id)
        try:
            ok, err = await wager_ban(uid, reason, banned_by=str(interaction.user.id))
        except Exception as e:
            print(f"[duels] /wager ban failed: {e}")
            ok, err = False, "Something went wrong — try again."
        if not ok:
            return await interaction.followup.send(err, ephemeral=True)
        try:
            await user.send(f"🚫 You are banned from Nectar Coin wagers: **{reason}**\n"
                            "A server owner can lift this with **/wager unban**.")
        except Exception:
            pass
        await interaction.followup.send(
            f"🚫 {user.mention} can no longer create, join or accept wagers.\n"
            f"Reason: **{reason}**", ephemeral=True)

    @wager_group.command(name="unban",
                         description="Lift a user's wager ban (server owner)")
    @app_commands.describe(user="User to allow wagers again")
    async def wager_unban_cmd(interaction: discord.Interaction, user: discord.User):
        if not _is_guild_owner(interaction):
            return await interaction.response.send_message(
                "Server owner only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        uid = str(user.id)
        was = await wager_ban_reason(uid)
        try:
            ok, err = await wager_unban(uid)
        except Exception as e:
            print(f"[duels] /wager unban failed: {e}")
            ok, err = False, "Something went wrong — try again."
        if not ok:
            return await interaction.followup.send(err, ephemeral=True)
        if not was:
            return await interaction.followup.send(
                f"{user.mention} was not banned from wagers.", ephemeral=True)
        try:
            await user.send("✅ Your Nectar Coin wager ban has been lifted.")
        except Exception:
            pass
        await interaction.followup.send(
            f"✅ {user.mention} can wager again.", ephemeral=True)

    tree.add_command(wager_group)
    print(f"[duels] registered /wager group (9 subcommands, "
          f"{len(tree.get_commands())} top-level on tree)", flush=True)
    print(f"[duels] WAGER_CHANNEL_ID={_wager_channel_id() or '(unset — using command channel)'}",
          flush=True)
