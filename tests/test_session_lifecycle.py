"""Unit tests for Gemini Live session lifecycle in cat_talker.agent.

These tests drive the REAL GeminiDesktopAgent.run_loop() against a fake
Live session that accurately models the SDK: connect() returns an async
context manager (NOT an awaitable session).

No real Gemini connection, no audio hardware, and -- critically -- no real
desktop actions: ALL_TOOLS and the media-ducking helpers are replaced with
fakes, and the real tool functions are guarded so the test fails loudly if
production ever calls them.
"""

import asyncio
import logging
import os
import sys
import types as pytypes
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cat_talker.agent as agent_mod
from cat_talker.agent import GeminiDesktopAgent


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeAudio:
    """Stands in for AudioInterface: no hardware, just an input queue."""

    def __init__(self):
        self.audio_in_queue = asyncio.Queue()
        # Drain-checked by the mic-release path; queue_output records to
        # .output (playback is instant in tests, so this stays drained).
        self.audio_out_queue = asyncio.Queue()
        self.volume_cb = None
        self.output = []
        self.is_playing = False
        self.suppress_calls = 0
        self.release_calls = 0

    def queue_output(self, data):
        self.output.append(data)

    def clear_output_queue(self):
        pass

    def suppress_mic(self):
        self.suppress_calls += 1
        self.is_playing = True

    def release_mic(self):
        self.release_calls += 1
        self.is_playing = False

    def close(self):
        pass


class FakeSession:
    """Models one SDK Live session.

    Real SDK semantics: EACH session.receive() call returns an async
    iterator for ONE interaction, which ends normally when that turn is
    done. The session itself stays alive; production must call receive()
    again for the next turn.

    interactions=[...] scripts this per-call behavior: each entry is one
    receive() call and may be a message list (one interaction, then ends),
    the string "hang" (hold open until stop, for clean shutdown), or an
    exception instance (a genuine connection failure). Calls past the end
    of the script reuse the last entry. receive_calls counts the calls.

    The legacy messages=/exc=/hang= form is one single interaction.
    """

    def __init__(self, messages=(), exc=None, hang=False, pre_exc_delay=0.0,
                 interactions=None):
        self._messages = list(messages)
        self._exc = exc
        self._hang = hang
        self._pre_exc_delay = pre_exc_delay
        self.interactions = interactions
        self.receive_calls = 0
        self._stop = None  # wired to agent.stop_event by wire_connect
        self.sent_audio = []
        self.sent_video = []
        self.sent_texts = []
        self.tool_responses = []
        self.client_contents = []
        self.exited = False  # set by FakeLiveConnection.__aexit__

    def receive(self):
        return self._stream()

    async def _stream(self):
        n = self.receive_calls
        self.receive_calls += 1
        if self.interactions is not None:
            item = self.interactions[min(n, len(self.interactions) - 1)]
            if isinstance(item, BaseException):
                raise item
            if item == "hang":
                if self._stop is not None:
                    await self._stop.wait()
                else:  # pragma: no cover - safety net, never blocks a test
                    await asyncio.Event().wait()
                return
            for m in item:
                yield m
            return
        for m in self._messages:
            yield m
        if self._pre_exc_delay:
            await asyncio.sleep(self._pre_exc_delay)
        if self._exc is not None:
            raise self._exc
        if self._hang:
            if self._stop is not None:
                await self._stop.wait()
            else:  # pragma: no cover - safety net, never blocks a test
                await asyncio.Event().wait()

    async def send_realtime_input(self, audio=None, video=None, **kwargs):
        if audio is not None:
            self.sent_audio.append(getattr(audio, "data", audio))
        if video is not None:
            self.sent_video.append(getattr(video, "data", video))

    async def send_tool_response(self, function_responses=None):
        self.tool_responses.extend(function_responses or [])

    async def send(self, *args, **kwargs):
        self.sent_texts.append((args, kwargs))

    async def send_client_content(self, *args, **kwargs):
        self.client_contents.append((args, kwargs))


class FakeLiveConnection:
    """Models the SDK return value: connect() -> async context manager."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info):
        self._session.exited = True
        return False


def _tool_msg(name, args, call_id="t1"):
    return pytypes.SimpleNamespace(
        server_content=None,
        client_content=None,
        tool_call=pytypes.SimpleNamespace(
            function_calls=[
                pytypes.SimpleNamespace(id=call_id, name=name, args=args)
            ]
        ),
    )


def _turn_complete_msg():
    return pytypes.SimpleNamespace(
        server_content=pytypes.SimpleNamespace(
            interrupted=False, turn_complete=True, model_turn=None
        ),
        client_content=None,
        tool_call=None,
    )


def _input_transcription_msg(text):
    return pytypes.SimpleNamespace(
        server_content=pytypes.SimpleNamespace(
            interrupted=False,
            turn_complete=False,
            model_turn=None,
            input_transcription=pytypes.SimpleNamespace(text=text),
        ),
        client_content=None,
        tool_call=None,
    )


def _model_audio_msg(data):
    return pytypes.SimpleNamespace(
        server_content=pytypes.SimpleNamespace(
            interrupted=False,
            turn_complete=False,
            model_turn=pytypes.SimpleNamespace(parts=[
                pytypes.SimpleNamespace(
                    inline_data=pytypes.SimpleNamespace(data=data),
                    text=None,
                )
            ]),
        ),
        client_content=None,
        tool_call=None,
    )


def _tool_msgs(calls):
    """One message carrying several function calls: [(name, args, id)]."""
    return pytypes.SimpleNamespace(
        server_content=None,
        client_content=None,
        tool_call=pytypes.SimpleNamespace(
            function_calls=[
                pytypes.SimpleNamespace(id=cid, name=name, args=args)
                for name, args, cid in calls
            ]
        ),
    )


def make_agent():
    """Build an agent with fake audio/vision/client - no hardware touched."""
    with patch.object(agent_mod, "AudioInterface", FakeAudio), \
         patch.object(agent_mod, "VisionInterface", MagicMock), \
         patch.object(agent_mod.genai, "Client"), \
         patch.object(agent_mod, "play_earcon", MagicMock()):
        agent = GeminiDesktopAgent()
    agent.audio = FakeAudio()
    agent.vision = MagicMock()
    return agent


def wire_connect(agent, session_or_sessions):
    """Patch agent.client.aio.live.connect to hand out FakeLiveConnections.

    Like the real SDK, the patched connect is a PLAIN function returning an
    async context manager (it must NOT be awaited by production code).
    Returns a call counter so tests can assert exact connection counts.
    """
    sessions = (
        session_or_sessions
        if isinstance(session_or_sessions, list)
        else [session_or_sessions]
    )
    for s in sessions:
        s._stop = agent.stop_event
    count = [0]

    def fake_connect(model=None, config=None):
        idx = min(count[0], len(sessions) - 1)
        count[0] += 1
        return FakeLiveConnection(sessions[idx])

    agent.client.aio.live.connect = fake_connect
    return count


_real_sleep = asyncio.sleep


async def _fast_sleep(delay, *args, **kwargs):
    """Keep reconnect backoff from slowing tests; real timing untouched."""
    await _real_sleep(min(delay, 0.02), *args, **kwargs)


def run_with_stop(agent, delay=0.4):
    """Run run_loop until stop_event fires (hanging sessions then end)."""
    async def go():
        loop = asyncio.get_running_loop()
        loop.call_later(delay, agent.stop_event.set)
        await asyncio.wait_for(agent.run_loop(), timeout=delay + 10.0)

    with patch.object(asyncio, "sleep", _fast_sleep):
        asyncio.run(go())


# ---------------------------------------------------------------------------
# Safety: never execute real desktop actions, never beep, never duck audio
# ---------------------------------------------------------------------------

@pytest.fixture
def tool_calls():
    """Replace ALL_TOOLS with fakes; fail loudly if a real tool is reached."""
    calls = []

    # NOTE: names must match the real tool functions exactly, because
    # production builds its dispatch map from func.__name__.
    def open_website(url=""):
        calls.append(("open_website", url))
        return "FAKE opened %s" % url

    def search_and_play_youtube(query=""):
        calls.append(("search_and_play_youtube", query))
        return "FAKE yt %s" % query

    def set_volume(level_percent=50):
        calls.append(("set_volume", level_percent))
        return "FAKE volume %s" % level_percent

    fakes = [open_website, search_and_play_youtube, set_volume]
    with patch("cat_talker.tools.ALL_TOOLS", fakes), \
         patch("cat_talker.tools.open_website",
               side_effect=AssertionError("real open_website executed!")), \
         patch("cat_talker.tools.search_and_play_youtube",
               side_effect=AssertionError("real search_and_play_youtube executed!")), \
         patch("cat_talker.tools.set_volume",
               side_effect=AssertionError("real set_volume executed!")), \
         patch("cat_talker.tools.start_media_ducking"), \
         patch("cat_talker.tools.stop_media_ducking"), \
         patch("cat_talker.tools.send_notification"):
        yield calls


@pytest.fixture(autouse=True)
def _no_real_side_effects():
    """Tests without tool calls must still not beep or touch audio."""
    with patch("cat_talker.tools.start_media_ducking"), \
         patch("cat_talker.tools.stop_media_ducking"), \
         patch("cat_talker.tools.send_notification"), \
         patch.object(agent_mod, "play_earcon", MagicMock()):
        yield


# ---------------------------------------------------------------------------
# A/B: worker failure is visible; normal exit is distinguishable
# ---------------------------------------------------------------------------

def test_receive_worker_failure_visible_to_supervisor(caplog, tool_calls):
    """(A) A receive worker exception must be logged, never swallowed, and
    the supervisor must reconnect by leaving the old session context."""
    agent = make_agent()
    bad = FakeSession(
        messages=[_turn_complete_msg()],
        exc=RuntimeError("boom - connection exploded"),
    )
    good = FakeSession(hang=True)
    count = wire_connect(agent, [bad, good])
    with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
        run_with_stop(agent)
    assert count[0] == 2, "supervisor did not reconnect after worker failure"
    assert any("receive_worker failed" in r.message for r in caplog.records), \
        "receive worker did not log its failure"
    assert any("Worker task failed" in r.message for r in caplog.records), \
        "supervisor swallowed the worker exception"
    assert bad.exited, "old session context was not exited before reconnect"


def test_normal_receive_end_does_not_reconnect(caplog):
    """(B) Normal exhaustion of ONE receive() iterator ends the turn, not the
    session: production must call receive() again with ZERO reconnects."""
    agent = make_agent()
    session = FakeSession(
        interactions=[[_turn_complete_msg()], "hang"]
    )
    count = wire_connect(agent, session)
    with caplog.at_level(logging.DEBUG, logger="cat_talker.agent"):
        run_with_stop(agent)
    assert count[0] == 1, "normal end of receive() caused a reconnect!"
    assert session.receive_calls == 2, \
        "receive() was not invoked again after the interaction ended"
    assert any("Live interaction complete" in r.message
               for r in caplog.records), "turn completion was not logged"
    assert not any("Worker task failed" in r.message for r in caplog.records), \
        "normal turn end was misreported as failure"


def test_one_session_handles_multiple_interactions(caplog):
    """(A) One session serves several sequential turns: every ended iterator
    is followed by another receive() call on the SAME session."""
    agent = make_agent()
    heard = []
    session = FakeSession(
        interactions=[
            [_input_transcription_msg("first turn")],
            [_input_transcription_msg("second turn")],
            "hang",
        ]
    )
    count = wire_connect(agent, session)

    async def go():
        loop = asyncio.get_running_loop()
        loop.call_later(0.5, agent.stop_event.set)
        with patch.object(asyncio, "sleep", _fast_sleep):
            await asyncio.wait_for(
                agent.run_loop(
                    text_callback=lambda role, text: heard.append((role, text))),
                timeout=10.5,
            )

    with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
        asyncio.run(go())
    assert count[0] == 1, "second turn reconnected instead of reusing the session"
    assert session.receive_calls == 3, \
        "expected 2 turns + held stream, got %d receive() calls" % session.receive_calls
    assert ("user", "first turn") in heard, heard
    assert ("user", "second turn") in heard, heard


def test_mic_worker_exit_logged(caplog):
    """Teardown cancels the sibling workers; mic_worker must log its exit."""
    agent = make_agent()
    first = FakeSession(exc=RuntimeError("drop"))  # failure -> teardown
    second = FakeSession(hang=True)
    wire_connect(agent, [first, second])
    with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
        run_with_stop(agent)
    assert any("mic_worker exited" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# C: CancelledError propagates (never converted into a reconnect)
# ---------------------------------------------------------------------------

def test_cancelled_error_propagates():
    """(C) If a worker ends via CancelledError, run_loop must re-raise it
    instead of silently reconnecting, and session state must be cleared."""
    agent = make_agent()
    session = FakeSession(exc=asyncio.CancelledError("stop"))
    count = wire_connect(agent, session)

    async def go():
        with patch.object(asyncio, "sleep", _fast_sleep):
            await agent.run_loop()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(go())
    assert count[0] == 1, "cancelled session must not reconnect"
    assert not agent._session_active.is_set(), "session state leaked"


# ---------------------------------------------------------------------------
# D: session-active state is symmetrical
# ---------------------------------------------------------------------------

def test_session_active_state_cleared_on_teardown():
    """(D) _session_active is set only while a valid session exists and is
    cleared during teardown - never left set after disconnect."""
    agent = make_agent()
    bad = FakeSession(exc=RuntimeError("drop"))
    good = FakeSession(hang=True)
    wire_connect(agent, [bad, good])

    clears = []
    orig_clear = agent._session_active.clear

    def spy_clear():
        clears.append(agent._session_generation)
        return orig_clear()

    agent._session_active.clear = spy_clear
    try:
        run_with_stop(agent)
    finally:
        agent._session_active.clear = orig_clear

    assert clears, "_session_active was never cleared during teardown"
    assert not agent._session_active.is_set(), "session-active leaked past run"
    assert bad.exited, "failed session context was not exited"
    assert agent._session_generation == 2, "expected exactly two sessions"


# ---------------------------------------------------------------------------
# E/F: stale microphone input
# ---------------------------------------------------------------------------

def test_stale_mic_input_flushed_on_reconnect(caplog):
    """(E) Audio queued for a dead session must be flushed on reconnect."""
    agent = make_agent()
    audio = agent.audio  # run_loop's finally releases agent.audio; keep a ref
    for i in range(3):
        audio.audio_in_queue.put_nowait(b"stale-%d" % i)
    bad = FakeSession(exc=RuntimeError("drop"))
    good = FakeSession(hang=True)
    wire_connect(agent, [bad, good])
    with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
        run_with_stop(agent)
    assert audio.audio_in_queue.empty(), "stale mic input was not flushed"
    assert any("Flushed" in r.message for r in caplog.records), \
        "flush was not logged (no flush happened?)"


def test_stale_input_never_replayed_into_next_session(tool_calls):
    """(F) End-to-end through run_loop: stale chunks present when the first
    session drops must reach NEITHER the old nor the new session.

    The mic is left live (not blocked) so this test FAILS if the flush or
    the session-generation gate regresses: without them the mic forwards
    the stale chunks into the first session during the failure window.
    """
    agent = make_agent()
    for i in range(3):
        agent.audio.audio_in_queue.put_nowait(b"stale-%d" % i)
    bad = FakeSession(
        messages=[_turn_complete_msg()],
        exc=RuntimeError("drop"),
        pre_exc_delay=0.2,  # window for a regressed mic to leak stale audio
    )
    good = FakeSession(hang=True)
    wire_connect(agent, [bad, good])
    run_with_stop(agent, delay=0.6)
    assert bad.sent_audio == [], \
        "stale audio leaked into the dying session: %r" % (bad.sent_audio,)
    assert good.sent_audio == [], \
        "stale audio was replayed into the new session: %r" % (good.sent_audio,)


def test_clear_input_queue_drains_stale_chunks():
    agent = make_agent()
    for i in range(5):
        agent.audio.audio_in_queue.put_nowait(b"chunk-%d" % i)
    assert agent.audio.audio_in_queue.qsize() == 5
    agent._clear_input_queue()
    assert agent.audio.audio_in_queue.qsize() == 0


# ---------------------------------------------------------------------------
# G: hardware reuse across reconnects
# ---------------------------------------------------------------------------

def test_reconnect_reuses_audio_interface():
    """(G) run_loop must construct AudioInterface exactly once; reconnects
    reuse the same hardware object instead of reopening it."""
    constructions = []

    class CountingFakeAudio(FakeAudio):
        def __init__(self):
            constructions.append(1)
            super().__init__()

    with patch.object(agent_mod, "AudioInterface", CountingFakeAudio), \
         patch.object(agent_mod, "VisionInterface", MagicMock), \
         patch.object(agent_mod.genai, "Client"), \
         patch.object(agent_mod, "play_earcon", MagicMock()):
        agent = GeminiDesktopAgent()
        assert agent.audio is None
        bad = FakeSession(exc=RuntimeError("drop"))
        good = FakeSession(hang=True)
        count = wire_connect(agent, [bad, good])
        run_with_stop(agent)
    assert count[0] == 2, "expected a reconnect to exercise reuse"
    assert len(constructions) == 1, \
        "AudioInterface was constructed %d times across a reconnect" % len(constructions)


# ---------------------------------------------------------------------------
# H/I: tool responses keep the SAME session alive
# ---------------------------------------------------------------------------

def test_tool_response_does_not_reconnect(tool_calls):
    """(H) A tool call (set_volume) plus the next turn must stay on the SAME
    Live session: no new connect, response sent on the same session."""
    agent = make_agent()
    session = FakeSession(
        messages=[
            _tool_msg("set_volume", {"level_percent": 50}),
            _turn_complete_msg(),
        ],
        hang=True,
    )
    count = wire_connect(agent, session)
    run_with_stop(agent)
    assert count[0] == 1, "tool response caused a reconnect!"
    assert len(session.tool_responses) == 1
    assert tool_calls == [("set_volume", 50)], \
        "expected the fake tool to run (real tools must never run)"


def test_two_sequential_turns_on_same_session(tool_calls):
    """(C) Two tool turns in SEPARATE receive() interactions must both be
    served by one session: tool -> response -> receive() again -> model
    continues. This is the exact flow the old code broke by reconnecting
    whenever an iterator ended."""
    agent = make_agent()
    session = FakeSession(
        interactions=[
            [_tool_msg("open_website", {"url": "example.com"}, call_id="t1")],
            [_tool_msg("set_volume", {"level_percent": 30}, call_id="t2"),
             _turn_complete_msg()],
            "hang",
        ]
    )
    count = wire_connect(agent, session)
    run_with_stop(agent)
    assert count[0] == 1, "second turn reconnected instead of reusing the session"
    assert session.receive_calls == 3, \
        "expected 2 turns + held stream, got %d receive() calls" % session.receive_calls
    assert len(session.tool_responses) == 2, "both turns must answer on one session"
    assert [c[0] for c in tool_calls] == ["open_website", "set_volume"]


# ---------------------------------------------------------------------------
# J: connection counting / K: no real desktop actions
# ---------------------------------------------------------------------------

def _connection_closed():
    from websockets.exceptions import ConnectionClosed
    from websockets.frames import Close
    return ConnectionClosed(Close(1006, "abnormal closure"), None)


def test_genuine_failure_still_reconnects(caplog):
    """(D) A genuine RuntimeError inside receive() still tears down and
    reconnects - only NORMAL exhaustion is now ignored."""
    agent = make_agent()
    bad = FakeSession(
        messages=[_turn_complete_msg()],
        exc=RuntimeError("drop"),
    )
    good = FakeSession(hang=True)
    count = wire_connect(agent, [bad, good])
    with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
        run_with_stop(agent)
    assert count[0] == 2, "genuine failure did not reconnect"
    assert bad.exited, "failed session context was not exited"
    assert good.exited, "held session context was not exited on shutdown"
    assert any("receive_worker failed" in r.message for r in caplog.records)


def test_connection_closed_reconnects():
    """(D) A real websockets ConnectionClosed from the server also still
    reconnects via the dedicated except-path."""
    agent = make_agent()
    bad = FakeSession(exc=_connection_closed())
    good = FakeSession(hang=True)
    count = wire_connect(agent, [bad, good])
    run_with_stop(agent)
    assert count[0] == 2, "ConnectionClosed did not reconnect"
    assert bad.exited and good.exited


def test_reconnect_connection_count_exact():
    """(J) One failing session followed by a held session == 2 connects."""
    agent = make_agent()
    bad = FakeSession(exc=RuntimeError("drop"))
    good = FakeSession(hang=True)
    count = wire_connect(agent, [bad, good])
    run_with_stop(agent)
    assert count[0] == 2, "expected exactly 2 connections, got %d" % count[0]
    assert bad.exited, "failed session context was not exited"
    assert good.exited, "held session context was not exited on shutdown"


def test_no_real_desktop_actions_execute(tool_calls):
    """(K) open_website / search_and_play_youtube / set_volume run as fakes.

    The real implementations are patched to raise AssertionError, so this
    fails loudly if production ever bypasses the mocked tool map - e.g. on
    a machine with wpctl/xdg-open installed where set_volume would change
    the real system volume.
    """
    agent = make_agent()
    session = FakeSession(
        messages=[
            _tool_msg("open_website", {"url": "example.com"}, call_id="t1"),
            _tool_msg("search_and_play_youtube", {"query": "cats"}, call_id="t2"),
            _tool_msg("set_volume", {"level_percent": 42}, call_id="t3"),
            _turn_complete_msg(),
        ],
        hang=True,
    )
    count = wire_connect(agent, session)
    run_with_stop(agent)
    assert count[0] == 1
    assert len(session.tool_responses) == 3
    assert tool_calls == [
        ("open_website", "example.com"),
        ("search_and_play_youtube", "cats"),
        ("set_volume", 42),
    ]


# ---------------------------------------------------------------------------
# Tool-call idempotency: one function_call.id executes at most once
# ---------------------------------------------------------------------------

def test_duplicate_tool_id_executes_once(caplog, tool_calls):
    """The same function_call.id arriving twice (e.g. the model re-sending a
    call it heard itself speak) must execute the side effect ONCE. The
    protocol is still honored: both calls get a FunctionResponse, one
    session, zero reconnects."""
    agent = make_agent()
    session = FakeSession(
        interactions=[
            [_tool_msg("set_volume", {"level_percent": 80}, call_id="dup-1")],
            [_tool_msg("set_volume", {"level_percent": 80}, call_id="dup-1")],
            "hang",
        ]
    )
    count = wire_connect(agent, session)
    with caplog.at_level(logging.DEBUG, logger="cat_talker.agent"):
        run_with_stop(agent)
    assert count[0] == 1, "duplicate tool call caused a reconnect!"
    assert tool_calls == [("set_volume", 80)], \
        "duplicate id executed the tool %d times!" % len(tool_calls)
    assert len(session.tool_responses) == 2, \
        "every call (even duplicates) needs a FunctionResponse"
    assert any("classification=NEW" in r.message and "dup-1" in r.message
               for r in caplog.records), "first execution was not logged"
    assert any("classification=DUPLICATE_ID" in r.message and "dup-1" in r.message
               for r in caplog.records), "duplicate was not logged"


def test_same_tool_different_ids_execute_independently(tool_calls):
    """Different IDs for the same tool are independent turns, not duplicates:
    set_volume to 80 then to 30 executes twice."""
    agent = make_agent()
    session = FakeSession(
        interactions=[
            [_tool_msg("set_volume", {"level_percent": 80}, call_id="vol-a")],
            [_tool_msg("set_volume", {"level_percent": 30}, call_id="vol-b")],
            "hang",
        ]
    )
    count = wire_connect(agent, session)
    run_with_stop(agent)
    assert count[0] == 1
    assert tool_calls == [("set_volume", 80), ("set_volume", 30)], tool_calls
    assert len(session.tool_responses) == 2


def test_same_signature_different_ids_executes_once(caplog, tool_calls):
    """The reported live bug: one utterance, three different IDs, same side
    effect, no new transcript between them. Only the first may execute; the
    rest are DUPLICATE_SIGNATURE but still answered per the protocol."""
    agent = make_agent()
    session = FakeSession(
        interactions=[
            [_tool_msg("open_website", {"url": "https://youtube.com"},
                       call_id="call-1")],
            [_tool_msg("open_website", {"url": "https://youtube.com"},
                       call_id="call-2")],
            [_tool_msg("open_website", {"url": "https://youtube.com"},
                       call_id="call-3")],
            "hang",
        ]
    )
    count = wire_connect(agent, session)
    with caplog.at_level(logging.DEBUG, logger="cat_talker.agent"):
        run_with_stop(agent)
    assert count[0] == 1, "semantic duplicate caused a reconnect!"
    assert tool_calls == [("open_website", "https://youtube.com")], \
        "same side effect executed %d times!" % len(tool_calls)
    assert len(session.tool_responses) == 3, \
        "every call (even duplicates) needs a FunctionResponse"
    assert any("classification=NEW" in r.message and "call-1" in r.message
               for r in caplog.records)
    dupes = [r for r in caplog.records
             if "classification=DUPLICATE_SIGNATURE" in r.message]
    assert len(dupes) == 2, "expected 2 signature-duplicate logs, got %d" % len(dupes)
    assert all("interaction=" in r.message and "sig=" in r.message for r in dupes)


def test_duplicate_aggregate_logged_once_per_interaction(caplog, tool_calls):
    """Duplicates log one DEBUG aggregate per interaction, not one WARNING
    each: three identical calls in a single message execute once and report
    a single 'suppressed 2' line."""
    agent = make_agent()
    session = FakeSession(
        interactions=[
            [_tool_msgs([
                ("set_volume", {"level_percent": 100}, "v-1"),
                ("set_volume", {"level_percent": 100}, "v-2"),
                ("set_volume", {"level_percent": 100}, "v-3"),
            ])],
            "hang",
        ]
    )
    count = wire_connect(agent, session)
    with caplog.at_level(logging.DEBUG, logger="cat_talker.agent"):
        run_with_stop(agent)
    assert count[0] == 1
    assert tool_calls == [("set_volume", 100)], tool_calls
    assert len(session.tool_responses) == 3
    suppressed = [r for r in caplog.records
                  if "Interaction suppressed" in r.message]
    assert len(suppressed) == 1, \
        "expected one aggregate line, got %d" % len(suppressed)
    assert "2 duplicate-signature" in suppressed[0].message


def test_mic_suppressed_during_output_released_after_turn(tool_calls):
    """Output-level suppression end to end: model audio output mutes the
    mic exactly once per turn (never per chunk); after turn completion the
    mic is released again. The session itself is untouched."""
    agent = make_agent()
    agent._mic_release_cooldown = 0.05  # shrink the cooldown for the test
    audio = agent.audio
    assert audio.is_playing is False, "mic must start enabled"
    session = FakeSession(
        interactions=[
            [_model_audio_msg(b"pcm-a"), _model_audio_msg(b"pcm-b"),
             _turn_complete_msg()],
            "hang",
        ]
    )
    count = wire_connect(agent, session)
    run_with_stop(agent, delay=0.7)
    assert count[0] == 1, "turn flow caused a reconnect!"
    assert session.receive_calls == 2
    assert audio.output == [b"pcm-a", b"pcm-b"], "model audio was not queued"
    assert audio.suppress_calls == 1, \
        "mic must be muted once per turn, not per chunk: %d" % audio.suppress_calls
    assert audio.release_calls >= 1, "mic never released after the turn"
    assert audio.is_playing is False, "mic stayed muted after the turn"


def test_new_interaction_permits_same_signature_again(tool_calls):
    """A new user turn (fresh input transcription) resets semantic dedup:
    the user saying it again really means it again."""
    agent = make_agent()
    session = FakeSession(
        interactions=[
            [_tool_msg("set_volume", {"level_percent": 30}, call_id="vol-1")],
            [_input_transcription_msg("a bit louder please")],
            [_tool_msg("set_volume", {"level_percent": 30}, call_id="vol-2")],
            "hang",
        ]
    )
    count = wire_connect(agent, session)
    run_with_stop(agent)
    assert count[0] == 1
    assert tool_calls == [("set_volume", 30), ("set_volume", 30)], tool_calls
    assert len(session.tool_responses) == 2


# ---------------------------------------------------------------------------
# Mic -> Gemini input path observability (no real hardware in any test)
# ---------------------------------------------------------------------------

def test_mic_chunks_consumed_and_sent(caplog):
    """Mic queue chunks must be consumed and forwarded via
    session.send_realtime_input(), with a periodic summary logged."""
    agent = make_agent()
    assert isinstance(agent.audio, FakeAudio), "test would touch real hardware!"
    chunks = [b"pcm-%03d" % i for i in range(55)]
    session = FakeSession(hang=True)
    count = wire_connect(agent, session)
    with caplog.at_level(logging.DEBUG, logger="cat_talker.agent"):
        async def go():
            loop = asyncio.get_running_loop()

            def feed():
                # Fresh input arriving DURING the live session (pre-filling
                # would be correctly flushed as stale by session start).
                for c in chunks:
                    agent.audio.audio_in_queue.put_nowait(c)

            loop.call_later(0.05, feed)
            loop.call_later(0.6, agent.stop_event.set)
            with patch.object(asyncio, "sleep", _fast_sleep):
                await asyncio.wait_for(agent.run_loop(), timeout=10.6)

        asyncio.run(go())
    assert count[0] == 1
    assert session.sent_audio == chunks, \
        "mic chunks were not all forwarded to the session"
    assert agent._mic_stats["received"] == 55, agent._mic_stats
    assert agent._mic_stats["sent"] == 55, agent._mic_stats
    assert any("mic_worker input: received=50" in r.message
               for r in caplog.records), \
        "expected a periodic mic summary log line"


def test_input_transcription_handled(caplog):
    """msg.server_content.input_transcription must be logged and displayed
    via text_callback without disturbing the session."""
    agent = make_agent()
    assert isinstance(agent.audio, FakeAudio), "test would touch real hardware!"
    heard = []
    session = FakeSession(
        messages=[_input_transcription_msg("hello chibi"),
                  _turn_complete_msg()],
        hang=True,
    )
    count = wire_connect(agent, session)
    with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
        async def go():
            loop = asyncio.get_running_loop()
            loop.call_later(0.4, agent.stop_event.set)
            with patch.object(asyncio, "sleep", _fast_sleep):
                await asyncio.wait_for(
                    agent.run_loop(text_callback=lambda role, text: heard.append((role, text))),
                    timeout=10.4,
                )
        asyncio.run(go())
    assert count[0] == 1, "transcription handling caused a reconnect!"
    assert ("user", "hello chibi") in heard, \
        "input transcript was not displayed: %r" % (heard,)
    assert any("User said: hello chibi" in r.message for r in caplog.records), \
        "input transcript was not logged"


def test_quiet_queue_diagnostic(caplog):
    """If the mic queue stays silent, mic_worker must warn instead of
    sitting mute forever."""
    agent = make_agent()
    assert isinstance(agent.audio, FakeAudio), "test would touch real hardware!"
    agent._mic_quiet_timeout = 0.2  # shrink the silence window for the test
    session = FakeSession(hang=True)
    wire_connect(agent, session)
    with caplog.at_level(logging.WARNING, logger="cat_talker.agent"):
        run_with_stop(agent, delay=0.7)
    assert any("no microphone input" in r.message for r in caplog.records), \
        "starved mic queue produced no diagnostic warning"
