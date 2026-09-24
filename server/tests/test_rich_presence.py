"""Rich presence bridge: Discord app data -> the lines Discord shows.

companion/rich_presence.py is the local API the userscript posts to (F4 >
Presence). These tests pin the parts that are easy to get subtly wrong and hard
to notice in game: the composed "In Match - Ranked Session" headline with the
mode/occupancy/party on the line below it (never the same fact twice), the
join button that must exist *only* for a party that carries a shareable code,
the lobby reading Idle while nothing is picked, the
mapping onto pypresence's `Presence.update(**kwargs)`, the resting "In Lobby"
card, and the HTTP contract plus port fallback.

No Discord, no network and no pypresence: the Discord side is replaced by a
recorder, and the bridge hands every Discord call its own deadline so a wedged
pipe can never park the local API.
"""
import importlib.util
import json
import socket
import threading
import time
import urllib.error
import urllib.request


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "rich_presence_under_test", "companion/rich_presence.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakePresence:
    """Stands in for DiscordPresence: records displays instead of talking to Discord."""

    def __init__(self):
        self.client_id = "fake"
        self.ready = True
        self.last_error = ""
        self.calls = []

    def set_activity(self, display):
        self.calls.append(display)
        return True

    def clear(self):
        self.calls.append(None)
        return True

    def stop(self):
        pass


class _FakeRPC:
    """Stands in for pypresence's `Presence`: records connect/update/clear.

    pypresence raises (DiscordNotFound / InvalidPipe / PipeClosed) rather than
    returning False, and `update()` is only legal after `connect()`, so the
    fake mirrors that: reads/writes before connect() are an error.
    """

    def __init__(self, connect=True, accept=True, block=0.0, connect_block=0.0):
        self.accept = accept
        self.block = block
        self.connect_block = connect_block
        self.will_connect = connect
        self.connected = False
        self.calls = []
        self.clears = 0
        self.connects = 0

    def connect(self):
        self.connects += 1
        if self.connect_block:
            time.sleep(self.connect_block)
        if not self.will_connect:
            raise RuntimeError("Discord not found (no discord-ipc-N pipe)")
        self.connected = True
        return self

    def update(self, **kwargs):
        if not self.connected:
            raise RuntimeError("PipeClosed: connect() first")
        if self.block:
            time.sleep(self.block)
        self.calls.append(kwargs)
        return self.accept

    def clear(self):
        if not self.connected:
            raise RuntimeError("PipeClosed: connect() first")
        self.clears += 1
        return True

    def close(self):
        self.connected = False


def _facts(**over):
    base = {
        "version": "2.24.0",
        "ts": int(time.time() * 1000),
        "since": int(time.time() * 1000) - 60_000,
        "user": "cis6led",
        "status": "lobby",
        "party": {"active": False, "private": False, "pid": None},
        "mode": {"key": None, "label": None},
        "context": {"kind": None, "name": None, "x": None, "y": None, "extra": None},
        "players": {"cur": None, "max": None},
        "points": None,
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged = dict(base[key])
            merged.update(value)
            base[key] = merged
        else:
            base[key] = value
    return base


def test_game_mode_labels_are_human_readable():
    rp = _load_bridge()
    assert rp.gm_label("zw_lg") == "Late Game Zonewars"
    assert rp.gm_label("wwzw") == "Duo Wild West"
    assert rp.gm_label("bf2v2") == "Boxfights 2v2"
    # unknown keys degrade to something readable rather than raw
    assert rp.gm_label("some_new_mode") == "Some New Mode"
    assert rp.gm_label(None) == ""


def test_match_line_uses_room_occupancy_but_never_ladder_state():
    """Rank and points never reach the card: the whole friends list sees it."""
    rp = _load_bridge()
    d = rp.build_display(_facts(
        status="match",
        mode={"key": "zw_lg", "label": "Late Game Zonewars"},
        context={"kind": "ranked", "name": "Ranked Session", "x": 3, "y": None},
        players={"cur": 8, "max": 12},
        points=8488,
    ))
    # Line one is the place + the session, nothing else.
    assert d["details"] == "In Match - Ranked Session"
    assert "8/12" not in d["details"]
    # Line two is the mode and the occupancy - and never the series pair.
    assert "Late Game Zonewars" in d["state"]
    assert "8/12 players" in d["state"]
    assert "3/12" not in d["state"]
    assert "8,488" not in d["state"]
    assert "pts" not in d["state"]


def test_ladder_extras_from_a_stale_userscript_are_dropped():
    """An older client pushing rank/points has them dropped by the bridge."""
    rp = _load_bridge()
    ranked = rp.build_display(_facts(
        context={"kind": "tourney", "name": "Nectar Cup #4", "extra": "rank 3/24"}))
    assert "rank" not in ranked["state"] and "3/24" not in ranked["state"]
    points = rp.build_display(_facts(
        context={"kind": "ranked", "name": "Ranked Session", "extra": "8,488 pts"}))
    assert "8,488" not in points["state"] and "pts" not in points["state"]

    # Field size is not ladder state, so it still rides along.
    field = rp.build_display(_facts(
        context={"kind": "tourney", "name": "Nectar Cup #4", "extra": "24 in tourney"}))
    assert "24 in tourney" in field["state"]


def test_counts_are_match_players_over_room_size_never_the_series():
    """(X/Y) is who is in the match over the room max - not tourney progress.

    A tourney series pair (sealed / series length) used to take precedence and
    read as nonsense against a room of 16 players.
    """
    rp = _load_bridge()
    d = rp.build_display(_facts(
        status="match",
        mode={"key": "bf", "label": "Boxfights"},
        context={"kind": "tourney", "name": "Nectar Cup #4", "x": 3, "y": 5},
        players={"cur": 15, "max": 16},
    ))
    assert d["details"] == "In Match - Nectar Cup #4"
    assert "15/16 players" in d["state"]
    assert "3/5" not in d["details"] and "3/5" not in d["state"]

    # a room that fills up moves the numbers
    fuller = rp.build_display(_facts(
        status="match", context={"kind": "tourney", "name": "Nectar Cup #4"},
        players={"cur": 16, "max": 16}))
    assert "16/16 players" in fuller["state"]

    # no room data at all: no numbers rather than made-up ones, and the mode
    # the details line cannot carry is not invented either
    blank = rp.build_display(_facts(status="match", context={"kind": "ranked", "name": "Ranked Session"}))
    assert blank["details"] == "In Match - Ranked Session"
    assert blank["state"] == "In a match"


def test_party_lobby_and_menu_lines():
    rp = _load_bridge()
    party = rp.build_display(_facts(
        status="lobby",
        party={"active": True, "private": True, "pid": "ABC12"},
        mode={"key": "zw", "label": "Zone Wars"},
        players={"cur": 2, "max": 4},
    ))
    # A party in the lobby is still the lobby: "In Party" over "party" told a
    # friend nothing and the two lines must never repeat each other.
    assert party["details"] == "In Lobby"
    assert "In Party" not in party["details"]
    assert party["state"] == "Zone Wars \u00b7 2/4 players \u00b7 private party"
    assert "private party" in party["state"]

    # a bare party with nothing else known still reads as a lobby + a party,
    # with the mode slot saying Idle because nothing is being played yet
    bare = rp.build_display(_facts(
        status="lobby", party={"active": True, "private": False, "pid": None}))
    assert bare["details"] == "In Lobby"
    assert bare["state"] == "Idle \u00b7 in a party"

    # Lobby is the resting state: a menu/unknown status is the lobby, not a
    # separate "In Menu" line nobody asked for - and with nothing known the
    # card says Idle rather than inventing a match to wait for.
    menu = rp.build_display(_facts(status="menu"))
    assert menu["details"] == "In Lobby"
    assert menu["state"] == "Idle"
    empty = rp.build_display({})
    assert empty["details"] == "In Lobby"
    assert empty["state"] == "Idle"
    assert [b["label"] for b in empty["buttons"]] == ["Join Nectar"]


def test_lobby_default_mode_reads_idle_but_a_match_keeps_its_mode():
    """df (Free Mode) is the client's boot default, not a mode being played.

    The game reports it until a real mode is adopted, so a lobby card used to
    announce "Free Mode" about a player who was standing in the lobby. In a
    match the same key is the mode being played, so it stays on the card.
    """
    rp = _load_bridge()
    lobby = rp.build_display(_facts(
        status="lobby", mode={"key": "df", "label": "Free Mode"}))
    assert lobby["state"] == "Idle"
    assert "Free Mode" not in lobby["state"]

    # other placeholders read the same way: a lobby session with no mode at all,
    # and the lobby's own MainWorld label
    assert rp.build_display(_facts(status="lobby"))["state"] == "Idle"
    assert rp.build_display(_facts(
        status="lobby", mode={"key": "MainWorld", "label": "Free Mode"}))["state"] == "Idle"

    # the party is a real fact, so it still rides along next to Idle
    with_party = rp.build_display(_facts(
        status="lobby", mode={"key": "df", "label": "Free Mode"},
        party={"active": True, "private": True, "pid": "ABC12"}))
    assert with_party["state"] == "Idle \u00b7 private party"

    # a match keeps the mode, and so does a real mode in the lobby
    match = rp.build_display(_facts(
        status="match", mode={"key": "df", "label": "Free Mode"}))
    assert "Free Mode" in match["state"]
    real = rp.build_display(_facts(
        status="lobby", mode={"key": "zw", "label": "Zone Wars"}))
    assert real["state"] == "Zone Wars"


def test_nectar_invite_is_always_there_and_party_join_only_when_private():
    rp = _load_bridge()
    nectar = {"label": "Join Nectar", "url": "https://discord.gg/GDSW9p39J5"}
    party_url = "https://www.crazygames.com/game/buildnow-gg?czy_invite=true&pID=ABC12"

    # Discord allows two buttons: the party invite first, then the community one
    private = rp.build_display(_facts(
        party={"active": True, "private": True, "pid": "ABC12"}))
    assert private["buttons"] == [
        {"label": "Join my party", "url": party_url}, nectar]

    for shape in ({"active": True, "private": False, "pid": "ABC12"},
                  {"active": True, "private": True, "pid": None},
                  {"active": False, "private": False, "pid": None},
                  # a garbage pID must never be pasted into a user-facing link
                  {"active": True, "private": True, "pid": "A B/../x"}):
        assert rp.build_display(_facts(party=shape))["buttons"] == [nectar]

    no_join = rp.build_display(
        _facts(party={"active": True, "private": True, "pid": "ABC12"}), allow_join=False)
    assert no_join["buttons"] == [nectar]
    no_invite = rp.build_display(
        _facts(party={"active": True, "private": True, "pid": "ABC12"}), allow_invite=False)
    assert no_invite["buttons"] == [{"label": "Join my party", "url": party_url}]
    neither = rp.build_display(_facts(), allow_join=False, allow_invite=False)
    assert neither["buttons"] == []


def test_display_is_handed_to_pypresence_as_update_kwargs():
    """build_display() -> Presence.update(**kwargs): name, image, start, buttons.

    pypresence owns the pipe and the handshake, so the only thing this module
    can get wrong is the mapping - and two of its fields are quietly different
    from the old client: `start` is epoch MILLISECONDS, and the clickable-URL
    fields are state_url/details_url/large_url.
    """
    rp = _load_bridge()
    rpc = _FakeRPC()
    presence = rp.DiscordPresence(client_id="1", rpc=rpc)
    since = int(time.time() * 1000) - 30_000
    display = rp.build_display(_facts(
        status="match",
        mode={"key": "bf", "label": "Boxfights"},
        context={"kind": "tourney", "name": "N" * 300, "x": 2, "y": 4},
        players={"cur": 4, "max": 4},
        party={"active": True, "private": True, "pid": "ZZ999"},
        since=since,
    ))
    try:
        assert presence.set_activity(display) is True
    finally:
        presence.stop()
    assert rpc.connects == 1  # connect() happens once, before the first update
    kwargs = rpc.calls[0]
    assert kwargs["name"] == "Buildnow.gg with Nectar"
    assert len(kwargs["details"]) <= rp.MAX_LEN
    assert len(kwargs["state"]) <= rp.MAX_LEN
    assert kwargs["large_image"].endswith("discordlogo.png")
    assert kwargs["large_text"] == "Buildnow.gg with Nectar"
    # milliseconds, not seconds: pypresence's clock would show 1970 otherwise
    assert abs(kwargs["start"] - since) < 1000
    assert kwargs["buttons"][0]["url"].endswith("pID=ZZ999")
    assert [b["label"] for b in kwargs["buttons"]] == ["Join my party", "Join Nectar"]
    # clickable rich presence, and PLAYING is what renders "Playing <app>"
    assert kwargs["large_url"] == rp.NECTAR_INVITE
    assert kwargs["details_url"].endswith("pID=ZZ999")
    assert kwargs["state_url"].endswith("pID=ZZ999")
    if kwargs.get("activity_type") is not None:
        assert int(getattr(kwargs["activity_type"], "value", -1)) == 0

    # a nonsense/far-past start timestamp is dropped rather than sent to Discord
    stale = rp.build_display(_facts(since=10))
    assert "start" not in stale
    assert "start" not in rp._display_to_update_kwargs({"details": "x", "state": "y"})
    # and nothing but http(s) links ever reaches a clickable field
    assert "large_url" not in rp._display_to_update_kwargs(
        {"details": "x", "state": "y", "large_url": "javascript:alert(1)"})


def test_clear_only_touches_a_client_that_is_up():
    """A dead pipe is never poked: connect() first, and only then clear()."""
    rp = _load_bridge()
    rpc = _FakeRPC()
    presence = rp.DiscordPresence(client_id="1", rpc=rpc)
    try:
        assert presence.clear() is True
        assert rpc.clears == 1
        assert rpc.connects == 1
    finally:
        presence.stop()

    # Discord not running: connect() raises, so clear() is never attempted -
    # and the caller gets an honest False plus the reason.
    off = _FakeRPC(connect=False)
    idle = rp.DiscordPresence(client_id="1", rpc=off, timeout=0.5)
    try:
        assert idle.clear() is False
        assert off.clears == 0
        assert "pypresence" in idle.last_error or "Discord" in idle.last_error
    finally:
        idle.stop()


def test_a_stalled_discord_call_times_out_instead_of_parking_the_bridge():
    """pypresence's update() can block with no deadline - ours must not.

    A pipe Discord stopped draining used to park the caller (and every request
    behind it) forever, so the bridge hands each call to its worker thread and
    waits its own CALL_TIMEOUT instead of waiting with it.
    """
    rp = _load_bridge()
    rpc = _FakeRPC(block=5.0)
    presence = rp.DiscordPresence(client_id="1", rpc=rpc, timeout=0.3)
    begin = time.perf_counter()
    try:
        assert presence.set_activity({"details": "x", "state": "y"}) is False
    finally:
        presence.stop()
    elapsed = time.perf_counter() - begin
    assert elapsed < 1.0, f"caller waited {elapsed:.2f}s on a stalled Discord"
    assert "did not answer" in presence.last_error
    assert presence.ready is False  # abandoned, so the next push dials fresh


def test_a_slow_dial_never_holds_up_startup():
    """connect()'s pipe open + READY round trip belongs to the worker thread."""
    rp = _load_bridge()
    dialling = threading.Event()

    def slow_factory(client_id):
        dialling.set()
        time.sleep(1.5)
        return _FakeRPC()

    begin = time.perf_counter()
    presence = rp.DiscordPresence(client_id="1", factory=slow_factory,
                                  timeout=0.3, dial_timeout=0.3)
    elapsed = time.perf_counter() - begin
    try:
        assert elapsed < 0.5, f"building the bridge waited {elapsed:.2f}s on the dial"
        assert dialling.wait(2.0), "the warmup dial never ran"
        deadline = time.time() + 4
        while not presence.ready and time.time() < deadline:
            time.sleep(0.05)
        assert presence.ready is True  # the dial finished on its own thread
    finally:
        presence.stop()


def test_a_missing_dependency_is_reported_not_fatal():
    """No pypresence installed: the bridge still serves and says exactly why."""
    rp = _load_bridge()

    def no_module(client_id):
        raise ImportError("No module named 'pypresence'")

    presence = rp.DiscordPresence(client_id="1", factory=no_module, timeout=0.5)
    bridge = rp.Bridge(presence=presence)
    server, port = rp.make_server(ports=(0,), bridge=bridge)
    rp.serve_forever_in_thread(server)
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(base + "/health", timeout=5) as r:
            assert json.loads(r.read().decode())["ok"] is True
        body = json.dumps(_facts(status="match", mode={"key": "zw", "label": "Zone Wars"})).encode()
        req = urllib.request.Request(base + "/presence", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            applied = json.loads(r.read().decode())
        assert applied["ok"] is True
        assert applied["discord"] is False
        assert "pypresence" in applied["error"]
        assert "pip install" in applied["error"]
    finally:
        rp.stop_server(server)
        presence.stop()


def test_http_stays_responsive_while_discord_is_stalled():
    """End-to-end: a stalled Discord call must not stall the local API."""
    rp = _load_bridge()
    presence = rp.DiscordPresence(client_id="1", rpc=_FakeRPC(block=5.0), timeout=0.3)
    bridge = rp.Bridge(presence=presence)
    server, port = rp.make_server(ports=(0,), bridge=bridge)
    rp.serve_forever_in_thread(server)
    base = f"http://127.0.0.1:{port}"
    try:
        body = json.dumps(_facts(status="match", mode={"key": "zw", "label": "Zone Wars"})).encode()
        req = urllib.request.Request(base + "/presence", data=body,
                                     headers={"Content-Type": "application/json"})
        begin = time.perf_counter()
        with urllib.request.urlopen(req, timeout=8) as r:
            applied = json.loads(r.read().decode())
        elapsed = time.perf_counter() - begin
        assert elapsed < 2, f"push took {elapsed:.2f}s with Discord stalled"
        assert applied["ok"] is True
        assert applied["discord"] is False  # honest: nothing reached Discord
        assert applied["error"]
        # and the API keeps answering
        with urllib.request.urlopen(base + "/health", timeout=5) as r:
            assert json.loads(r.read().decode())["ok"] is True
    finally:
        rp.stop_server(server)
        presence.stop()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_http_api_applies_facts_then_clears():
    rp = _load_bridge()
    ipc = _FakePresence()
    bridge = rp.Bridge(presence=ipc)
    server, port = rp.make_server(ports=(0,), bridge=bridge)
    rp.serve_forever_in_thread(server)
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(base + "/health", timeout=5) as r:
            health = json.loads(r.read().decode())
        assert health["service"] == "nectar-rich-presence"
        assert health["ok"] is True
        # No push yet: nothing on Discord and mode idle - the card stays empty
        # until the game reports, which is what makes the F4 preview trustworthy.
        assert health["activity"] is None
        assert health["mode"] == "idle"

        body = json.dumps(_facts(
            status="match",
            mode={"key": "zw", "label": "Zone Wars"},
            players={"cur": 1, "max": 12},
            party={"active": True, "private": True, "pid": "P1Q2R"},
        )).encode()
        req = urllib.request.Request(base + "/presence", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            applied = json.loads(r.read().decode())
        assert applied["ok"] is True
        assert applied["discord"] is True
        assert applied["activity"]["details"] == "In Match"
        assert applied["activity"]["state"] == "Zone Wars \u00b7 1/12 players \u00b7 private party"
        assert applied["activity"]["buttons"][0]["label"] == "Join my party"
        assert ipc.calls[-1]["details"] == "In Match"
        assert ipc.calls[-1]["buttons"][0]["label"] == "Join my party"

        with urllib.request.urlopen(base + "/presence", timeout=5) as r:
            again = json.loads(r.read().decode())
        assert again["last_push_age"] is not None

        req = urllib.request.Request(base + "/clear", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            cleared = json.loads(r.read().decode())
        assert cleared["activity"] is None
        assert ipc.calls[-1] is None

        with urllib.request.urlopen(base + "/health", timeout=5) as r:
            assert json.loads(r.read().decode())["activity"] is None
    finally:
        rp.stop_server(server)


def test_http_api_rejects_bad_payloads():
    rp = _load_bridge()
    bridge = rp.Bridge(presence=_FakePresence())
    server, port = rp.make_server(ports=(0,), bridge=bridge)
    rp.serve_forever_in_thread(server)
    base = f"http://127.0.0.1:{port}"
    try:
        for path, payload in (("/presence", b"{not json"), ("/presence", b"[1,2,3]")):
            req = urllib.request.Request(base + path, data=payload,
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=5)
                raise AssertionError("expected 400")
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
        try:
            urllib.request.urlopen(base + "/nope", timeout=5)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        rp.stop_server(server)


def test_port_fallback_when_the_preferred_port_is_taken():
    rp = _load_bridge()
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    taken = busy.getsockname()[1]
    try:
        bridge = rp.Bridge(presence=_FakePresence())
        server, port = rp.make_server(ports=(taken, 0), bridge=bridge)
        try:
            assert port != taken
        finally:
            rp.stop_server(server)  # must not block when serve_forever never ran
    finally:
        busy.close()


def test_going_quiet_clears_the_card():
    """Game closed / crashed: the profile goes blank, not "In Lobby".

    A resting card for a game that is not running is exactly what lingered on
    profiles - the honest state is no activity until the game reports again.
    """
    rp = _load_bridge()
    ipc = _FakePresence()
    bridge = rp.Bridge(presence=ipc, stale=3.0)
    bridge.push(_facts(status="match", mode={"key": "zw", "label": "Zone Wars"}))
    assert ipc.calls[-1]["details"].startswith("In Match")
    assert bridge.status()["mode"] == "live"
    # pretend the tab stopped reporting a while ago
    bridge.last_push = time.time() - 30
    bridge.tick()
    assert bridge.display is None
    assert bridge.status()["activity"] is None
    assert ipc.calls[-1] is None  # presence.clear() reached Discord
    assert bridge.status()["mode"] == "idle"
    # a second idle tick does nothing at all (no Discord traffic)
    before = len(ipc.calls)
    bridge.tick()
    assert len(ipc.calls) == before
    # ... and the next push (game back) goes live again
    bridge.push(_facts(status="match", mode={"key": "zw", "label": "Zone Wars"}))
    assert bridge.status()["mode"] == "live"
    assert ipc.calls[-1]["details"].startswith("In Match")


def test_explicit_clear_stays_off():
    """The player turning presence off must not be undone by the ticker."""
    rp = _load_bridge()
    ipc = _FakePresence()
    bridge = rp.Bridge(presence=ipc, stale=3.0)
    bridge.clear("requested")
    assert bridge.status()["activity"] is None
    assert bridge.status()["mode"] == "cleared"
    bridge.last_push = time.time() - 30
    bridge.tick()
    assert bridge.status()["activity"] is None
    assert ipc.calls[-1] is None
    assert bridge.bootstrap() is False   # and startup respects it too


def test_bootstrap_sets_no_card_and_warms_the_dial():
    """Startup: no activity until the game reports; the dial is warmed so the
    first push is fast. A resting card here is what lingered after closes."""
    rp = _load_bridge()

    class _Warm(_FakePresence):
        def __init__(self):
            super().__init__()
            self.warmups = 0

        def warmup(self):
            self.warmups += 1
            return True

    ipc = _Warm()
    bridge = rp.Bridge(presence=ipc)
    assert bridge.status()["mode"] == "idle"
    assert bridge.status()["activity"] is None
    assert bridge.bootstrap() is True
    assert ipc.warmups == 1
    assert bridge.status()["activity"] is None  # still nothing on Discord
    # /health carries the emptiness, so the F4 panel shows idle, not a card
    assert bridge.status()["mode"] == "idle"


class _SlowIPC(_FakePresence):
    """Accepts the activity, but only after Discord has made us wait."""

    def __init__(self, delay=1.5):
        super().__init__()
        self.delay = delay

    def set_activity(self, activity):
        time.sleep(self.delay)
        return super().set_activity(activity)


def test_health_answers_while_discord_is_slow_to_accept():
    """A slow Discord write must not queue the whole local API behind it."""
    rp = _load_bridge()
    bridge = rp.Bridge(presence=_SlowIPC(1.5))
    server, port = rp.make_server(ports=(0,), bridge=bridge)
    rp.serve_forever_in_thread(server)
    base = f"http://127.0.0.1:{port}"
    result = {}

    def push():
        body = json.dumps(_facts(
            status="match", mode={"key": "zw", "label": "Zone Wars"})).encode()
        req = urllib.request.Request(base + "/presence", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                result["applied"] = json.loads(r.read().decode())
        except Exception as exc:  # surfaced by the assertions below
            result["error"] = exc

    try:
        worker = threading.Thread(target=push, daemon=True)
        worker.start()
        time.sleep(0.3)  # let the push reach Discord and start waiting
        begin = time.perf_counter()
        with urllib.request.urlopen(base + "/health", timeout=6) as r:
            health = json.loads(r.read().decode())
        elapsed = time.perf_counter() - begin
        assert health["ok"] is True
        assert health["activity"]["details"] == "In Match"
        assert health["activity"]["state"] == "Zone Wars"
        assert elapsed < 0.6, f"/health waited {elapsed:.2f}s behind a slow Discord write"
        worker.join(timeout=15)
        assert "error" not in result, result.get("error")
        assert result["applied"]["discord"] is True
    finally:
        rp.stop_server(server)


def test_two_bridges_never_share_one_port():
    """The second bridge walks to the next port instead of double-binding.

    Windows lets a second process bind a port that is already listening unless
    the bind asks for exclusivity, which silently split requests between a live
    bridge and a wedged leftover.
    """
    rp = _load_bridge()
    first, first_port = rp.make_server(ports=(0,), bridge=rp.Bridge(presence=_FakePresence()))
    rp.serve_forever_in_thread(first)
    try:
        second, second_port = rp.make_server(ports=(first_port, 0),
                                             bridge=rp.Bridge(presence=_FakePresence()))
        try:
            assert second_port != first_port
        finally:
            rp.stop_server(second)
    finally:
        rp.stop_server(first)
