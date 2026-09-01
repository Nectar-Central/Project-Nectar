"""
logger.py — Clean, colored, consistent logging.

Every log line is timestamped and tagged so you can filter:
  [SYS] / [OK] / [ERR] / [EVT] / [CAPTURE]

Colors are 24-bit ANSI — works in Windows Terminal and PowerShell.
"""

from datetime import datetime, timezone
import json
import yaml
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
# Ensure UTF-8 for Windows cp1252 consoles (handles →, ≤, etc.)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------------------
# File handlers — JSON + YAML (rotating, 10MB each, 5 backups)
# Vercel's filesystem is read-only except /tmp — fall back gracefully.
# ---------------------------------------------------------------------------

import os as _os

_struct_logger = logging.getLogger("engine.structured")
_struct_logger.setLevel(logging.INFO)
_struct_logger.propagate = False

try:
    # On Vercel use /tmp, otherwise project root /logs
    if _os.getenv("VERCEL"):
        _log_dir = Path("/tmp/logs")
    else:
        _log_dir = Path(__file__).parent.parent.parent / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)

    _json_handler = RotatingFileHandler(
        _log_dir / "engine.jsonl", maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    _json_handler.setFormatter(logging.Formatter("%(message)s"))

    _yaml_handler = RotatingFileHandler(
        _log_dir / "engine.yaml", maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    _yaml_handler.setFormatter(logging.Formatter("%(message)s"))

    _struct_logger.addHandler(_json_handler)
    _struct_logger.addHandler(_yaml_handler)
except (OSError, PermissionError) as _e:
    # Read-only FS (Vercel) or other FS error — log to stdout only
    # Keep _struct_logger with no file handlers; console prints still work.
    pass

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"


def _color(red: int, green: int, blue: int, text: str) -> str:
    """Wrap text in 24-bit foreground color."""

    return f"\x1b[38;2;{red};{green};{blue}m{text}{RESET}"


def _bg(red: int, green: int, blue: int, text: str) -> str:
    """Wrap text in 24-bit background color."""

    return f"\x1b[48;2;{red};{green};{blue}m{text}{RESET}"


# Color palette
COL = {
    "sys": lambda s: _color(124, 58, 237, s),   # violet
    "ok": lambda s: _color(16, 185, 129, s),    # emerald
    "err": lambda s: _color(239, 68, 68, s),    # red
    "warn": lambda s: _color(245, 158, 11, s),  # amber
    "info": lambda s: _color(156, 163, 175, s), # gray
    "dim": lambda s: DIM + s + RESET,
}

# Background tags
TAG = {
    "SYS": _bg(124, 58, 237, _color(255, 255, 255, " SYS ")),
    "OK": _bg(16, 185, 129, _color(0, 32, 24, " OK  ")),
    "ERR": _bg(239, 68, 68, _color(255, 255, 255, " ERR ")),
    "EVT": _bg(109, 40, 217, _color(255, 255, 255, " EVT ")),
    "JOIN": _bg(16, 185, 129, _color(0, 32, 24, " JOIN ")),
    "LEAVE": _bg(245, 158, 11, _color(32, 16, 0, " LEVE ")),
    "KILL": _bg(239, 68, 68, _color(255, 255, 255, " KILL ")),
    "ALV": _bg(59, 130, 246, _color(255, 255, 255, " ALV  ")),
    "ZONE": _bg(168, 85, 247, _color(255, 255, 255, " ZONE ")),
    "STATE": _bg(124, 58, 237, _color(255, 255, 255, " STATE")),
}


def _timestamp() -> str:
    """Return current time as HH:MM:SS.mmm in dim color."""

    now = datetime.now(timezone.utc)
    return DIM + now.strftime("%H:%M:%S.%f")[:-3] + RESET


def _ts_iso() -> str:
    """Return ISO timestamp for JSON/YAML."""
    return datetime.utcnow().isoformat() + "Z"


def log_sys(message: str) -> None:
    """System/infrastructure log (violet)."""

    print(f"{_timestamp()} {TAG['SYS']} {COL['sys'](message)}")


def log_ok(message: str) -> None:
    """Success log (emerald)."""

    print(f"{_timestamp()} {TAG['OK']} {COL['ok'](message)}")


def log_err(message: str) -> None:
    """Error log (red)."""

    print(f"{_timestamp()} {TAG['ERR']} {COL['err'](message)}")


def log_info(message: str) -> None:
    """Info log (gray, no tag)."""

    print(f"{_timestamp()} {TAG['SYS']} {COL['info'](message)}")


def log_human(message: str) -> None:
    """Clean human-readable sentence (no tag, just time + text)."""

    if not message:
        return
    print(f"{_timestamp()} {TAG['OK']} {message}")


def log_req(ip: str, method: str, url: str, status: int, duration_ms: int) -> None:
    """HTTP request log with color based on status."""

    if status >= 500:
        color = COL["err"]
        tag = TAG["ERR"]
    elif status >= 400:
        color = COL["warn"]
        tag = TAG["ERR"]
    else:
        color = COL["ok"]
        tag = TAG["OK"]

    print(
        f"{_timestamp()} {tag} {color(f'{method} {url}')} "
        f"{COL['dim'](ip)} → {color(str(status))} {COL['dim'](f'{duration_ms}ms')}"
    )


def banner(port: int) -> None:
    """Print startup banner."""

    art = f"""
  {_color(124, 58, 237, '  ██████╗ ██╗  ██╗ ██████╗ ████████╗ ██████╗ ███╗   ██╗')}   {BOLD}PHOTON SERVER (Python){RESET}  {DIM}v1.0.0{RESET}
  {_color(124, 58, 237, '  ██╔══██╗██║  ██║██╔═══██╗╚══██╔══╝██╔═══██╗████╗  ██║')}   {COL['info']('listening on')} {BOLD}http://127.0.0.1:{port}{RESET}
  {_color(124, 58, 237, '  ██████╔╝███████║██║   ██║   ██║   ██║   ██║██╔██╗ ██║')}   {DIM}Python · FastAPI · Supabase{RESET}
    """

    print(art)
    print(f"{TAG['SYS']} {COL['info']('Ready — waiting for packets…')}")
    print(DIM + "─" * 88 + RESET)


# ---------------------------------------------------------------------------
# Structured logging — JSON / YAML
# ---------------------------------------------------------------------------

def log_engine_json(event: str, session: str = "", **fields) -> None:
    """Emit one-line JSON for log aggregation (console + file)."""
    payload = {"ts": _ts_iso(), "event": event, "session": session, **fields}
    line = json.dumps(payload, separators=(",", ":"))
    print(line)
    _struct_logger.info(line)


def log_engine_yaml(event: str, session: str = "", **fields) -> None:
    """Emit YAML for human reading (console + file)."""
    payload = {"ts": _ts_iso(), "event": event, "session": session, **fields}
    line = yaml.dump(payload, default_flow_style=False).strip()
    print(line)
    _struct_logger.info(line)


# ---------------------------------------------------------------------------
# Pretty console events — color-coded by event type
# ---------------------------------------------------------------------------

_EVENT_TAG = {
    "join": TAG["JOIN"],
    "leave": TAG["LEAVE"],
    "killfeed": TAG["KILL"],
    "alv": TAG["ALV"],
    "zone": TAG["ZONE"],
    "state": TAG["STATE"],
    "name_change": TAG["SYS"],
}

def log_engine(event: str, session: str = "", **fields) -> None:
    """
    Pretty console log with color tag + key=value fields.
    Also emits JSON line for aggregation.
    """
    tag = _EVENT_TAG.get(event, TAG["SYS"])
    # Build compact key=value string
    field_str = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    session_part = f" session={session}" if session else ""
    print(f"{_timestamp()} {tag}{session_part} {field_str}")
    
    # Also emit JSON for file/ELK
    log_engine_json(event, session, **fields)
