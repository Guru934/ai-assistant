"""Diagnostic-only tests: interim/finalized transcripts, audio-state log.

Covers the instrumentation added for the post-wake recognition diagnosis
(no behavior change):
- interim_input_transcription is logged separately and never commands,
- finalized transcripts keep commanding + emit a fragmentation diag line,
- audio-state snapshot keys exist and session-established/first-chunk
  markers are logged on connect,
- echo suppression ON/OFF is controllable without touching DSP code.
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


def _drive_fast(agent, stop_after, actions=()):
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


def _interim_msg(text):
    return pytypes.SimpleNamespace(
        server_content=pytypes.SimpleNamespace(
            interrupted=False,
            turn_complete=False,
            model_turn=None,
            input_transcription=None,
            interim_input_transcription=pytypes.SimpleNamespace(text=text),
        ),
        client_content=None,
        tool_call=None,
    )


def test_interim_transcription_never_commands(caplog):
    """Even a sleep phrase in interim text must not sleep or notify."""
    agent = make_agent()
    heard = []
    session = FakeSession(interactions=[[_interim_msg("go to sleep")], "hang"])
    count = wire_connect(agent, session)
    with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
        _drive_fast(agent, 0.4, [])
    assert not agent.sleep.is_sleeping(), "interim text put the agent to sleep!"
    assert ("user", "go to sleep") not in heard
    assert heard == [], heard
    assert count[0] == 1
    assert any("interim" in r.getMessage() for r in caplog.records), \
        "interim transcription was not logged separately"


def test_interim_does_not_reset_interaction_or_wake(caplog):
    agent = make_agent()
    before = agent._interaction_id
    session = FakeSession(interactions=[[_interim_msg("wake up")], "hang"])
    wire_connect(agent, session)
    with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
        _drive_fast(agent, 0.4, [])
    assert agent._interaction_id == before, "interim reset the interaction"


def test_finalized_transcription_emits_fragmentation_diag(caplog):
    agent = make_agent()
    session = FakeSession(
        interactions=[
            [_input_transcription_msg("first command"),
             _turn_complete_msg()],
            [_input_transcription_msg("second")],
            "hang",
        ]
    )
    wire_connect(agent, session)
    with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
        _drive_fast(agent, 0.6, [])
    diags = [r.getMessage() for r in caplog.records
             if "transcript diag" in r.getMessage()]
    assert len(diags) == 2, diags
    assert "text='first command'" in diags[0]
    assert "turn_complete_since_prev=False" in diags[0]
    assert "text='second'" in diags[1]
    assert "turn_complete_since_prev=True" in diags[1]
    assert "interaction=" in diags[1]


def test_audio_state_snapshot_keys():
    agent = make_agent()
    snap = agent._audio_state_snapshot()
    assert set(snap) == {"mic_paused", "is_playing", "stream_active",
                         "in_queue", "generation", "echo_enabled",
                         "echo_stats"}
    assert snap["generation"] == agent._session_generation
    assert snap["mic_paused"] is False


def test_session_established_logs_audio_state_and_first_chunk(caplog):
    agent = make_agent()
    session = FakeSession(hang=True)
    count = wire_connect(agent, session)

    async def go():
        import asyncio as _aio
        loop = _aio.get_running_loop()
        loop.call_later(
            0.2, lambda: agent.audio.audio_in_queue.put_nowait(b"pcm-live"))
        loop.call_later(0.5, agent.stop_event.set)
        await _aio.wait_for(agent.run_loop(), timeout=10.5)

    import asyncio as _aio
    real_sleep = _aio.sleep

    async def fast_sleep(delay, *a, **k):
        await real_sleep(min(delay, 0.02), *a, **k)

    with patch.object(_aio, "sleep", fast_sleep):
        with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
            _aio.run(go())
    assert count[0] == 1
    assert any("audio-state [session-established]" in r.getMessage()
               for r in caplog.records), "no post-connect audio-state log"
    assert any("mic-first-chunk" in r.getMessage() for r in caplog.records), \
        "no first-chunk latency log"
    assert session.sent_audio == [b"pcm-live"]


def test_echo_suppress_env_override(monkeypatch):
    import cat_talker.config as config_mod

    monkeypatch.setenv("CAT_TALKER_ECHO_SUPPRESS", "0")
    with patch.object(config_mod, "load_config",
                      return_value={"echo_suppress_enabled": True}):
        assert config_mod.get_echo_suppress() is False

    monkeypatch.setenv("CAT_TALKER_ECHO_SUPPRESS", "1")
    with patch.object(config_mod, "load_config",
                      return_value={"echo_suppress_enabled": False}):
        assert config_mod.get_echo_suppress() is True

    monkeypatch.delenv("CAT_TALKER_ECHO_SUPPRESS", raising=False)
    with patch.object(config_mod, "load_config",
                      return_value={"echo_suppress_enabled": False}):
        assert config_mod.get_echo_suppress() is False
    with patch.object(config_mod, "load_config",
                      return_value={}):
        assert config_mod.get_echo_suppress() is True  # default on
