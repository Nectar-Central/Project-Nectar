"""Duel thread creation must survive discord.py 2.7.

discord.py 2.7 dropped the `type` kwarg on Message/PartialMessage.create_thread,
so `_make_thread` failed EVERY call with
`PartialMessage.create_thread() got an unexpected keyword argument 'type'` —
wagers went live with no thread and nobody invited, silently.

Private threads are standalone (Discord forbids attaching them to a message),
so they must be created on the channel; only the public fallback is
message-attached. These tests pin that split with fakes (no Discord, no DB).
"""
import asyncio
import importlib.util
import sys
from pathlib import Path

import discord


def _load_duels():
    bot_dir = str(Path("companion/bot").resolve())
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    spec = importlib.util.spec_from_file_location(
        "duels_under_test", Path(bot_dir) / "duels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeThread:
    def __init__(self, tid, private):
        self.id = tid
        self.private = private


class _FakeChannel:
    """Mimics TextChannel.create_thread (type kwarg supported)."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    async def create_thread(self, *, name, auto_archive_duration=1440,
                            type=None, **kwargs):
        self.calls.append({"name": name, "type": type})
        if self.fail is not None:
            raise self.fail
        assert type == discord.ChannelType.private_thread, type
        return _FakeThread(11, True)


class _FakePartialMessage:
    """Mimics discord.py 2.7 PartialMessage.create_thread: NO `type` kwarg."""

    def __init__(self, channel, fail=None):
        self.channel = channel
        self.calls = []
        self.fail = fail

    async def create_thread(self, *, name, auto_archive_duration=1440,
                            slowmode_delay=None, reason=None):
        self.calls.append({"name": name})
        if self.fail is not None:
            raise self.fail
        return _FakeThread(22, False)


def test_private_thread_created_on_channel():
    duels = _load_duels()
    ch = _FakeChannel()
    msg = _FakePartialMessage(ch)
    thread, is_priv = asyncio.run(duels._make_thread(msg, "wager-abc"))
    assert is_priv is True
    assert thread.id == 11
    assert ch.calls and ch.calls[0]["type"] == discord.ChannelType.private_thread
    assert msg.calls == []  # message path never touched


def test_public_fallback_omits_type_kwarg():
    duels = _load_duels()
    ch = _FakeChannel(fail=Exception("403 missing perms"))
    msg = _FakePartialMessage(ch)
    thread, is_priv = asyncio.run(duels._make_thread(msg, "wager-abc"))
    assert is_priv is False
    assert thread.id == 22
    # The fallback must NOT pass `type` — that kwarg no longer exists on
    # Message/PartialMessage.create_thread (the reported crash).
    assert msg.calls == [{"name": "wager-abc"}]


def test_total_failure_returns_none_never_raises():
    duels = _load_duels()
    ch = _FakeChannel(fail=Exception("403"))
    msg = _FakePartialMessage(ch, fail=Exception("404"))
    assert asyncio.run(duels._make_thread(msg, "wager-abc")) == (None, False)


def test_no_channel_uses_message_path():
    duels = _load_duels()

    class _NoChannel:
        channel = None

        async def create_thread(self, *, name, auto_archive_duration=1440,
                                slowmode_delay=None, reason=None):
            return _FakeThread(33, False)

    thread, is_priv = asyncio.run(duels._make_thread(_NoChannel(), "wager-abc"))
    assert (thread.id, is_priv) == (33, False)
