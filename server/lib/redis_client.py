"""
redis_client.py — Upstash Redis wrapper with in-memory fallback.

If UPSTASH env vars are missing, all operations become no-ops
(so local dev works without Redis).
Now also persists Engine SessionView for Vercel serverless (any lambda can read).
"""

import os
import time
import json as _json
from dotenv import load_dotenv

load_dotenv()

# Try to import redis-py, but allow missing for local dev
try:
    import redis as redis_py
except ImportError:
    redis_py = None

# Global in-memory fallbacks
_memory_blacklist: dict[str, float] = {}  # token -> expiry timestamp
_memory_gm: dict[str, tuple[str, float]] = {}  # session_id -> (gm, expiry)
_memory_rate: dict[str, tuple[int, float]] = {}  # key -> (count, window_start)
_memory_state: dict[str, tuple[str, float]] = {}  # session_id -> (json, expiry) for Engine state


def _upstash_cfg():
    url = (os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("REDIS_URL") or "").rstrip("/")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN") or ""
    if url and token and url.startswith("http"):
        return url, token
    return None, None


# --- Bandwidth guards: keep-alive pool + write throttling ---
# Upstash REST over public internet counts as Render Service-Initiated
# egress (TLS + payload each call). Two savings:
# 1. Reuse one pooled HTTP client (no fresh TLS handshake per batch).
# 2. Skip redundant SETs: same payload or <PERSIST_MIN_INTERVAL_S apart.
_http_client = None
_last_set: dict[str, tuple[str, float]] = {}  # key -> (payload_hash, ts)
PERSIST_MIN_INTERVAL_S = 10.0
RAW_MIN_INTERVAL_S = 30.0


def _get_http():
    """Pooled sync HTTP client (keep-alive). Falls back to None -> urllib."""
    global _http_client
    try:
        if _http_client is not None:
            return _http_client
        import httpx as _httpx
        _http_client = _httpx.Client(
            limits=_httpx.Limits(max_connections=20, max_keepalive_connections=20),
            timeout=_httpx.Timeout(5.0, connect=3.0),
        )
        return _http_client
    except Exception:
        return None


def _payload_hash(s: str) -> str:
    try:
        import hashlib as _hl
        return _hl.md5(s.encode()[:65536]).hexdigest() + f":{len(s)}"
    except Exception:
        return str(len(s))

def _get_redis():
    """Get Redis client if configured, else None."""

    url = os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("REDIS_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")

    # For local dev, we use in-memory fallback if no Redis
    if not url or not redis_py:
        return None

    # Upstash uses REST, but redis-py can work with URL
    # For simplicity, we use in-memory if not configured
    return None

async def _upstash_get(key: str) -> str | None:
    url, token = _upstash_cfg()
    if not url:
        return None
    # Pooled client first (keep-alive, no fresh TLS per call).
    try:
        client = _get_http()
        if client is not None:
            r = client.get(f"{url}/get/{key}", headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 200:
                return None
            try:
                return r.json().get("result")
            except Exception:
                return None
    except Exception:
        pass
    try:
        import urllib.request
        req = urllib.request.Request(f"{url}/get/{key}", headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=3) as r:
            body = r.read().decode()
            data = _json.loads(body)
            result = data.get("result")
            return result
    except Exception:
        return None

async def _upstash_set(key: str, value: str, ttl: int = 3600) -> bool:
    url, token = _upstash_cfg()
    if not url:
        return False
    # Session payloads are 10-60KB — must go in the POST body, never the URL
    # (GET-style /set/<key>/<value> fails past ~8KB with URI Too Long).
    # Upstash REST accepts a JSON command array: ["SET", key, value, "EX", ttl].
    try:
        body = _json.dumps(["SET", key, value] + (["EX", int(ttl)] if ttl else [])).encode()
        client = _get_http()
        if client is not None:
            r = client.post(url, content=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            if r.status_code != 200:
                return False
            try:
                return r.json().get("result") == "OK"
            except Exception:
                return True
    except Exception:
        pass
    try:
        import urllib.request
        body = _json.dumps(["SET", key, value] + (["EX", int(ttl)] if ttl else [])).encode()
        req = urllib.request.Request(url, data=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            if r.status != 200:
                return False
            try:
                return _json.loads(r.read().decode()).get("result") == "OK"
            except Exception:
                return True
    except Exception:
        return False


async def _upstash_ping(timeout: int = 3) -> bool:
    """Cheap reachability check (GET /ping) — False when unconfigured or unreachable."""
    url, token = _upstash_cfg()
    if not url:
        return False
    try:
        client = _get_http()
        if client is not None:
            r = client.get(f"{url}/ping", headers={"Authorization": f"Bearer {token}"})
            return r.status_code == 200
    except Exception:
        pass
    try:
        import urllib.request
        req = urllib.request.Request(f"{url}/ping", headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def is_cache_configured() -> bool:
    """True when Upstash env vars are present (no network call)."""
    url, token = _upstash_cfg()
    return bool(url and token)


class Cache:
    """Cache helpers — mirrors the old JS `cache` object."""

    @staticmethod
    async def is_token_blacklisted(token: str) -> bool:
        """Check if token is blacklisted (already used)."""

        # Check in-memory
        now = time.time()
        expiry = _memory_blacklist.get(token)

        if expiry is not None:
            if now < expiry:
                return True
            else:
                # Expired, remove
                _memory_blacklist.pop(token, None)
                return False

        # Try Redis if available
        client = _get_redis()
        if client:
            try:
                return await client.exists(f"blacklist:{token}") == 1
            except Exception:
                pass

        return False

    @staticmethod
    async def blacklist_token(token: str, ttl_seconds: int = 60) -> None:
        """Blacklist a token for ttl seconds."""

        _memory_blacklist[token] = time.time() + ttl_seconds

        client = _get_redis()
        if client:
            try:
                await client.set(f"blacklist:{token}", "1", ex=ttl_seconds)
            except Exception:
                pass

    @staticmethod
    async def set_session_gm(session_id: str, gm: str) -> None:
        """Cache game mode for a session (1 hour)."""

        if not session_id or not gm:
            return

        _memory_gm[session_id] = (gm, time.time() + 3600)

        client = _get_redis()
        if client:
            try:
                await client.set(f"session_gm:{session_id}", gm, ex=3600)
            except Exception:
                pass

    @staticmethod
    async def get_session_gm(session_id: str) -> str | None:
        """Get cached game mode."""

        if not session_id:
            return None

        # Check memory first
        entry = _memory_gm.get(session_id)
        if entry:
            gm, expiry = entry
            if time.time() < expiry:
                return gm
            else:
                _memory_gm.pop(session_id, None)

        client = _get_redis()
        if client:
            try:
                value = await client.get(f"session_gm:{session_id}")
                if value:
                    return value.decode() if isinstance(value, bytes) else str(value)
            except Exception:
                pass

        return None

    @staticmethod
    async def rate_limit(key: str, max_requests: int, window_seconds: int) -> bool:
        """Simple in-memory rate limiting. Returns True if allowed."""

        now = time.time()
        entry = _memory_rate.get(key)

        if entry is None:
            _memory_rate[key] = (1, now + window_seconds)
            return True

        count, expiry = entry

        if now > expiry:
            # Window expired, reset
            _memory_rate[key] = (1, now + window_seconds)
            return True

        if count < max_requests:
            _memory_rate[key] = (count + 1, expiry)
            return True

        return False

    # --- Engine SessionView persistence (Vercel) ---
    @staticmethod
    async def set_session_state(session_id: str, state: dict, ttl: int = 3600) -> bool:
        """Persist SessionView.to_dict() to Upstash + memory (survives lambda cold start).

        Returns True when Upstash accepted it (False = memory-only, lost across lambdas).
        Bandwidth guard: always updates memory, but skips the Upstash POST when
        the payload is unchanged or the last persist was <PERSIST_MIN_INTERVAL_S
        ago (capture batches every 2-5s; 10-60KB each). Returns True on skip so
        callers don't log a failure.
        """
        if not session_id or not state:
            return False
        try:
            payload = _json.dumps(state, separators=(",", ":"))
        except Exception:
            return False
        # memory (always fresh — local reads never hit network)
        _memory_state[session_id] = (payload, time.time() + ttl)
        # throttle Upstash egress (disabled under pytest for determinism)
        try:
            import sys as _sys
            if "pytest" not in _sys.modules:
                h = _payload_hash(payload)
                now = time.time()
                last = _last_set.get(f"bnc:state:{session_id}")
                if last and (last[0] == h or now - last[1] < PERSIST_MIN_INTERVAL_S):
                    return True  # skip: unchanged or too soon
            ok = await _upstash_set(f"bnc:state:{session_id}", payload, ttl=ttl)
            if ok:
                _last_set[f"bnc:state:{session_id}"] = (h, now)
            return ok
        except Exception:
            pass
        return await _upstash_set(f"bnc:state:{session_id}", payload, ttl=ttl)

    @staticmethod
    async def get_session_state(session_id: str) -> dict | None:
        """Fetch SessionView dict from memory or Upstash. Returns None if not found/expired."""
        if not session_id:
            return None
        # memory first
        entry = _memory_state.get(session_id)
        if entry:
            payload, exp = entry
            if time.time() < exp:
                try:
                    return _json.loads(payload)
                except Exception:
                    pass
            else:
                _memory_state.pop(session_id, None)
        # upstash
        raw = await _upstash_get(f"bnc:state:{session_id}")
        if raw:
            try:
                data = _json.loads(raw)
                # refresh memory
                _memory_state[session_id] = (raw, time.time() + 3600)
                return data
            except Exception:
                return None
        return None


    @staticmethod
    async def get_raw(key: str) -> str | None:
        """Generic small-value read (memory + Upstash). For gauges/snapshots."""
        if not key:
            return None
        entry = _memory_state.get(key)
        if entry:
            payload, exp = entry
            if time.time() < exp:
                return payload
            _memory_state.pop(key, None)
        return await _upstash_get(key)

    @staticmethod
    async def set_raw(key: str, value: str, ttl: int = 3600) -> bool:
        """Generic small-value write (memory + Upstash). For gauges/snapshots.

        Throttled like session state (precision snapshots on every health
        poll would otherwise POST continuously).
        """
        if not key or value is None:
            return False
        _memory_state[key] = (value, time.time() + ttl)
        try:
            h = _payload_hash(value if isinstance(value, str) else str(value))
            now = time.time()
            last = _last_set.get(key)
            if last and (last[0] == h or now - last[1] < RAW_MIN_INTERVAL_S):
                return True
            ok = await _upstash_set(key, value, ttl=ttl)
            if ok:
                _last_set[key] = (h, now)
            return ok
        except Exception:
            pass
        return await _upstash_set(key, value, ttl=ttl)

    @staticmethod
    def is_configured() -> bool:
        """True when Upstash env vars are present (no network call)."""
        return is_cache_configured()

    @staticmethod
    async def ping(timeout: int = 3) -> bool:
        """True when Upstash is reachable."""
        return await _upstash_ping(timeout=timeout)


# Singleton
cache = Cache()
