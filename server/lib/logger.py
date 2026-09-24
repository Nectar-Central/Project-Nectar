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
from dotenv import load_dotenv

_struct_logger = logging.getLogger("engine.structured")
_struct_logger.setLevel(logging.INFO)
_struct_logger.propagate = False

# ---------------------------------------------------------------------------
# Verbose switch — ALL logs enabled by default.
# Set BNC_QUIET=1 to re-enable the old gated behavior.
# ---------------------------------------------------------------------------

def _verbose() -> bool:
    try:
        if _os.getenv("BNC_QUIET") == "1":
            return False
    except Exception:
        pass
    return True

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
    "COMP": _bg(6, 182, 212, _color(0, 32, 32, " COMP ")),
    "RANKED": _bg(234, 179, 8, _color(32, 24, 0, " RANK ")),
    "OK": _bg(16, 185, 129, _color(0, 32, 24, " OK  ")),
    "ERR": _bg(239, 68, 68, _color(255, 255, 255, " ERR ")),
    "EVT": _bg(109, 40, 217, _color(255, 255, 255, " EVT ")),
    "JOIN": _bg(16, 185, 129, _color(0, 32, 24, " JOIN ")),
    "LEAVE": _bg(245, 158, 11, _color(32, 16, 0, " LEVE ")),
    "KILL": _bg(239, 68, 68, _color(255, 255, 255, " KILL ")),
    "WIN": _bg(234, 179, 8, _color(32, 24, 0, " WIN  ")),
    "DEFEAT": _bg(100, 116, 139, _color(255, 255, 255, " DEFEAT ")),
    "ALV": _bg(59, 130, 246, _color(255, 255, 255, " ALV  ")),
    "ZONE": _bg(168, 85, 247, _color(255, 255, 255, " ZONE ")),
    "STG": _bg(249, 115, 22, _color(255, 255, 255, " STG  ")),
    "MATCH": _bg(79, 70, 229, _color(255, 255, 255, "MATCH")),
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
    """System/infrastructure chatter (violet). ALL logs enabled — always prints."""

    try:
        if not _verbose():
            if _os.getenv("BNC_DEBUG") != "1":
                return
    except Exception:
        pass
    print(f"{_timestamp()} {TAG['SYS']} {COL['sys'](message)}", flush=True)


def log_ok(message: str) -> None:
    """Success log (emerald)."""

    print(f"{_timestamp()} {TAG['OK']} {COL['ok'](message)}", flush=True)


def log_err(message: str) -> None:
    """Error log (red)."""

    print(f"{_timestamp()} {TAG['ERR']} {COL['err'](message)}", flush=True)


def log_info(message: str) -> None:
    """Info log (gray). ALL logs enabled — always prints."""

    try:
        if not _verbose():
            if _os.getenv("BNC_DEBUG") != "1":
                return
    except Exception:
        pass
    print(f"{_timestamp()} {TAG['SYS']} {COL['info'](message)}", flush=True)


def log_human(message: str) -> None:
    """Clean human-readable sentence (no tag, just time + text)."""

    if not message:
        return
    print(f"{_timestamp()} {TAG['OK']} {message}", flush=True)


def log_dbg(message: str) -> None:
    """Per-batch/per-packet chatter: ALL logs enabled — always prints."""
    try:
        if not _verbose():
            if _os.getenv("BNC_DEBUG") != "1":
                return
    except Exception:
        pass
    print(f"{_timestamp()} {TAG['SYS']} {COL['dim'](message)}", flush=True)


# ---------------------------------------------------------------------------
# Ranked file log — logs/ranked.log (rotating, 10MB, 3 backups)
# ---------------------------------------------------------------------------

_ranked_logger = logging.getLogger("ranked.file")
_ranked_logger.setLevel(logging.INFO)
_ranked_logger.propagate = False

try:
    if _os.getenv("VERCEL"):
        _ranked_dir = Path("/tmp/logs")
    else:
        _ranked_dir = Path(__file__).parent.parent.parent / "logs"
    _ranked_dir.mkdir(parents=True, exist_ok=True)
    _ranked_handler = RotatingFileHandler(
        _ranked_dir / "ranked.log", maxBytes=10_000_000, backupCount=3, encoding="utf-8"
    )
    _ranked_handler.setFormatter(logging.Formatter("%(message)s"))
    _ranked_logger.addHandler(_ranked_handler)
except (OSError, PermissionError):
    pass


def log_ranked(message: str) -> None:
    """Ranked stream: append to logs/ranked.log (always) + ALWAYS console."""
    try:
        line = f"{datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]} {message}"
        try:
            _ranked_logger.info(line)
        except Exception:
            pass
        try:
            if _verbose() or _os.getenv("BNC_RANKED_LOG") == "1":
                print(f"{_timestamp()} {TAG['RANKED']} {message}", flush=True)
            else:
                print(f"{_timestamp()} {TAG['RANKED']} {message}", flush=True)
        except Exception:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Comp file log — logs/comp.log (rotating, 10MB, 3 backups)
# ---------------------------------------------------------------------------

_comp_logger = logging.getLogger("comp.file")
_comp_logger.setLevel(logging.INFO)
_comp_logger.propagate = False

try:
    if _os.getenv("VERCEL"):
        _comp_dir = Path("/tmp/logs")
    else:
        _comp_dir = Path(__file__).parent.parent.parent / "logs"
    _comp_dir.mkdir(parents=True, exist_ok=True)
    _comp_handler = RotatingFileHandler(
        _comp_dir / "comp.log", maxBytes=10_000_000, backupCount=3, encoding="utf-8"
    )
    _comp_handler.setFormatter(logging.Formatter("%(message)s"))
    _comp_logger.addHandler(_comp_handler)
except (OSError, PermissionError):
    pass


def log_comp(message: str) -> None:
    """Comp stream: append to logs/comp.log (always) + ALWAYS console."""
    try:
        line = f"{datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]} {message}"
        try:
            _comp_logger.info(line)
        except Exception:
            pass
        try:
            print(f"{_timestamp()} {TAG['COMP']} {message}", flush=True)
        except Exception:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Match file log — logs/match.log (rotating, 10MB, 3 backups)
# MATCH START / MATCH END / WIN / DEFEAT — the four lines every duel, wager
# and ranked seal is diagnosed from. Same shape as the RANK stream.
# ---------------------------------------------------------------------------

_match_logger = logging.getLogger("match.file")
_match_logger.setLevel(logging.INFO)
_match_logger.propagate = False

try:
    if _os.getenv("VERCEL"):
        _match_dir = Path("/tmp/logs")
    else:
        _match_dir = Path(__file__).parent.parent.parent / "logs"
    _match_dir.mkdir(parents=True, exist_ok=True)
    _match_handler = RotatingFileHandler(
        _match_dir / "match.log", maxBytes=10_000_000, backupCount=3, encoding="utf-8"
    )
    _match_handler.setFormatter(logging.Formatter("%(message)s"))
    _match_logger.addHandler(_match_handler)
except (OSError, PermissionError):
    pass


def log_match(verb: str, event: str = "", session: str = "", tag: str = "MATCH",
              **fields) -> None:
    """One match lifecycle / outcome line: logs/match.log + console + JSON.

    `verb` is the short word shown first (START / END / new_version / WIN /
    DEFEAT) and doubles as the JSON `action`; the keyword fields are rendered
    as key=value. `tag` picks the console color block. Never raises — a
    logging failure must not break a match.
    """
    try:
        field_str = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
        line = f"[match] {verb} {field_str}".strip()
        try:
            _match_logger.info(
                f"{datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]} {line}")
        except Exception:
            pass
        try:
            session_part = f" session={session}" if session else ""
            print(f"{_timestamp()} {TAG.get(tag, TAG['MATCH'])}{session_part} {line}",
                  flush=True)
        except Exception:
            pass
        if event:
            try:
                payload = {"ts": _ts_iso(), "event": event, "session": session,
                           "action": verb, **fields}
                _struct_logger.info(json.dumps(payload, separators=(",", ":")))
            except Exception:
                pass
    except Exception:
        pass


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
        f"{COL['dim'](ip)} → {color(str(status))} {COL['dim'](f'{duration_ms}ms')}",
        flush=True,
    )


def banner(port: int) -> None:
    """Print startup banner."""

    art = f"""
  {_color(124, 58, 237, '  ██████╗ ██╗  ██╗ ██████╗ ████████╗ ██████╗ ███╗   ██╗')}   {BOLD}PHOTON SERVER (Python){RESET}  {DIM}v1.0.0{RESET}
  {_color(124, 58, 237, '  ██╔══██╗██║  ██║██╔═══██╗╚══██╔══╝██╔═══██╗████╗  ██║')}   {COL['info']('listening on')} {BOLD}http://127.0.0.1:{port}{RESET}
  {_color(124, 58, 237, '  ██████╔╝███████║██║   ██║   ██║   ██║   ██║██╔██╗ ██║')}   {DIM}Python · FastAPI · Supabase{RESET}
    """

    print(art, flush=True)
    print(f"{TAG['SYS']} {COL['info']('Ready — waiting for packets…')}", flush=True)
    print(DIM + "─" * 88 + RESET, flush=True)
    # The port above is the PORT env var, NOT the bound socket: uvicorn's
    # --port flag wins. If the bot logs "Cannot connect", relaunch with
    # --port <same> or point the bot's SERVER_URL at the real port.
    print(f"{TAG['SYS']} {DIM}socket = uvicorn --port (must match PORT above; bot calls SERVER_URL){RESET}",
          flush=True)


# ---------------------------------------------------------------------------
# Structured logging — JSON / YAML
# ---------------------------------------------------------------------------

def log_engine_json(event: str, session: str = "", **fields) -> None:
    """Emit one-line JSON for log aggregation (console + file)."""
    payload = {"ts": _ts_iso(), "event": event, "session": session, **fields}
    line = json.dumps(payload, separators=(",", ":"))
    print(line, flush=True)
    _struct_logger.info(line)


def log_engine_yaml(event: str, session: str = "", **fields) -> None:
    """Emit YAML for human reading (console + file)."""
    payload = {"ts": _ts_iso(), "event": event, "session": session, **fields}
    line = yaml.dump(payload, default_flow_style=False).strip()
    print(line, flush=True)
    _struct_logger.info(line)


# ---------------------------------------------------------------------------
# Pretty console events — color-coded by event type
# ---------------------------------------------------------------------------

_EVENT_TAG = {
    "join": TAG["JOIN"],
    "leave": TAG["LEAVE"],
    "killfeed": TAG["KILL"],
    "win": TAG["WIN"],
    "defeat": TAG["DEFEAT"],
    "alv": TAG["ALV"],
    "zone": TAG["ZONE"],
    "stg": TAG["STG"],
    "match": TAG["MATCH"],
    "state": TAG["STATE"],
    "name_change": TAG["SYS"],
    "proc": TAG["SYS"],
    "warn": TAG["SYS"],
}

# Per-packet detail events ALSO reach the console (ALL logs enabled).
# Set BNC_QUIET=1 to hide them again behind log_dbg.
_ENGINE_DEBUG_EVENTS = {"actor", "debug", "props", "zone", "team_pt", "unknown"}


def log_engine(event: str, session: str = "", **fields) -> None:
    """
    Pretty console log with color tag + key=value fields.
    JSON aggregation goes to logs/engine.jsonl only (never the console).
    ALL events print — including actor/debug/props/zone/team_pt/unknown.
    """
    if event in _ENGINE_DEBUG_EVENTS:
        try:
            field_str = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
            session_part = f" session={session}" if session else ""
            if _verbose():
                tag = _EVENT_TAG.get(event, TAG["SYS"])
                print(f"{_timestamp()} {tag}{session_part} {field_str}", flush=True)
            else:
                log_dbg(f"{event}{session_part} {field_str}")
        except Exception:
            pass
        return
    tag = _EVENT_TAG.get(event, TAG["SYS"])
    # Build compact key=value string
    field_str = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    session_part = f" session={session}" if session else ""
    print(f"{_timestamp()} {tag}{session_part} {field_str}", flush=True)

    # JSON for file aggregation only (no console print)
    try:
        payload = {"ts": _ts_iso(), "event": event, "session": session, **fields}
        _struct_logger.info(json.dumps(payload, separators=(",", ":")))
    except Exception:
        pass
