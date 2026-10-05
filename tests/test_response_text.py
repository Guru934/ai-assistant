"""Focused tests for AUDIO + TEXT assistant responses.

TEXT deltas assemble incrementally (ResponseTextAccumulator), stream to
the UI bubble via the model_delta role (no history write), and flush once
as role "model" (history + UI) on turn completion. Audio is untouched,
user transcripts stay distinct, and malformed text never invents words.
Real run_loop with fake sessions; Qt offscreen for the bridge test.
"""

import asyncio
import logging
import os
import sys
import types as pytypes
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cat_talker.agent import ResponseTextAccumulator
from test_session_lifecycle import (
    FakeSession,
    make_agent,
    run_with_stop,
    wire_connect,
    _turn_complete_msg,
)


def _text_msg(text, audio_data=None):
    parts = []
    if audio_data is not None:
        parts.append(pytypes.SimpleNamespace(
            inline_data=pytypes.SimpleNamespace(data=audio_data),
            text=None))
    parts.append(pytypes.SimpleNamespace(inline_data=None, text=text))
    return pytypes.SimpleNamespace(
        server_content=pytypes.SimpleNamespace(
            interrupted=False, turn_complete=False,
            model_turn=pytypes.SimpleNamespace(parts=parts)),
        client_content=None, tool_call=None)


def _run_text_session(agent, messages, heard):
    from test_session_lifecycle import _fast_sleep
    session = FakeSession(interactions=[messages, "hang"])
    wire_connect(agent, session)

    async def go():
        loop = asyncio.get_running_loop()
        loop.call_later(0.5, agent.stop_event.set)
        with patch.object(asyncio, "sleep", _fast_sleep):
            await asyncio.wait_for(
                agent.run_loop(
                    text_callback=lambda role, text: heard.append((role, text))),
                timeout=10.5)
    asyncio.run(go())


# ---------------------------------------------------------------------------
# Accumulator unit tests: ordering, overlap, duplicates
# ---------------------------------------------------------------------------

def test_single_delta():
    acc = ResponseTextAccumulator()
    assert acc.add("Hello there.") == "Hello there."
    assert acc.complete() == "Hello there."
    assert acc.complete() == ""


def test_multiple_deltas_join_in_order():
    acc = ResponseTextAccumulator()
    assert acc.add("Hello ") == "Hello "
    assert acc.add("there, ") == "there, "
    assert acc.add("how are you?") == "how are you?"
    assert acc.complete() == "Hello there, how are you?"


def test_exact_duplicate_ignored():
    acc = ResponseTextAccumulator()
    assert acc.add("Hi.") == "Hi."
    assert acc.add("Hi.") == ""
    assert acc.complete() == "Hi."


def test_cumulative_resend_returns_only_new_tail():
    acc = ResponseTextAccumulator()
    assert acc.add("Hello") == "Hello"
    assert acc.add("Hello there") == " there"
    assert acc.complete() == "Hello there"


def test_overlap_join():
    acc = ResponseTextAccumulator()
    assert acc.add("Hello the") == "Hello the"
    assert acc.add("there friend") == "re friend"
    assert acc.complete() == "Hello there friend"


def test_empty_and_nonstring_ignored():
    acc = ResponseTextAccumulator()
    assert acc.add("") == ""
    assert acc.add(None) == ""
    assert acc.add(123) == ""
    assert acc.complete() == ""


# ---------------------------------------------------------------------------
# run_loop integration: streaming, flush, audio coexistence
# ---------------------------------------------------------------------------

def test_one_complete_text_response():
    agent = make_agent()
    heard = []
    _run_text_session(agent, [_text_msg("Namaste!"), _turn_complete_msg()],
                      heard)
    models = [t for r, t in heard if r == "model"]
    assert models == ["Namaste!"], heard
    deltas = [t for r, t in heard if r == "model_delta"]
    assert deltas == ["Namaste!"], deltas


def test_deltas_stream_then_flush_once():
    agent = make_agent()
    heard = []
    _run_text_session(agent, [_text_msg("Hello "),
                              _text_msg("there, friend"),
                              _turn_complete_msg()], heard)
    models = [t for r, t in heard if r == "model"]
    assert models == ["Hello there, friend"], heard
    # Streaming never rewrites history: exactly one model entry.
    assert len(models) == 1


def test_duplicate_parts_do_not_duplicate_stream():
    agent = make_agent()
    heard = []
    _run_text_session(agent, [_text_msg("Hi."),
                              _text_msg("Hi."),
                              _turn_complete_msg()], heard)
    models = [t for r, t in heard if r == "model"]
    assert models == ["Hi."], heard
    deltas = [t for r, t in heard if r == "model_delta"]
    assert deltas == ["Hi."], deltas


def test_text_and_audio_in_same_response():
    agent = make_agent()
    audio = agent.audio
    heard = []
    _run_text_session(agent, [_text_msg("Sure thing.", audio_data=b"\x01\x02"),
                              _turn_complete_msg()], heard)
    assert audio.output == [b"\x01\x02"], "audio chunk must still play"
    models = [t for r, t in heard if r == "model"]
    assert models == ["Sure thing."], heard


def test_two_responses_do_not_concatenate():
    agent = make_agent()
    heard = []
    _run_text_session(agent, [_text_msg("First."),
                              _turn_complete_msg(),
                              _text_msg("Second."),
                              _turn_complete_msg()], heard)
    models = [t for r, t in heard if r == "model"]
    assert models == ["First.", "Second."], heard


def test_malformed_text_event_keeps_audio(caplog):
    agent = make_agent()
    audio = agent.audio
    heard = []
    bad = pytypes.SimpleNamespace(
        server_content=pytypes.SimpleNamespace(
            interrupted=False, turn_complete=False,
            model_turn=pytypes.SimpleNamespace(parts=[
                pytypes.SimpleNamespace(inline_data=None, text=12345),
                pytypes.SimpleNamespace(
                    inline_data=pytypes.SimpleNamespace(data=b"\x09"),
                    text=None),
            ])),
        client_content=None, tool_call=None)
    with caplog.at_level(logging.WARNING, logger="cat_talker.agent"):
        _run_text_session(agent, [bad, _turn_complete_msg()], heard)
    assert audio.output == [b"\x09"], "audio must survive malformed text"
    assert [t for r, t in heard if r == "model"] == [], heard
    assert any("non-string" in r.message for r in caplog.records), \
        "malformed text must be logged"


def test_user_transcript_stays_distinct():
    from test_session_lifecycle import _input_transcription_msg
    agent = make_agent()
    heard = []
    _run_text_session(agent, [_input_transcription_msg("open youtube"),
                              _text_msg("Opening YouTube."),
                              _turn_complete_msg()], heard)
    users = [t for r, t in heard if r == "user"]
    models = [t for r, t in heard if r == "model"]
    assert users == ["open youtube"], heard
    assert models == ["Opening YouTube."], heard


def test_coding_report_text_flows_through_model_role():
    report = ("Worker completed; verification was not available.\n"
              "Workspace: /tmp/x\nVerification: worker_reported_pass")
    agent = make_agent()
    heard = []
    _run_text_session(agent, [_text_msg(report), _turn_complete_msg()],
                      heard)
    models = [t for r, t in heard if r == "model"]
    assert models == [report], heard
    assert "Workspace: /tmp/x" in models[0]
    assert "Verification: worker_reported_pass" in models[0]


def test_sleep_lifecycle_unchanged_by_text():
    agent = make_agent()
    assert agent.sleep.is_sleeping() is False
    heard = []
    _run_text_session(agent, [_text_msg("Hi."), _turn_complete_msg()],
                      heard)
    assert agent.sleep.is_sleeping() is False
    assert agent.request_toggle() == "sleeping"
    assert agent.request_toggle() == "awake"


# ---------------------------------------------------------------------------
# UI bridge: model_delta shows in bubble, never in history
# ---------------------------------------------------------------------------

def test_ui_receives_text_through_bridge(tmp_path, monkeypatch):
    """model_delta updates the bubble only; model writes history."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    from cat_talker.main import RadialVisualizerWindow
    window = RadialVisualizerWindow.__new__(RadialVisualizerWindow)
    seen_bubbles = []

    def _capture(text: str):
        seen_bubbles.append(text)

    window._on_bubble = _capture
    history = tmp_path / "hist.txt"
    monkeypatch.setattr("cat_talker.main.os.path.expanduser",
                        lambda p: str(history))
    # _on_text needs no QWidget init for the history path; init the
    # bubble attribute slot manually.
    window._on_text("model_delta", "streaming...")
    assert seen_bubbles == ["streaming..."]
    assert not history.exists(), "deltas must not touch history"
