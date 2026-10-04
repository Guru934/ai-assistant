"""Mic-worker / input-stream single-ownership regression tests.

Reproduces the observed failure (two mic workers alive for one session
generation) and proves the invariant: exactly one mic consumer per live
session, one authoritative input stream, clean generation ownership.
No hardware, network, speakers, or credentials.
"""

import asyncio
import logging
import os
import sys
import threading
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from test_session_lifecycle import (
    FakeSession,
    make_agent,
    wire_connect,
)


def _drive(agent, stop_after, actions=()):
    import asyncio as _aio

    real_sleep = _aio.sleep

    async def fast_sleep(delay, *a, **k):
        await real_sleep(min(delay, 0.02), *a, **k)

    async def go():
        loop = asyncio.get_running_loop()
        for delay, fn in actions:
            loop.call_later(delay, fn)
        loop.call_later(stop_after, agent.stop_event.set)
        await asyncio.wait_for(agent.run_loop(), timeout=stop_after + 10.0)

    with patch.object(_aio, "sleep", fast_sleep):
        _aio.run(go())


# ─── double-setup guard ───────────────────────────────────────────

def test_ensure_kills_leftover_workers_before_new_ones():
    """Attempted duplicate setup: leftovers cancelled, none survive."""
    agent = make_agent()

    async def go():
        loop = asyncio.get_running_loop()
        order = []

        async def _old():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                order.append("old-cancelled")
                raise

        leftovers = [loop.create_task(_old()) for _ in range(3)]
        await asyncio.sleep(0)  # let tasks park in Event.wait() first
        agent._session_tasks = leftovers
        await agent._ensure_no_live_session_workers()
        assert all(t.done() for t in leftovers)
        assert order == ["old-cancelled"] * 3

    asyncio.run(go())


def test_ensure_noop_when_nothing_live(caplog):
    agent = make_agent()

    async def go():
        await agent._ensure_no_live_session_workers()

    with caplog.at_level(logging.WARNING, logger="cat_talker.agent"):
        asyncio.run(go())
    assert not [r for r in caplog.records if "raced teardown" in r.message]


# ─── one mic worker per session, peak never exceeds 1 ─────────────

def test_repeated_cycles_peak_one_and_single_start_per_gen(caplog):
    agent = make_agent()
    sessions = [FakeSession(hang=True) for _ in range(4)]
    count = wire_connect(agent, sessions)
    audio = agent.audio
    actions = []
    t = 0.2
    for _ in range(3):
        actions.append((t, agent.request_sleep))
        t += 0.2
        actions.append((t, agent.request_wake))
        t += 0.2
    actions.append((t + 0.1,
                    lambda: audio.audio_in_queue.put_nowait(b"live")))
    with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
        _drive(agent, t + 0.5, actions)
    assert count[0] == 4
    assert agent._session_generation == 4
    assert agent._mic_peak == 1, f"peak mic concurrency {agent._mic_peak}"
    assert agent._mic_live == 0
    starts = [r.getMessage() for r in caplog.records
              if "mic worker START" in r.getMessage()]
    assert len(starts) == 4, starts
    assert sorted(int(s.split("session=")[1].split()[0]) for s in starts) \
        == [1, 2, 3, 4]
    # The live chunk reached only the live session.
    assert sessions[3].sent_audio == [b"live"]
    assert sessions[0].sent_audio == []
    assert sessions[1].sent_audio == []
    assert sessions[2].sent_audio == []


def test_f3_shutdown_cancels_workers_without_reconnect():
    agent = make_agent()
    first = FakeSession(hang=True)
    count = wire_connect(agent, [first])
    _drive(agent, 0.4, [])
    assert count[0] == 1
    assert first.exited
    assert agent._mic_peak <= 1
    assert agent._mic_live == 0
    assert not [t for t in agent._session_tasks if not t.done()]


# ─── watchdog stream recreation ───────────────────────────────────

def _iface(**overrides):
    from cat_talker.audio import AudioInterface
    iface = AudioInterface.__new__(AudioInterface)
    iface._closed = False
    iface._running = True
    iface._loop_closed = False
    iface._echo_enabled = False
    iface._stream_id = 1
    iface._stream_lock = threading.Lock()
    for k, v in overrides.items():
        setattr(iface, k, v)
    return iface


class _FakeStream:
    def __init__(self, active=True, fail_open=False):
        self._active = active
        self.fail_open = fail_open
        self.stopped = 0
        self.closed = 0

    def is_active(self):
        if isinstance(self._active, Exception):
            raise self._active
        return self._active

    def stop_stream(self):
        self.stopped += 1

    def close(self):
        self.closed += 1


class _FakePa:
    def __init__(self):
        self.opened = []

    def open(self, **kwargs):
        stream = _FakeStream(active=True)
        self.opened.append((kwargs, stream))
        return stream


def test_watchdog_healthy_stream_untouched():
    iface = _iface(in_stream=_FakeStream(active=True),
                   pyaudio=_FakePa())
    assert iface._maybe_recreate_input_stream() == "healthy"
    assert iface._stream_id == 1
    assert iface.pyaudio.opened == []


def test_watchdog_recreates_dead_stream_once(caplog):
    dead = _FakeStream(active=False)
    pa = _FakePa()
    iface = _iface(in_stream=dead, pyaudio=pa)
    with caplog.at_level(logging.INFO, logger="cat_talker.audio"):
        assert iface._maybe_recreate_input_stream() == "ok"
    assert dead.stopped == 1 and dead.closed == 1
    assert len(pa.opened) == 1
    assert pa.opened[0][0]["stream_callback"] is not None
    assert iface._stream_id == 2
    assert iface.in_stream is pa.opened[0][1]
    assert any("stream_id=2" in r.getMessage() for r in caplog.records)


def test_watchdog_exception_means_recreate():
    iface = _iface(in_stream=_FakeStream(active=RuntimeError("gone")),
                   pyaudio=_FakePa())
    assert iface._maybe_recreate_input_stream() == "ok"
    assert iface._stream_id == 2


def test_watchdog_skipped_when_closed():
    iface = _iface(in_stream=_FakeStream(active=False),
                   pyaudio=_FakePa())
    iface._closed = True
    iface._running = False
    assert iface._maybe_recreate_input_stream() == "skipped"
    assert iface.pyaudio.opened == []


def test_watchdog_recreate_failure_is_harmless():
    class _BadPa:
        def open(self, **kwargs):
            raise OSError("no device")

    iface = _iface(in_stream=_FakeStream(active=False), pyaudio=_BadPa())
    assert iface._maybe_recreate_input_stream() == "skipped"


def test_concurrent_recreate_serialized():
    """Two racing recreates still yield exactly one replacement."""
    pa = _FakePa()
    iface = _iface(in_stream=_FakeStream(active=False), pyaudio=pa)
    results = []

    def one():
        results.append(iface._maybe_recreate_input_stream())

    threads = [threading.Thread(target=one) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(pa.opened) == 1, f"{len(pa.opened)} streams opened"
    assert iface._stream_id == 2


# ─── control-socket single ownership ──────────────────────────────

def test_second_server_reports_failure_and_raises(tmp_path):
    """A second server for one runtime dir is refused BEFORE touching
    the live socket: no unlink, no steal, no headless duplicate."""
    from cat_talker.control import send_command, serve_forever
    import threading as _threading

    path = str(tmp_path / "control.sock")
    stop = threading.Event()
    first_states = []
    t = threading.Thread(
        target=serve_forever,
        kwargs={"get_agent": lambda: None, "path": path,
                "stop_event": stop,
                "on_ready": first_states.append},
        daemon=True)
    t.start()
    for _ in range(100):
        if first_states:
            break
        import time as _time
        _time.sleep(0.02)
    assert first_states == [True]
    second_states = []
    with pytest.raises(OSError):
        serve_forever(get_agent=lambda: None, path=path,
                      on_ready=second_states.append)
    assert second_states == [False]
    # The first server still owns the path and still answers
    # (its socket file was never unlinked/stolen).
    reply = send_command("status", path=path)
    assert "ok" in reply
    stop.set()
    t.join(timeout=5)
