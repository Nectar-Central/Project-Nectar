"""Shared formatting helpers -no Discord imports, pure functions."""

import re
from datetime import datetime

# ---------------------------------------------------------------------------
# Game modes -human readable labels
# ---------------------------------------------------------------------------
GAME_MODE_NAMES = {
    "zw_lg": "Late Game Zonewars",
    "zw": "Zone Wars",
    "wwzw": "Duo Wild West",
    "ww": "Solo Wild West",
    "WW": "Solo Wild West",
    "bf": "Boxfights",
    "BF": "Boxfights",
    "bf2v2": "Boxfights 2v2",
    "bf_2v2": "Boxfights 2v2",
    "BF2V2": "Boxfights 2v2",
    "va": "V-Arena",
    "VA": "V-Arena",
    "tdm_bvr": "Red Vs Blue",
    "RVB": "Red Vs Blue",
    "r_1v1": "Realistic 1v1",
    "R_1V1": "Realistic 1v1",
    "df": "Free Mode",
    "DF": "Free Mode",
    "lobby": "Lobby",
    "lobby_main": "Lobby",
    "mainlobby": "Lobby",
    "main_lobby": "Lobby",
}

def format_game_mode(raw: str | None, duo: bool = False) -> str:
    if not raw:
        return "Unknown"
    key = str(raw).strip()
    if key in GAME_MODE_NAMES:
        label = GAME_MODE_NAMES[key]
    elif key.lower() in GAME_MODE_NAMES:
        label = GAME_MODE_NAMES[key.lower()]
    else:
        # fallback: prettify raw (e.g. zw_warmup -> Zw Warmup)
        return key.replace("_", " ").replace("-", " ").title()
    # (Duo) qualification for solo/duo variants (DF, ZW_LG)
    if duo and key.lower() in ("df", "zw_lg") and not label.lower().startswith(("solo", "duo", "(duo)")):
        return f"(Duo) {label}"
    return label

def format_game_mode_short(raw: str | None) -> str:
    """Short label for compact displays."""
    return format_game_mode(raw)

# ---------------------------------------------------------------------------
# Skin names -cct_88_Inspector_Female_01 => Inspector Female
# ---------------------------------------------------------------------------
_CCT_PREFIX = re.compile(r"^cct_\d+_", re.IGNORECASE)
_CCT_PREFIX_SIMPLE = re.compile(r"^cct_", re.IGNORECASE)

def format_skin_name(skin_id: str | None) -> str:
    if not skin_id:
        return "Unknown"
    s = str(skin_id).strip()
    # Strip cct_XX_ prefix
    s = _CCT_PREFIX.sub("", s)
    s = _CCT_PREFIX_SIMPLE.sub("", s)
    # Split on _ and -, drop purely numeric tokens and empty
    parts = re.split(r"[_\-]+", s)
    kept = []
    for p in parts:
        if not p:
            continue
        if p.isdigit():
            continue
        # Drop tokens that are numeric with leading zeros like 01
        if re.fullmatch(r"\d+", p):
            continue
        kept.append(p)
    if not kept:
        return s.replace("_", " ").title() or "Unknown"
    # Title-case each part, but preserve known acronyms
    titled = []
    for w in kept:
        # Keep 2-3 letter all-caps like FBI as FBI
        if len(w) <= 3 and w.isupper():
            titled.append(w)
        else:
            titled.append(w.capitalize() if w.islower() or w.isupper() else w.capitalize())
    name = " ".join(titled)
    # Collapse double spaces
    name = re.sub(r"\s+", " ", name).strip()
    return name or "Unknown"

def skin_url(skin_id: str | None) -> str | None:
    if not skin_id:
        return None
    return f"https://buildnow-api.netlify.app/skins/{skin_id}.png"

def skin_display(skin_id: str | None) -> str:
    """Human name with hyperlink to preview, for embed fields."""
    if not skin_id:
        return "—"
    name = format_skin_name(skin_id)
    url = skin_url(skin_id)
    return f"[{name}]({url})"

# ---------------------------------------------------------------------------
# Token -censor middle
# ---------------------------------------------------------------------------
def censor_token(token: str | None) -> str:
    if not token:
        return "—"
    t = str(token).strip()
    if len(t) <= 8:
        return f"`{t[:2]}••••{t[-2:]}`"
    return f"`{t[:4]}••••{t[-4:]}`"

def censor_token_plain(token: str | None) -> str:
    """Without backticks, for building embed values that already wrap."""
    if not token:
        return "—"
    t = str(token).strip()
    if len(t) <= 8:
        return f"{t[:2]}••••{t[-2:]}"
    return f"{t[:4]}••••{t[-4:]}"

# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------
def _parse_iso(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except Exception:
        return None

def fmt_time(iso_str: str | None) -> str:
    """HH:MM:SS from ISO string, fallback to slicing."""
    dt = _parse_iso(iso_str)
    if dt:
        return dt.strftime("%H:%M:%S")
    if iso_str and len(iso_str) >= 19:
        return iso_str[11:19]
    return "??:??:??"

def fmt_date(iso_str: str | None) -> str:
    """Mon DD HH:MM from ISO string."""
    dt = _parse_iso(iso_str)
    if dt:
        return dt.strftime("%b %d %H:%M")
    if iso_str and len(iso_str) >= 16:
        return iso_str[:16].replace("T", " ")
    return iso_str or "?"

def fmt_date_short(iso_str: str | None) -> str:
    dt = _parse_iso(iso_str)
    if dt:
        return dt.strftime("%b %d")
    return fmt_date(iso_str)

# ---------------------------------------------------------------------------
# Playtime
# ---------------------------------------------------------------------------
def format_playtime(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"

def format_playtime_per_mode(playtime_per_gm: dict | None) -> str:
    if not playtime_per_gm:
        return "—"
    lines = []
    for gm, secs in playtime_per_gm.items():
        label = format_game_mode(gm)
        lines.append(f"{label}: {format_playtime(int(secs))}")
    return "\n".join(lines) if lines else "—"

# ---------------------------------------------------------------------------
# Misc display helpers
# ---------------------------------------------------------------------------
def format_zone(zone: dict | None) -> str:
    if not zone:
        return "—"
    phase = zone.get("phase")
    t = zone.get("time")
    if phase is None and t is None:
        return "—"
    if phase is not None and t is not None:
        return f"Phase {phase} · {t}s"
    if phase is not None:
        return f"Phase {phase}"
    return f"{t}s"

def truncate(s: str, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"
