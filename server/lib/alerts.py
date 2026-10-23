"""
alerts.py -insert into bngc_alerts + bngc_security_logs for bot DMs.

Country is read from Vercel/CF geo headers (x-vercel-ip-country, cf-ipcountry,
x-country). Falls back to ip only if header missing.
"""
from __future__ import annotations
from typing import Any

def _country_from_request(request) -> str | None:
    if not request:
        return None
    h = request.headers
    for k in ("x-vercel-ip-country", "cf-ipcountry", "x-country", "x-geo-country"):
        v = h.get(k)
        if v:
            return str(v).strip().upper()[:2]
    return None

def _ip_from_request(request) -> str | None:
    if not request:
        return None
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if ip:
        return ip
    try:
        return request.client.host if request.client else None
    except Exception:
        return None

def create_alert(
    discord_id: str,
    alert_type: str,
    request=None,
    *,
    game_username: str | None = None,
    game_id: str | None = None,
    discord_username: str | None = None,
    details: dict[str, Any] | None = None,
    severity: str = "info",
) -> None:
    if not discord_id or not alert_type:
        return
    try:
        from .supabase_client import supabase
        if not supabase:
            return
        ip = _ip_from_request(request)
        country = _country_from_request(request)
        payload = {
            "discord_id": str(discord_id),
            "alert_type": str(alert_type).lower(),
            "country": country,
            "ip_address": ip,
            "game_username": game_username,
            "game_id": game_id,
            "discord_username": discord_username,
            "details": details or {},
            "severity": severity,
            "is_read": False,
            "notified": False,
        }
        supabase.table("bngc_alerts").insert(payload).execute()
        # mirror to security logs for audit
        try:
            supabase.table("bngc_security_logs").insert({
                "discord_id": str(discord_id),
                "event_type": f"alert:{alert_type}",
                "ip_address": ip,
                "details": {"country": country, "game_username": game_username, "game_id": game_id, **(details or {})},
                "severity": severity,
            }).execute()
        except Exception:
            pass
    except Exception:
        pass
