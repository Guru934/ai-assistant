"""Voice reliability: repeated sleep/wake cycles, buffers, guards, media.

Deterministic regression net for the intermittent post-wake voice
failure. No hardware, network, speakers, or credentials: FakeSession
models the Live SDK, subprocess/playerctl are mocked.

- 5 consecutive sleep/wake cycles: one capture path, no task growth,
  no duplicate streams, clean queues, correct pause/suppression state,
  exact session-generation ownership, prompt first chunk, no stale
  delivery, dead sessions stay dead.
- EchoSuppressor reference staging stays bounded with no consumption.
- Tiny/foreign-script transcripts never reach command parsing.
- Read-only MPRIS watcher: playing edge sleeps, stopped stays asleep,
  failures are harmless, exactly one sleep per edge.
"""

import asyncio
import logging
import os
import sys
import types as pytypes
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from test_session_lifecycle import (
    FakeSession,
    _input_transcription_msg,
    _turn_complete_msg,
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


def _sleep_wake_actions(cycles, sleep_at=0.2, step=0.15, wake_gap=0.15):
    actions = []
    t = sleep_at
    for _ in range(cycles):
        actions.append((t, "sleep"))
        t += wake_gap
        actions.append((t, "wake"))
        t += step
    return actions, t


# ─── Phase 1: repeated sleep/wake cycles ──────────────────────────

def test_five_sleep_wake_cycles_stable(caplog):
    """5 cycles: exact connects, generations, exits, states, no leaks."""
    agent = make_agent()
    sessions = [FakeSession(hang=True) for _ in range(6)]
    count = wire_connect(agent, sessions)
    audio = agent.audio  # run_loop releases agent.audio in finally; keep ref

    actions, end = _sleep_wake_actions(5)
    resolved = [(t, (agent.request_sleep if k == "sleep" else agent.request_wake))
                for t, k in actions]

    with caplog.at_level(logging.DEBUG, logger="cat_talker.agent"):
        _drive(agent, end + 0.4, resolved)

    # Exactly one fresh session per wake, no reconnect while sleeping.
    assert count[0] == 6, f"expected 6 connects, got {count[0]}"
    assert agent._session_generation == 6
    for i, s in enumerate(sessions):
        assert s.exited, f"session {i} was never closed"
    # No stale backlog, mic resumed, suppression clear.
    assert audio.audio_in_queue.empty()
    assert audio.input_paused is False
    assert audio.is_playing is False
    assert not agent.sleep.is_sleeping()
    # Lifecycle answers come from the logs, not object counting.
    created = [r.getMessage() for r in caplog.records
               if "session workers created" in r.getMessage()]
    assert len(created) == 6, created
    gens = sorted(int(m.split("generation=")[1].split()[0].rstrip(","))
                  for m in created)
    assert gens == [1, 2, 3, 4, 5, 6], gens
    assert not [t for t in agent._session_tasks if not t.done()], \
        "a session worker task survived teardown"


def test_old_sessions_cannot_send_after_cycles():
    """Only the live generation's mic audio reaches its session."""
    agent = make_agent()
    sessions = [FakeSession(hang=True) for _ in range(3)]
    count = wire_connect(agent, sessions)
    audio = agent.audio

    actions, end = _sleep_wake_actions(2)
    resolved = [(t, (agent.request_sleep if k == "sleep" else agent.request_wake))
                for t, k in actions]
    resolved.append((end + 0.1,
                     lambda: audio.audio_in_queue.put_nowait(b"live-now")))
    _drive(agent, end + 0.4, resolved)

    assert count[0] == 3
    assert sessions[0].sent_audio == []
    assert sessions[1].sent_audio == []
    assert sessions[2].sent_audio == [b"live-now"]
    assert audio.audio_in_queue.empty()


def test_sleep_window_audio_never_delivered():
    """Chunks arriving with no live session are flushed, never replayed."""
    agent = make_agent()
    first = FakeSession(hang=True)
    second = FakeSession(hang=True)
    count = wire_connect(agent, [first, second])
    audio = agent.audio
    _drive(agent, 1.0, [
        (0.2, agent.request_sleep),
        (0.3, lambda: audio.audio_in_queue.put_nowait(b"while-asleep")),
        (0.45, agent.request_wake),
        (0.65, lambda: audio.audio_in_queue.put_nowait(b"after-wake")),
    ])
    assert count[0] == 2
    assert first.sent_audio == []
    assert second.sent_audio == [b"after-wake"]


# ─── Phase 2: echo reference staging bound ────────────────────────

def test_echo_reference_staging_bounded_without_consumption():
    """Monitor keeps feeding during sleep; staging must not grow forever."""
    from cat_talker.echo_suppress import TRIM_AT, EchoSuppressor
    echo = EchoSuppressor()
    for _ in range(3000):
        echo.feed_reference(b"\x01\x02" * 1024)
    assert len(echo._pending) <= TRIM_AT + 2048, \
        f"staging grew unbounded: {len(echo._pending)}"


# ─── Phase 5: tiny-transcript guard ───────────────────────────────

def _guard_cases():
    return [
        # (text, actionable?)
        ("sleep", True),
        ("go to sleep now", True),
        ("wake up", True),
        ("yes", True),
        ("no", True),
        ("hello chibi", True),
        ("what time is it", True),
        ("जी जी", True),
        ("Patna ka mausam", True),
        ("X", False),
        ("A", False),
        ("...", False),
        ("¿?", False),
        ("", False),
        ("   ", False),
        ("好啊。", False),
        ("अ", False),  # single Devanagari letter: no command content
    ]


@pytest.mark.parametrize("text,expected", _guard_cases())
def test_is_actionable_transcript(text, expected):
    from cat_talker.agent import is_actionable_transcript
    assert is_actionable_transcript(text) is expected


def test_garbage_transcript_reaches_no_command(caplog):
    """Foreign-script noise: logged, but no turn, no parse, no dedup reset."""
    agent = make_agent()
    before_id = agent._interaction_id
    heard = []
    session = FakeSession(
        interactions=[
            [pytypes.SimpleNamespace(
                server_content=pytypes.SimpleNamespace(
                    interrupted=False, turn_complete=False, model_turn=None,
                    input_transcription=pytypes.SimpleNamespace(text="好啊。"),
                    interim_input_transcription=None),
                client_content=None, tool_call=None)],
            "hang",
        ])
    count = wire_connect(agent, session)

    async def go():
        import asyncio as _aio
        loop = _aio.get_running_loop()
        loop.call_later(0.5, agent.stop_event.set)
        real_sleep = _aio.sleep

        async def fast_sleep(delay, *a, **k):
            await real_sleep(min(delay, 0.02), *a, **k)

        with patch.object(_aio, "sleep", fast_sleep):
            await asyncio.wait_for(
                agent.run_loop(
                    text_callback=lambda r, t: heard.append((r, t))),
                timeout=10.5)

    with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
        asyncio.run(go())
    assert count[0] == 1
    assert [h for h in heard if h[0] == "user"] == [], heard
    assert agent._interaction_id == before_id
    assert not agent.sleep.is_sleeping()
    assert any("transcript" in r.getMessage().lower()
               for r in caplog.records)


def test_short_legit_commands_still_work():
    """'no'/'yes' length commands stay actionable at unit level."""
    from cat_talker.agent import is_actionable_transcript
    assert is_actionable_transcript("no") is True
    assert is_actionable_transcript("YES") is True


# ─── Phase 6: read-only MPRIS watcher ─────────────────────────────

def _playerctl_playing(monkeypatch, lines=("brave | Playing | video",)):
    import shutil
    import subprocess as _sp

    def _fake_run(argv, **kwargs):
        assert isinstance(argv, list) and "shell" not in kwargs
        assert argv[:2] == ["playerctl", "status"]
        assert kwargs.get("timeout") is not None

        class _R:
            returncode = 0
            stdout = "\n".join(lines) + "\n"
        return _R()

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/playerctl")
    monkeypatch.setattr(_sp, "run", _fake_run)


def test_media_playing_edge_sleeps_once(monkeypatch):
    from cat_talker import media_watcher as mw
    agent = make_agent()
    _playerctl_playing(monkeypatch)
    calls = []
    orig = agent.request_sleep
    agent.request_sleep = lambda: calls.append(1) or orig()
    w = mw.MediaWatcher()
    assert w.poll_once(agent) is True   # rising edge -> sleep
    assert w.poll_once(agent) is False  # still playing -> no repeat
    assert w.poll_once(agent) is False
    assert calls == [1]


@pytest.mark.parametrize("lines,expected", [
    (("Playing",), True),
    (("Paused",), False),
    (("Playing", "Paused"), True),
    (("Stopped",), False),
    ((), False),
    (("brave | Playing | title",), True),
])
def test_query_media_playing_formats(monkeypatch, lines, expected):
    _playerctl_playing(monkeypatch, lines=lines)
    from cat_talker import media_watcher as mw
    assert mw.query_media_playing() is expected


def test_media_stopped_never_wakes_and_stays_asleep(monkeypatch):
    from cat_talker import media_watcher as mw
    agent = make_agent()
    assert agent.request_sleep() == "sleeping"
    _playerctl_playing(monkeypatch, lines=("brave | Paused | video",))
    w = mw.MediaWatcher()
    assert w.poll_once(agent) is False
    assert agent.sleep.is_sleeping(), "stopped media must not wake"
    assert agent.request_wake() == "awake"  # F2 still works


def test_media_query_failure_is_harmless(monkeypatch):
    import shutil
    import subprocess as _sp
    from cat_talker import media_watcher as mw
    agent = make_agent()
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert mw.query_media_playing() is None
    assert mw.MediaWatcher().poll_once(agent) is False
    assert not agent.sleep.is_sleeping()

    def _boom(*a, **k):
        raise OSError("playerctl exploded")

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/playerctl")
    monkeypatch.setattr(_sp, "run", _boom)
    assert mw.query_media_playing() is None
    assert mw.MediaWatcher().poll_once(agent) is False
    assert not agent.sleep.is_sleeping()


def test_media_watcher_in_run_loop_single_instance(caplog, monkeypatch):
    """Playing throughout: one auto-sleep, no reconnect storm, one watcher."""
    monkeypatch.setenv("CAT_TALKER_MEDIA_WATCH", "1")
    from cat_talker import media_watcher as mw
    agent = make_agent()
    first = FakeSession(hang=True)
    second = FakeSession(hang=True)
    count = wire_connect(agent, [first, second])
    polls = []

    def _always_playing():
        polls.append(1)
        return True

    async def go():
        import asyncio as _aio
        loop = _aio.get_running_loop()
        loop.call_later(0.6, agent.stop_event.set)
        real_sleep = _aio.sleep

        async def fast_sleep(delay, *a, **k):
            await real_sleep(min(delay, 0.02), *a, **k)

        with patch.object(_aio, "sleep", fast_sleep), \
             patch.object(mw, "query_media_playing",
                          side_effect=lambda: _always_playing()):
            await asyncio.wait_for(agent.run_loop(), timeout=10.6)

    with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
        asyncio.run(go())
    assert agent.sleep.is_sleeping(), "playing media must auto-sleep"
    assert first.exited, "auto-sleep must close the session"
    assert count[0] == 1, "sleep must not reconnect"
    assert not [t for t in agent._session_tasks if not t.done()]
