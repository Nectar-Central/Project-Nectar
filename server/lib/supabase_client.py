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

supabase: Client | None = None

if _supabase_url and _supabase_key:
    try:
        supabase = create_client(_supabase_url, _supabase_key)
    except Exception as exc:
        # Never crash the container on a bad key (e.g. new-format sb_secret_
        # key with pinned supabase==2.7.0, which only accepts legacy JWTs).
        # Routes degrade to 500 "Server not configured" instead of exit.
        try:
            print(f"[supabase] create_client failed ({exc}); running unconfigured")
        except Exception:
            pass
        supabase = None
else:
    supabase = None  # Will be None in tests without env


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
        """Get session by ID (sync)."""

        if not supabase:
            return {"data": None, "error": "no supabase"}

        try:
            result = (
                supabase.table("bngc_sessions")
                .select("*, bngc_users!inner(is_locked)")
                .eq("session_id", session_id)
                .eq("is_active", True)
                .single()
                .execute()
            )
            data = result.data
            if data and "bngc_users" in data:
                data["is_locked"] = data["bngc_users"].get("is_locked", False)
            return {"data": data, "error": None}
        except Exception as exc:
            if "PGRST116" in str(exc):
                return {"data": None, "error": None}
            return {"data": None, "error": str(exc)}

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
    """
    return await asyncio.to_thread(fn, *args, **kwargs)
