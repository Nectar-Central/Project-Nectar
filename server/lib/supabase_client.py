"""
supabase_client.py — Supabase helpers with clean error handling.

All functions return {"data": ..., "error": ...} dicts to keep
call sites explicit. Uses service_role key (server-side only).
"""

import os
import asyncio
from supabase import create_client, Client
from dotenv import load_dotenv
# ---------------------------------------------------------------------------
# Client (singleton)
# ---------------------------------------------------------------------------

load_dotenv()

_supabase_url = os.getenv("SUPABASE_URL", "")
_supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

# Allow loading from app/.env as fallback (for local dev)
if not _supabase_url:
    # Try to load from app/.env if present
    try:
        from pathlib import Path
        from dotenv import load_dotenv

        # Try app/.env
        app_env = Path(__file__).parent.parent.parent / "app" / ".env"
        if app_env.exists():
            load_dotenv(app_env)
            _supabase_url = os.getenv("SUPABASE_URL", "")
            _supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

        # Also try server/.env
        srv_env = Path(__file__).parent.parent / ".env"
        if srv_env.exists():
            load_dotenv(srv_env)
            _supabase_url = os.getenv("SUPABASE_URL", _supabase_url)
            _supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", _supabase_key)
    except Exception:
        pass

class _SupabaseProxy:
    """Forwarding proxy for the singleton client.

    ~10 modules do ``from ... import supabase`` at import time. When
    refresh_client() used to swap the module global for a new object, every
    one of those holders kept pointing at the OLD (closed) httpx session and
    failed permanently with "client has been closed" until the next restart
    (stats 500s + processor get-user failures across all sessions).

    The proxy is bound once at import; refresh only repoints its target, so
    every holder always reaches the current client. Falsy while unconfigured
    (no env), exactly like the previous ``None``.
    """

    def __init__(self):
        object.__setattr__(self, "_target", None)

    def _set(self, client):
        object.__setattr__(self, "_target", client)

    def _get(self):
        return object.__getattribute__(self, "_target")

    def __bool__(self):
        try:
            return object.__getattribute__(self, "_target") is not None
        except Exception:
            return False

    __nonzero__ = __bool__

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        target = object.__getattribute__(self, "_target")
        if target is None:
            raise AttributeError(f"supabase not configured (attr {name})")
        return getattr(target, name)


supabase = _SupabaseProxy()

# Transient pool/disconnect signatures — safe to retry once with a fresh client.
_TRANSIENT_MARKERS = (
    "Server disconnected",
    "connection closed",
    "connection reset",
    "has been closed",
    "is closed",
    "closed client",
    "pool",
    "timeout",
    "temporarily unavailable",
    "ConnectionTerminated",
    "RemoteProtocolError",
)


def _is_transient(exc: Exception | str) -> bool:
    try:
        s = str(exc or "")
    except Exception:
        return False
    sl = s.lower()
    return any(m.lower() in sl for m in _TRANSIENT_MARKERS)


def _build_client():
    """Build a Supabase client with explicit httpx pooling/timeout.

    The default shared httpx pool exhausts under capture burst
    (stats poll every ~2s + capture POSTs) and surfaces as
    "Server disconnected". Explicit limits + timeouts keep one
    slow BAR far from poisoning the pool.
    """
    try:
        import httpx as _httpx
        from supabase.lib.client_options import SyncClientOptions

        _http = _httpx.Client(
            limits=_httpx.Limits(max_connections=50, max_keepalive_connections=20),
            timeout=_httpx.Timeout(10.0, connect=5.0),
        )
        return create_client(
            _supabase_url,
            _supabase_key,
            options=SyncClientOptions(
                httpx_client=_http, postgrest_client_timeout=10,
            ),
        )
    except Exception:
        pass
    return create_client(_supabase_url, _supabase_key)


def _close_session(client) -> None:
    """Best-effort httpx session close. Never raises."""
    try:
        if client is None:
            return
        sess = getattr(getattr(client, "postgrest", None), "session", None)
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass
    except Exception:
        pass


def refresh_client():
    """Rebuild the singleton after a pool disconnect. Repoints the proxy
    target FIRST, then closes the old session — holders never observe a
    closed client. Returns the new client (or the current one on failure)."""
    global supabase
    if not _supabase_url or not _supabase_key:
        return None
    try:
        try:
            old = supabase._get() if isinstance(supabase, _SupabaseProxy) else supabase
        except Exception:
            old = None
        new_client = _build_client()
        try:
            if isinstance(supabase, _SupabaseProxy):
                supabase._set(new_client)
            else:
                supabase = new_client
        except Exception:
            supabase = new_client
        _close_session(old)
        return new_client
    except Exception as exc:
        try:
            print(f"[supabase] refresh failed ({exc})")
        except Exception:
            pass
        try:
            if isinstance(supabase, _SupabaseProxy):
                return supabase._get()
        except Exception:
            pass
        return supabase


if _supabase_url and _supabase_key:
    try:
        supabase._set(_build_client())
    except Exception as exc:
        # Never crash the container on a bad key (e.g. new-format sb_secret_
        # key with pinned supabase==2.7.0, which only accepts legacy JWTs).
        # Routes degrade to 500 "Server not configured" instead of exit.
        try:
            print(f"[supabase] create_client failed ({exc}); running unconfigured")
        except Exception:
            pass
        # proxy target stays None -> `if not supabase` guards keep working
else:
    pass  # proxy target stays None in tests without env


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class DB:
    """Database helpers — mirrors the old JS `db` object but with Python style."""

    @staticmethod
    async def get_user(discord_id: str) -> dict:
        """Get user by discord_id."""

        if not supabase:
            return {"data": None, "error": "no supabase"}

        result = supabase.table("bngc_users").select("*").eq("discord_id", discord_id).single().execute()
        # supabase-py returns .data and .error handling via exception; single() raises if not found
        # We normalize: if no row, data is None
        return {"data": getattr(result, "data", None), "error": None}

    @staticmethod
    def get_user_sync(discord_id: str) -> dict:
        """Sync version for non-async contexts."""

        if not supabase:
            return {"data": None, "error": "no supabase"}

        try:
            result = supabase.table("bngc_users").select("*").eq("discord_id", discord_id).single().execute()
            return {"data": result.data, "error": None}
        except Exception as exc:
            # PGRST116 = not found → treat as None
            if "PGRST116" in str(exc):
                return {"data": None, "error": None}
            return {"data": None, "error": str(exc)}

    @staticmethod
    def get_session_sync(session_id: str) -> dict:
        """Get session by ID (sync).

        Primary path joins bngc_users for is_locked; on join/column
        failure falls back to a plain session lookup so a transient
        schema/pool blip never wipes discord_id downstream.
        """

        if not supabase:
            return {"data": None, "error": "no supabase"}

        def _query(client, with_join: bool):
            q = client.table("bngc_sessions")
            sel = "*, bngc_users!inner(is_locked)" if with_join else "*"
            return (
                q.select(sel)
                .eq("session_id", session_id)
                .eq("is_active", True)
                .single()
                .execute()
            )

        try:
            result = _query(supabase, True)
            data = result.data
            if data and "bngc_users" in data:
                try:
                    data["is_locked"] = data["bngc_users"].get("is_locked", False)
                except Exception:
                    pass
            return {"data": data, "error": None}
        except Exception as exc:
            if "PGRST116" in str(exc):
                return {"data": None, "error": None}
            # Join failed (or pool blip) — retry without the join once.
            try:
                result = _query(supabase, False)
                return {"data": result.data, "error": None}
            except Exception as exc2:
                if "PGRST116" in str(exc2):
                    return {"data": None, "error": None}
                return {"data": None, "error": str(exc2)}

    @staticmethod
    def update_session_sync(session_id: str, updates: dict) -> dict:
        """Update session (sync) — tries with last_used_at, falls back without."""

        if not supabase:
            return {"data": None, "error": "no supabase"}

        # Try with last_used_at first
        try:
            from datetime import datetime, timezone

            payload = {**updates, "last_used_at": datetime.now(timezone.utc).isoformat()}
            result = supabase.table("bngc_sessions").update(payload).eq("session_id", session_id).execute()
            data = result.data[0] if isinstance(result.data, list) and result.data else result.data
            return {"data": data, "error": None}
        except Exception as exc:
            # If column missing, retry without it
            if "last_used_at" in str(exc):
                try:
                    result = supabase.table("bngc_sessions").update(updates).eq("session_id", session_id).execute()
                    data = result.data[0] if isinstance(result.data, list) and result.data else result.data
                    return {"data": data, "error": None}
                except Exception as exc2:
                    return {"data": None, "error": str(exc2)}
            return {"data": None, "error": str(exc)}

    @staticmethod
    def lock_user_sync(discord_id: str, reason: str, ip: str | None, country: str | None) -> dict:
        """Lock a user account."""

        if not supabase:
            return {"data": None, "error": "no supabase"}

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()

        try:
            result = (
                supabase.table("bngc_users")
                .update({
                    "is_locked": True,
                    "lock_reason": reason,
                    "locked_at": now,
                    "locked_ip": ip,
                    "locked_country": country,
                })
                .eq("discord_id", discord_id)
                .execute()
            )
            # supabase-py returns list, take first
            if isinstance(result.data, list) and result.data:
                result.data = result.data[0]

            # Also log to security logs (best effort)
            try:
                supabase.table("bngc_security_logs").insert({
                    "discord_id": discord_id,
                    "event_type": "account_locked",
                    "ip_address": ip,
                    "country": country,
                    "details": {"reason": reason},
                    "severity": "critical",
                }).execute()
            except Exception:
                pass

            return {"data": result.data, "error": None}

        except Exception as exc:
            return {"data": None, "error": str(exc)}


# Singleton instance
db = DB()


async def db_io(fn, *args, **kwargs):
    """Run a blocking sync Supabase call in a worker thread.

    The shared client is sync: every .execute() blocks the event loop for
    its full RTT, serializing ALL concurrent requests on one worker. Awaiting
    through here keeps ordering (still sequential per request) while letting
    other requests use the loop. httpx pools are thread-safe for this shape.

    On a transient pool disconnect ("Server disconnected") the client is
    refreshed and the call retried exactly once.
    """
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:
        if not _is_transient(exc):
            raise
        try:
            refresh_client()
        except Exception:
            pass
        await asyncio.sleep(0.25)
        return await asyncio.to_thread(fn, *args, **kwargs)


def is_transient_error(exc: Exception | str) -> bool:
    """Public helper for routes: True when the error is a pool blip worth degrading (not 500)."""
    return _is_transient(exc)
