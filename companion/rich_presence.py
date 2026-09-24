#!/usr/bin/env python3
"""Nectar -> Discord rich presence bridge (pypresence edition).

The userscript (companion/buildnow-companion.user.js, F4 > Presence tab) knows
things only the page can know - the live scene, the ranked session, the tourney
name and the private-party pID - and POSTs them here as plain facts. This
process formats them and drives the local Discord client through the
`pypresence` package, so Discord shows:

    Playing Buildnow.gg with Nectar
    In Match - Ranked Session
    Late Game Zonewars · 8/12 players · private party

Line one is only WHERE the player is (in a match or in the lobby) plus the
name of the session when it has one, and line two carries the mode, the
occupancy (players in the match over the room's max) and the party state -
never the same fact on both lines. A party is a thing you are in, not a place
you are at, so it never becomes the headline.

Two buttons ride along (Discord's limit): "Join Nectar" with the community
invite, and - for any party that carries a shareable code - "Join my party",
which opens
https://www.crazygames.com/game/buildnow-gg?czy_invite=true&pID=<pID>. The
userscript fills that code from the room the player is standing in, or from the
session's own party/short code for a party formed in the lobby (where the party
has no room code of its own) - so a lobby party offers the invite too instead of
saying "in a party" next to no way in.

A lobby that has not picked anything yet reads Idle: the game boots on df (Free
Mode) and keeps reporting it until a real mode is adopted, and "Free Mode" over
the lobby was a lie about a player who was just standing there.

Ladder state never reaches the card: rank and points are the player's own
business and a Discord activity is visible to the whole friends list, so the
second line carries mode, tourney field size and party state only. A stale
userscript still pushing rank/points has them dropped here.

HTTP API (JSON, permissive CORS - the userscript posts from the game page):
    GET  /health    -> {ok, service, version, port, discord, activity}
    POST /presence  -> facts from the userscript; applied immediately
    GET  /presence  -> last facts + last activity (debugging / the F4 preview)
    POST /clear     -> clears the activity

Dependency: `pip install pypresence` drives the Discord client (Windows named
pipe, or $XDG_RUNTIME_DIR/discord-ipc-N elsewhere). It is imported on first
use, so a machine without it still starts a bridge that answers /health and
reports exactly what is missing to the F4 panel.

WHY pypresence INSTEAD OF discordrpc:
    - `pypresence` is the actively maintained, far more widely used library
      for Discord IPC in Python (qwertyquerty/pypresence). `discordrpc` is a
      smaller, less-maintained package with a different call shape.
    - `pypresence.Presence` requires an explicit `.connect()` before any
      `.update()`/`.clear()` call - there is no implicit lazy dial - and it
      raises real exceptions (`DiscordNotFound`, `InvalidPipe`,
      `PipeClosed`, etc.) instead of returning booleans, so error handling
      below is exception-based rather than truthy-return-based.
    - `pypresence.Presence.update()` takes keyword arguments directly
      (state, details, large_image, large_text, buttons, start) - there is
      no separate "activity kwargs" builder step needed.

Port: 8765 by default - the same port the decryptor uses for packet capture, so
when the decryptor already owns it the bridge walks forward through 8766..8769
and reports the port it took from /health (the userscript auto-detects). A port
something else already answers on is skipped rather than double-bound (Windows
would otherwise let two processes listen on it and split requests between
them), and a wedged leftover is skipped too.

Bounded by design: pypresence's `connect()` and `update()` calls can block on
the underlying pipe I/O (opening the pipe, waiting for Discord's READY frame),
so every call runs on this process's single worker thread and the caller waits
a budget of its own instead of waiting with it - CALL_TIMEOUT for a push,
DIAL_TIMEOUT for the startup dial that Discord's READY round trip takes its
time with. A wedged Discord therefore costs one slow push that reports
"discord": false honestly, never a stuck F4 panel.

Usage:
    pip install pypresence
    python companion/rich_presence.py
    python companion/rich_presence.py --port 8766 --client-id 1527028139279122512
    python companion/rich_presence.py --no-join --stale 20 --verbose
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.5.0"
SERVICE = "nectar-rich-presence"

# Discord application for the Python bridge. This is a different app from the
# legacy Node script (companion/discord-presence.js, 1529945836199608531),
# so art/assets do not carry over - use --client-id to point at the old app.
DEFAULT_CLIENT_ID = "1527028139279122512"
DEFAULT_PORTS = (8765, 8766, 8767, 8768, 8769)
DEFAULT_HOST = "127.0.0.1"

# The app icon: passed as an external asset so no upload to the Discord app is
# needed (Discord accepts image URLs for rich presence artwork).
ICON_URL = ("https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/"
            "assets/discordlogo.png")
ICON_TEXT = "Buildnow.gg with Nectar"
APP_NAME = "Buildnow.gg with Nectar"
JOIN_URL = "https://www.crazygames.com/game/buildnow-gg?czy_invite=true&pID={pid}"
NECTAR_INVITE = "https://discord.gg/GDSW9p39J5"
MAX_LEN = 128
# The userscript pushes every 15s, so going quiet for 45s means the game really
# stopped talking (closed/crashed) rather than one missed push.
STALE_S = 45.0
# How long a caller waits for a Discord call before giving up on it. A push gets
# CALL_TIMEOUT, because the userscript times its own request out at 4s and a
# wedged Discord must not eat that budget. The warm-up dial is the one slow call
# - a pipe open plus Discord's READY round trip - so it gets DIAL_TIMEOUT and
# runs at startup, off the push path. A push that lands mid-dial says so and
# the next one applies.
CALL_TIMEOUT = 2.5
DIAL_TIMEOUT = 10.0

# Human-readable game modes (mirrors companion/bot/ranked.py GM_NAMES).
GM_NAMES = {
    "zw_lg": "Late Game Zonewars", "zw": "Zone Wars",
    "wwzw": "Duo Wild West", "ww": "Solo Wild West", "wildwest": "Solo Wild West",
    "wild_west": "Solo Wild West", "bf": "Boxfights", "bf2v2": "Boxfights 2v2",
    "va": "V-Arena", "tdm_bvr": "Red Vs Blue", "rvb": "Red Vs Blue",
    "r_1v1": "Realistic 1v1", "df": "Free Mode", "br": "Battle Royale",
    "tdm": "Team Deathmatch", "ffa": "Free For All", "snd": "Search & Destroy",
}

# Mode values that mean "nothing picked yet" rather than a mode being played:
# the game boots on df (Free Mode / MainWorld) and keeps reporting it until a
# real mode is adopted, and a menu/lobby session can carry no mode at all.
# Compared against a normalised key or label (lowercase, -/space -> _).
IDLE_MODES = {"", "df", "lobby", "menu", "mainworld", "main_world", "free_mode"}


def log(msg: str) -> None:
    # DEBUG: every log line is timestamped and flushed immediately so `python
    # rich_presence.py -v` shows live output even when piped/redirected.
    try:
        print(f"{time.strftime('%H:%M:%S')} [presence] {msg}", flush=True)
    except Exception:
        pass


def clamp(s, n: int = MAX_LEN) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def gm_label(key) -> str:
    raw = str(key or "").strip()
    if not raw:
        return ""
    return GM_NAMES.get(raw) or GM_NAMES.get(raw.lower()) or raw.replace("_", " ").replace("-", " ").title()


def _status_phrase(facts: dict) -> str:
    """Line one says WHERE the player is: in a match or in the lobby.

    Those are the only two places there are. A party is not one of them, and
    it used to take over the line: a party in the lobby rendered

        In Party
        party

    which told a friend nothing and hid the one fact that mattered (that the
    player is in the lobby, waiting). The party now rides on line two, next to
    the mode, and a menu/unknown status still reads as the lobby it is.
    """
    status = str((facts or {}).get("status") or "").lower()
    return "In Match" if status == "match" else "In Lobby"


def _session_name(facts: dict) -> str:
    """The named session the player is in, if there is one (tourney/ranked).

    Deliberately the session name and NOT the game mode: the mode belongs to
    line two, so the two lines can never say the same thing twice (the mode
    used to be the line-one fallback and then get repeated on line two).
    """
    ctx = (facts or {}).get("context") or {}
    kind = str(ctx.get("kind") or "").lower()
    name = str(ctx.get("name") or "").strip()
    if name and kind in ("tourney", "ranked", "room"):
        return name
    return ""


def _mode_label(facts: dict) -> str:
    mode = (facts or {}).get("mode") or {}
    return gm_label(mode.get("label") or mode.get("key"))


def _counts(facts: dict):
    """(current, max) for the (X/Y) suffix: who is in this match, room size.

    Both come from the live session, so a room that fills up (or empties out)
    moves the numbers Discord shows.
    """
    players = (facts or {}).get("players") or {}
    try:
        cur = int(players.get("cur")) if players.get("cur") is not None else None
        room = int(players.get("max")) if players.get("max") is not None else None
    except (TypeError, ValueError):
        return None
    if cur is None or room is None or room <= 0 or cur < 0 or cur > max(room, 10000):
        return None
    return cur, room


def build_details(facts: dict) -> str:
    """The primary line: 'In Match - Ranked Session' / 'In Lobby'.

    Place first, session name second - never the mode and never the occupancy:
    both live on line two, so the card reads as two different facts instead of
    the same one twice.
    """
    line = _status_phrase(facts)
    name = _session_name(facts)
    if name:
        line += f" - {name}"
    return clamp(line)


def _is_ladder_extra(text: str) -> bool:
    """True for a rank/points token, whoever sent it.

    Rank and points are deliberately absent from the card, so an older
    userscript that still pushes them ("rank 3/12", "8,488 pts") has them
    dropped here rather than shown to everyone on the user's friends list.
    """
    low = str(text or "").strip().lower()
    return low.startswith("rank ") or "pts" in low or "point" in low


def _is_idle_mode(facts: dict) -> bool:
    """True when the mode is the client's boot default instead of a picked one.

    The game starts on df (Free Mode / MainWorld) and reports it until a real
    mode is adopted, so a lobby card read "Free Mode - in a party" about a
    player who was doing no such thing. No mode at all is the same emptiness.
    """
    mode = (facts or {}).get("mode") or {}
    raw = str(mode.get("key") or mode.get("label") or "").strip().lower()
    return raw.replace("-", "_").replace(" ", "_") in IDLE_MODES


def build_state(facts: dict) -> str:
    """Secondary line: mode, occupancy, tourney field, party - one fact each.

    Only what line one does not already say. Just facts about where the player
    is - never their rank or their points - and the party reads as a party
    ("in a party" / "private party") rather than as a place.

    In the lobby with nothing picked yet - no mode, or the df default the game
    boots on - the mode slot says Idle: the player is waiting, not playing Free
    Mode. A match with no mode still says "In a match".
    """
    facts = facts or {}
    bits: list[str] = []
    mode = _mode_label(facts)
    in_match = _status_phrase(facts) == "In Match"
    if not in_match and _is_idle_mode(facts):
        bits.append("Idle")
    elif mode:
        bits.append(mode)
    counts = _counts(facts)
    if counts:
        bits.append(f"{counts[0]}/{counts[1]} players")
    # The tourney field size is a second way of saying how many are in the
    # room, so it only shows up when the room's own occupancy is unknown.
    extra = str((facts.get("context") or {}).get("extra") or "").strip()
    if extra and not _is_ladder_extra(extra) and not counts:
        bits.append(extra)
    party = facts.get("party") or {}
    if party.get("active"):
        bits.append("private party" if party.get("private") else "in a party")
    if bits:
        return clamp(" \u00b7 ".join(bits))
    # Nothing known yet (bare push with no facts): say something useful
    # instead of repeating the place that is already on line one.
    return "In a match" if in_match else "Idle"


def build_buttons(facts: dict, allow_join: bool = True, allow_invite: bool = True) -> list:
    """Discord shows at most two buttons: the party invite, then Nectar's.

    The party button only exists for a private party carrying a pID; the Nectar
    server invite rides along on every activity.
    """
    buttons: list = []
    if allow_join:
        party = (facts or {}).get("party") or {}
        pid = str(party.get("pid") or "").strip()
        if party.get("active") and party.get("private") and pid:
            if pid.isalnum() and (2 <= len(pid) <= 16):
                buttons.append({"label": "Join my party",
                                "url": JOIN_URL.format(pid=urllib.parse.quote(pid))})
    if allow_invite:
        buttons.append({"label": "Join Nectar", "url": NECTAR_INVITE})
    return buttons[:2]


def _party_link(facts: dict):
    """The clickable party invite for a private party with a usable pID."""
    party = (facts or {}).get("party") or {}
    pid = str(party.get("pid") or "").strip()
    if party.get("active") and party.get("private") and pid:
        if pid.isalnum() and (2 <= len(pid) <= 16):
            return JOIN_URL.format(pid=urllib.parse.quote(pid))
    return None


def build_display(facts: dict, allow_join: bool = True, allow_invite: bool = True,
                  image: str = ICON_URL) -> dict:
    """Pure facts -> what Discord should show. No side effects, no IO."""
    facts = facts or {}
    party_url = _party_link(facts)
    display = {
        "name": APP_NAME,
        "details": build_details(facts),
        "state": build_state(facts),
        "large_image": image or ICON_URL,
        "large_text": ICON_TEXT,
        "buttons": build_buttons(facts, allow_join, allow_invite),
        "status": str(facts.get("status") or "lobby").lower() or "lobby",
        # pypresence's clickable-URL fields: the artwork and both lines open
        # something real instead of doing nothing when a friend clicks them.
        "large_url": NECTAR_INVITE,
        "details_url": party_url or NECTAR_INVITE,
        "state_url": party_url or NECTAR_INVITE,
    }
    try:
        # The elapsed-time clock. pypresence's `start` is in MILLISECONDS
        # (epoch ms), unlike the old discordrpc client's seconds.
        since_ms = float(facts.get("since") or 0)
        now_ms = time.time() * 1000
        if 0 < since_ms <= now_ms + 60_000 and (now_ms - since_ms) < 6 * 3600_000:
            display["start"] = int(since_ms)
    except (TypeError, ValueError):
        pass
    return display


# ---------------------------------------------------------------------------
# Discord (pypresence)
# ---------------------------------------------------------------------------

def _default_factory(client_id: str):
    """Build (but do not connect) a pypresence client.

    Imported here rather than at module scope so a machine without the
    dependency still runs the bridge - /health and the F4 panel then report the
    missing package instead of the process dying at import.

    Connecting is a separate step (`Presence.connect()`), done by the caller,
    because pypresence's constructor itself does not touch the pipe - only
    connect()/update()/clear() do, and those are exactly the calls this module
    wants to run on the bounded worker thread.
    """
    from pypresence import Presence  # pip install pypresence

    return Presence(str(client_id))


def _activity_type():
    """pypresence's ActivityType.PLAYING, or None when the enum is unavailable.

    PLAYING is what renders "Playing <app name>"; it is pypresence's default,
    but it is passed explicitly so the displayed verb never depends on a
    library default. An older pypresence without `.types` returns None and
    the kwarg is dropped rather than guessed.
    """
    try:
        from pypresence.types import ActivityType
        return ActivityType.PLAYING
    except Exception:
        return None


def _pypresence_version() -> str:
    """The pypresence version in play, for the startup banner."""
    try:
        from importlib.metadata import version
        return version("pypresence")
    except Exception:
        return "unknown"


def _display_to_update_kwargs(display: dict) -> dict:
    """Our display dict -> Presence.update() keyword arguments.

    pypresence's `update()` takes these directly as kwargs (no separate
    activity-object wrapping needed, unlike the old discordrpc client), and it
    owns the framing/handshake - nothing here speaks IPC.
    """
    display = display or {}
    kwargs = {
        "activity_type": _activity_type(),
        "name": display.get("name") or APP_NAME,
        "state": display.get("state") or "Waiting for a match",
        "details": display.get("details") or "In Lobby",
        "large_image": display.get("large_image") or ICON_URL,
        "large_text": display.get("large_text") or ICON_TEXT,
    }
    start = display.get("start")
    if start:
        try:
            kwargs["start"] = int(start)     # milliseconds (pypresence)
        except (TypeError, ValueError):
            pass
    # Clickable artwork/text (pypresence state_url/details_url/large_url).
    for key in ("large_url", "details_url", "state_url"):
        url = str(display.get(key) or "").strip()
        if url.startswith("http"):
            kwargs[key] = url
    buttons = display.get("buttons") or []
    if buttons:
        kwargs["buttons"] = [{"label": str(b.get("label") or ""),
                              "url": str(b.get("url") or "")} for b in buttons[:2]]
    # None only ever means "let pypresence choose its default".
    return {k: v for k, v in kwargs.items() if v is not None}


class DiscordPresence:
    """One Discord activity, driven by the `pypresence` package.

    The package owns the pipe, the handshake and the op/length/payload frames,
    so this side is only the mapping from our display dict onto update() plus
    an honest ready/last_error for /health.

    pypresence's `connect()` and `update()` can block on pipe I/O with no
    deadline of their own, and `update()`/`clear()` raise (rather than return
    False) when the pipe is gone (`PipeClosed`) or Discord never answered. So
    every call runs on this object's single worker thread and the caller waits
    a budget instead of waiting with it, which keeps a wedged Discord from
    parking the HTTP handler that asked for the update. One worker for the
    object's life (not one per call) means a stalled client can never turn
    into thread churn - jobs simply queue behind it and time out, and a client
    that stops answering is dropped so the next job connects fresh.
    """

    def __init__(self, client_id: str = DEFAULT_CLIENT_ID, verbose: bool = False,
                 timeout: float = CALL_TIMEOUT, dial_timeout: float = DIAL_TIMEOUT,
                 factory=None, rpc=None, warm: bool = True):
        self.client_id = str(client_id)
        self.verbose = verbose
        self.timeout = max(0.1, float(timeout))
        self.dial_timeout = max(self.timeout, float(dial_timeout))
        self.last_error = ""
        self._connected = False
        self._factory = factory or _default_factory
        self._rpc = rpc
        self._jobs: queue.Queue = queue.Queue(maxsize=16)
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()
        # Connect in the background: the READY round trip belongs to startup,
        # not to the first push someone asked for.
        if warm:
            try:
                self._jobs.put_nowait(("warmup", None, {"ok": False, "event": threading.Event()}))
            except queue.Full:
                pass

    # -- what the API reports ------------------------------------------------
    @property
    def ready(self) -> bool:
        """True only once a client is up *and* Discord has completed the handshake."""
        return bool(self._rpc is not None and self._connected)

    def client(self):
        """The live Presence client, built (not yet connected) on first use."""
        if self._rpc is None:
            try:
                self._rpc = self._factory(self.client_id)
                self._connected = False
            except Exception as exc:
                self.last_error = (f"pypresence unavailable ({exc}) - "
                                   f"run: pip install pypresence")
                log(self.last_error)  # DEBUG: surface exactly why the client couldn't build
                return None
        return self._rpc

    # -- the calls the bridge makes -----------------------------------------
    def warmup(self) -> bool:
        """Connect now so startup can report honestly instead of on first push."""
        return self._submit("warmup", None)

    def set_activity(self, display: dict) -> bool:
        return self._submit("activity", display)

    def clear(self) -> bool:
        return self._submit("clear", None)

    def stop(self) -> None:
        try:
            self._jobs.put_nowait(None)
        except queue.Full:
            pass

    # -- the worker ----------------------------------------------------------
    def _submit(self, kind: str, display) -> bool:
        box = {"ok": False, "event": threading.Event()}
        try:
            self._jobs.put_nowait((kind, display, box))
        except queue.Full:
            self.last_error = "Discord is not answering - dropping this update"
            log(f"queue full, dropped {kind}")  # DEBUG
            return False
        budget = self.dial_timeout if kind == "warmup" else self.timeout
        if box["event"].wait(budget):
            return bool(box["ok"])
        if not self._connected:
            # Mid-connect: the job is still queued, so the client that lands
            # will pick it up - no need to make this caller wait for READY.
            self.last_error = "still connecting to Discord - the next update will apply"
        else:
            self.last_error = f"Discord did not answer within {budget:.1f}s"
            self._rpc = None  # stopped answering: the next job connects fresh
            self._connected = False
        if self.verbose:
            log(self.last_error)
        return False

    def _loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                break
            kind, display, box = job
            try:
                box["ok"] = self._run(kind, display)
            except Exception as exc:
                # DEBUG: pypresence raises typed exceptions (DiscordNotFound,
                # InvalidPipe, PipeClosed, ServerError, ...) - log the class
                # name too since the message alone can be terse.
                box["ok"] = False
                self.last_error = f"pypresence error: {type(exc).__name__}: {exc}"
                self._rpc = None
                self._connected = False
                if self.verbose:
                    log(self.last_error)
            finally:
                box["event"].set()

    def _run(self, kind: str, display) -> bool:
        rpc = self.client()
        if rpc is None:
            return False
        if not self._connected:
            rpc.connect()  # raises DiscordNotFound / InvalidPipe on failure
            self._connected = True
            if self.verbose:
                log("connected to Discord")  # DEBUG
        if kind == "warmup":
            return self.ready
        if kind == "clear":
            rpc.clear()
            return True
        rpc.update(**_display_to_update_kwargs(display))
        self.last_error = ""
        if self.verbose:
            log(f"applied: {display.get('details', '')} | {display.get('state', '')}")  # DEBUG
        return True


# ---------------------------------------------------------------------------
# Bridge: facts in, Discord activity out
# ---------------------------------------------------------------------------

class Bridge:
    """Holds the latest facts, applies them to Discord, clears when stale."""

    def __init__(self, client_id: str = DEFAULT_CLIENT_ID, allow_join: bool = True,
                 stale: float = STALE_S, image: str = ICON_URL, verbose: bool = False,
                 allow_invite: bool = True, presence=None):
        self.allow_join = allow_join
        self.allow_invite = allow_invite
        self.stale = max(3.0, float(stale))
        self.image = image
        self.verbose = verbose
        self.presence = presence if presence is not None else DiscordPresence(client_id=client_id,
                                                                             verbose=verbose)
        self.facts: dict = {}
        self.display = None
        self.last_push = 0.0
        self.last_apply = 0.0
        self.applies = 0
        # 'live' = the userscript's facts are flowing, 'idle' = nothing live
        # (startup, or the game went quiet and the card was cleared),
        # 'cleared' = explicitly turned off by the player.
        self.mode = "idle"
        self._lock = threading.Lock()
        # Serialises the Discord writes only: they can spend seconds on a busy
        # pipe, and status()/tick() must never queue behind them.
        self._apply_lock = threading.Lock()

    def bootstrap(self) -> bool:
        """Warm the Discord dial at startup (called once when we come up).

        Sets no activity: with the game not yet reporting, the honest card is
        none at all - a resting "In Lobby" card is exactly what lingered on
        profiles after the game closed. The first userscript push goes live.
        Runs on a background thread by callers: a missing Discord costs the
        whole dial budget before reporting honestly.
        """
        with self._lock:
            if self.mode == "cleared":
                return False
        warm = getattr(self.presence, "warmup", None)
        try:
            if callable(warm):
                return bool(warm())
            return bool(self.presence.ready)
        except Exception as exc:
            log(f"warmup failed: {exc}")  # DEBUG
            return False

    def push(self, facts: dict) -> dict:
        with self._lock:
            self.facts = facts or {}
            self.last_push = time.time()
            self.display = build_display(self.facts, allow_join=self.allow_join,
                                         allow_invite=self.allow_invite, image=self.image)
            self.mode = "live"
            display = self.display
        result = self._apply(display)
        # `applies` counts pushes Discord actually accepted, not attempts: a
        # wedged Discord must not inflate the health counter.
        if result.get("applied"):
            with self._lock:
                self.applies += 1
                self.last_apply = time.time()
        return result

    def _apply(self, display: dict) -> dict:
        """Hand one built display to Discord (outside _lock, see above)."""
        with self._apply_lock:
            ok = self.presence.set_activity(display)
        if self.verbose or not ok:
            bits = f"{display['details']} | {display['state']}"
            log(f"{'applied' if ok else 'apply FAILED'}: {bits}")  # DEBUG
        with self._lock:
            mode = self.mode
        return {
            "ok": True,
            "service": SERVICE,
            "version": VERSION,
            "discord": bool(ok),
            "applied": bool(ok),
            "mode": mode,
            "activity": display,
            "error": "" if ok else self.presence.last_error,
        }

    def clear(self, why: str = "") -> dict:
        with self._lock:
            self.facts = {}
            self.display = None
            self.last_push = 0.0
            self.mode = "cleared"   # stays off: the ticker must not resurrect it
        with self._apply_lock:
            ok = self.presence.clear()
        if why:
            log(f"cleared ({why})")  # DEBUG
        return {"ok": True, "service": SERVICE, "version": VERSION,
                "discord": bool(ok), "mode": "cleared", "activity": None,
                "error": "" if ok else self.presence.last_error}

    def status(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "service": SERVICE,
                "version": VERSION,
                "discord": bool(self.presence.ready),
                "client_id": self.presence.client_id,
                "applies": self.applies,
                "mode": self.mode,
                "last_push_age": (time.time() - self.last_push) if self.last_push else None,
                "activity": self.display,
                "facts": self.facts or None,
            }

    def tick(self) -> None:
        """The tab stopped reporting (game closed / crashed): clear the card.

        A stale match line is bad, but so is a resting "In Lobby" card for a
        game that is not running - the profile should show nothing at all.
        Lands in "idle" (not "cleared"): the next push goes live again, while
        an explicit /clear (the player turning presence off) is respected and
        is never undone here.
        """
        with self._lock:
            if self.mode != "live" or not self.last_push:
                return
            if time.time() - self.last_push <= self.stale:
                return
            self.facts = {}
            self.last_push = 0.0
            self.display = None
            self.mode = "idle"
        with self._apply_lock:
            self.presence.clear()
        log(f"no update for {self.stale:.0f}s - game quiet, activity cleared")


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = f"{SERVICE}/{VERSION}"
    protocol_version = "HTTP/1.1"
    bridge: Bridge = None  # type: ignore

    def log_message(self, fmt, *args):  # quieter default logging
        pass

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            log(f"response write failed: {exc}")  # DEBUG

    def do_OPTIONS(self):  # noqa: N802
        # 204 must not carry a body - answer the CORS preflight with headers
        # only (the generic _send would attach a `{}` JSON body).
        try:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception as exc:
            log(f"response write failed: {exc}")  # DEBUG

    def do_GET(self):  # noqa: N802
        path = (self.path or "/").split("?")[0].rstrip("/") or "/"
        if path in ("/health", "/status"):
            self._send(200, self.bridge.status())
        elif path == "/presence":
            self._send(200, self.bridge.status())
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        path = (self.path or "/").split("?")[0].rstrip("/") or "/"
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if 0 < length <= 1_000_000 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:
            log(f"bad json on {path}: {exc}")  # DEBUG
            self._send(400, {"ok": False, "error": "invalid json"})
            return
        if not isinstance(body, dict):
            self._send(400, {"ok": False, "error": "body must be an object"})
            return
        if path == "/presence":
            self._send(200, self.bridge.push(body))
        elif path == "/clear":
            self._send(200, self.bridge.clear("requested"))
        else:
            self._send(404, {"ok": False, "error": "not found"})


class _BridgeServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that refuses to share its port.

    Windows honours SO_REUSEADDR by letting a second process bind a port that
    is already listening, which silently splits requests between two bridges -
    and if the older one is a wedged leftover, the userscript's probe reaches
    it instead of the live one. Exclusive binding turns that into an ordinary
    "address in use" so make_server() walks to the next port.
    """

    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def _port_taken(host: str, port: int, timeout: float = 0.3) -> bool:
    """True when something already accepts connections on this port.

    A connect() answers that without parsing /health, and it also catches a
    listener too wedged to reply - exactly the one we must not bind alongside,
    because the client resolves the port by probing it.
    """
    probe = socket.socket()
    probe.settimeout(timeout)
    try:
        probe.connect((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            probe.close()
        except Exception:
            pass


def make_server(ports=DEFAULT_PORTS, host: str = DEFAULT_HOST, bridge: Bridge = None):
    """Bind the first free port. Returns (server, port). Raises OSError if none."""
    handler = type("_BridgeHandler", (_Handler,), {"bridge": bridge})
    last_exc = None
    for port in ports:
        if int(port) and _port_taken(host, port):
            last_exc = OSError(f"{host}:{int(port)} is already in use")
            log(f"port {port} already in use, trying next")  # DEBUG
            continue
        try:
            srv = _BridgeServer((host, int(port)), handler)
        except OSError as exc:
            last_exc = exc
            log(f"bind failed on port {port}: {exc}")  # DEBUG
            continue
        # port 0 asks the OS for a free port - report what we actually got.
        return srv, int(srv.server_address[1])
    raise last_exc or OSError("no free port")


def http_url(port: int) -> str:
    return f"http://{DEFAULT_HOST}:{port}"


def serve_in_thread(port=None, client_id: str = DEFAULT_CLIENT_ID, allow_join: bool = True,
                    stale: float = STALE_S, verbose: bool = False, allow_invite: bool = True):
    """Start the bridge in background threads (used by the tray app).

    Returns the running (bridge, server, port); call `stop_server(server)` to
    shut it down and clear the activity.
    """
    ports = (port,) if port else DEFAULT_PORTS
    bridge = Bridge(client_id=client_id, allow_join=allow_join, stale=stale,
                    verbose=verbose, allow_invite=allow_invite)
    server, bound = make_server(ports=ports, bridge=bridge)
    serve_forever_in_thread(server)
    threading.Thread(target=_stale_loop, args=(bridge,), daemon=True).start()
    # Warm the Discord dial right away (background thread: dialling Discord is
    # allowed to take its time, the local API must answer immediately). No
    # card is shown until the game reports - see Bridge.bootstrap.
    threading.Thread(target=bridge.bootstrap, daemon=True).start()
    log(f"listening on {http_url(bound)} -> Discord app {bridge.presence.client_id}"
        + ("" if allow_join else " (join button disabled)"))
    return bridge, server, bound


def serve_forever_in_thread(server):
    """Run serve_forever in a daemon thread, remembering it for stop_server().

    BaseServer.shutdown() waits for a serve_forever loop to acknowledge, so it
    must never be called on a server that was never started (it would block
    forever). This marks the server as serving.
    """
    setattr(server, "_nectar_serving", True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def stop_server(server) -> None:
    try:
        if getattr(server, "_nectar_serving", False):
            server.shutdown()
    except Exception as exc:
        log(f"shutdown() failed: {exc}")  # DEBUG
    try:
        server.server_close()
    except Exception as exc:
        log(f"server_close() failed: {exc}")  # DEBUG


def _stale_loop(bridge: Bridge) -> None:
    while True:
        time.sleep(1.0)
        try:
            bridge.tick()
        except Exception as exc:
            log(f"stale-loop tick failed: {exc}")  # DEBUG


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Nectar -> Discord rich presence bridge")
    parser.add_argument("--port", type=int, default=0,
                        help=f"preferred port (default: {DEFAULT_PORTS[0]}, then "
                             f"{'/'.join(str(p) for p in DEFAULT_PORTS[1:])} if busy)")
    parser.add_argument("--client-id", default=os.environ.get("DISCORD_CLIENT_ID") or DEFAULT_CLIENT_ID,
                        help="Discord application id")
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address (default loopback)")
    parser.add_argument("--stale", type=float, default=float(os.environ.get("RPC_STALE_S") or STALE_S),
                        help=f"clear the activity after this many seconds without an update "
                             f"(default {STALE_S:.0f})")
    parser.add_argument("--image", default=ICON_URL, help="large image url or asset key")
    parser.add_argument("--no-join", action="store_true",
                        help="never add the 'Join my party' button")
    parser.add_argument("--no-invite", action="store_true",
                        help="never add the 'Join Nectar' server invite button")
    parser.add_argument("--verbose", "-v", action="store_true", help="log every apply")
    args = parser.parse_args(argv)

    ports = (args.port,) if args.port else DEFAULT_PORTS
    bridge = Bridge(client_id=args.client_id, allow_join=not args.no_join,
                    stale=args.stale, image=args.image, verbose=args.verbose,
                    allow_invite=not args.no_invite)
    try:
        server, bound = make_server(ports=ports, host=args.host, bridge=bridge)
    except OSError as exc:
        print(f"[presence] no free port in {', '.join(str(p) for p in ports)}: {exc}")
        return 1

    log(f"Nectar rich presence bridge v{VERSION} (pypresence {_pypresence_version()})")
    log(f"local api  {http_url(bound)}/presence   (userscript: F4 > Presence)")

    def _report_discord():
        """Warm the dial, then say whether Discord answered.

        Off the main thread on purpose: the dial is allowed to take its time,
        and the local API has to answer the userscript immediately either way.
        No card is sent here - the game going live is what puts one up.
        """
        ready = bridge.bootstrap()
        ok = bridge.presence.ready
        log(f"discord    app {bridge.presence.client_id} - "
            + ("connected - waiting for the game"
               if (ok and ready) else
               ("connected" if ok else (bridge.presence.last_error or "waiting for Discord"))))

    threading.Thread(target=_report_discord, daemon=True).start()
    if args.no_join:
        log("join button disabled (--no-join)")
    if args.no_invite:
        log("nectar invite button disabled (--no-invite)")
    threading.Thread(target=_stale_loop, args=(bridge,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutdown requested (Ctrl+C)")  # DEBUG
    finally:
        stop_server(server)
        try:
            bridge.clear("shutdown")
        except Exception as exc:
            log(f"clear-on-shutdown failed: {exc}")  # DEBUG
        try:
            bridge.presence.stop()
        except Exception as exc:
            log(f"worker stop failed: {exc}")  # DEBUG
    return 0


if __name__ == "__main__":
    sys.exit(main())