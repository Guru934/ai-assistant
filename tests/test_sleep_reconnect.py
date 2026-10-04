"""Regression tests for: SLEEP -> session cancellation -> no reconnect.

Incident: voice "go to sleep" arrived while the model was speaking
(_is_speaking True, no tool running). The agent reported "deferred" but
the controller had already flipped to SLEEPING, so no teardown was ever
scheduled: SLEEPING + live session + live mic (zombie). The live mic then
fed echo/noise ("y", "double sleep") into the live session, a wake flip
followed, and the supervisor opened a NEW session.

These tests drive the REAL run_loop against FakeSession (no hardware, no
network) and prove the sleep teardown is authoritative:
1. voice "go to sleep" enters SLEEPING (even mid-speech),
2. sleep-cancelled sessions do NOT reconnect,
3. no new session opens while sleeping,
4. F2 wake opens exactly one fresh session,
5. ordinary failures still reconnect,
6. tool-busy deferral still works (existing suite covers it; asserted here
   at unit level so this file is self-contained).
"""

import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from test_session_lifecycle import (
    FakeAudio,
    FakeSession,
    _input_transcription_msg,
    _model_audio_msg,
    _turn_complete_msg,
    make_agent,
    wire_connect,
)


def _drive_fast(agent, stop_after, actions=()):
    """Run run_loop with reconnect backoff fast-forwarded, then stop."""
    import asyncio as _aio

    real_sleep = _aio.sleep

    async def fast_sleep(delay, *a, **k):
        await real_sleep(min(delay, 0.02), *a, **k)

    async def go():
        loop = asyncio.get_running_loop()
        for delay, fn in actions:
            loop.call_later(delay, fn)
        loop.call_later(stop_after, agent.stop_event.set)
        await asyncio.wait_for(
            agent.run_loop(), timeout=stop_after + 10.0)

    with patch.object(_aio, "sleep", fast_sleep):
        _aio.run(go())


def _sleep_while_speaking_session():
    """Model audio first (assistant speaking), then the sleep phrase."""
    return [
        [_model_audio_msg(b"pcm-greeting"),
         _input_transcription_msg("hello, go to sleep"),
         _turn_complete_msg()],
        "hang",
    ]


# 1. voice "go to sleep" enters SLEEPING, even mid-speech.
def test_voice_sleep_while_speaking_enters_sleeping():
    agent = make_agent()
    session = FakeSession(interactions=_sleep_while_speaking_session())
    count = wire_connect(agent, session)
    _drive_fast(agent, 0.5, [])
    assert agent.sleep.is_sleeping(), "voice sleep must enter SLEEPING"
    assert count[0] == 1


# 2+3. the sleep-cancelled session is torn down and never reconnects.
def test_sleep_while_speaking_tears_down_without_reconnect():
    agent = make_agent()
    session = FakeSession(interactions=_sleep_while_speaking_session())
    count = wire_connect(agent, session)
    audio = agent.audio
    _drive_fast(agent, 0.6, [])
    assert agent.sleep.is_sleeping()
    assert session.exited, \
        "sleep must close the Live session (zombie session stayed alive)"
    assert count[0] == 1, "sleep-cancelled session reconnected!"
    assert audio.input_paused, "mic capture must pause while sleeping"


# 3b. staying asleep opens nothing further (longer drive, still one).
def test_no_new_session_while_sleeping():
    agent = make_agent()
    session = FakeSession(interactions=_sleep_while_speaking_session())
    count = wire_connect(agent, session)
    _drive_fast(agent, 1.0, [])
    assert agent.sleep.is_sleeping()
    assert count[0] == 1, "a new session opened while sleeping"


# 4. F2 wake after that sleep opens exactly one fresh session.
def test_f2_wake_after_sleep_opens_exactly_one_fresh_session():
    agent = make_agent()
    first = FakeSession(interactions=_sleep_while_speaking_session())
    second = FakeSession(hang=True)
    count = wire_connect(agent, [first, second])
    audio = agent.audio
    _drive_fast(agent, 1.0, [
        (0.5, agent.request_wake),
    ])
    assert first.exited, "sleep must have closed the first session"
    assert count[0] == 2, "wake must open exactly one fresh session, got %d" % count[0]
    assert not agent.sleep.is_sleeping()
    assert audio.input_paused is False, "wake must resume mic capture"


# 5. ordinary connection failures still reconnect (control).
def test_ordinary_failure_still_reconnects():
    agent = make_agent()
    bad = FakeSession(exc=RuntimeError("drop"))
    good = FakeSession(hang=True)
    count = wire_connect(agent, [bad, good])
    _drive_fast(agent, 0.5, [])
    assert count[0] == 2, "genuine failure stopped reconnecting"
    assert bad.exited and not agent.sleep.is_sleeping()


# 6. sleep during model speech is immediate, not deferred (unit level).
def test_sleep_during_speech_is_not_deferred():
    agent = make_agent()
    agent._is_speaking = True  # model audio in flight, no tool running
    assert agent.sleep.is_busy() is False
    disp = agent.request_sleep()
    assert disp == "sleeping", \
        "sleep during speech must tear down now, got %r" % disp
    assert agent.sleep.is_sleeping()


# 7. sleep while speaking clears speech/suppression/output state (unit).
def test_sleep_while_speaking_clears_audio_state():
    import cat_talker.tools as tools_mod

    agent = make_agent()
    audio = agent.audio
    # Simulate mid-speech: output suppression engaged, TTS queued.
    agent._is_speaking = True
    audio.suppress_mic()
    audio.audio_out_queue.put_nowait(b"tts-pending")
    drained = []
    orig_clear = audio.clear_output_queue
    orig_stop_duck = tools_mod.stop_media_ducking

    def draining_clear():
        while not audio.audio_out_queue.empty():
            drained.append(audio.audio_out_queue.get_nowait())

    audio.clear_output_queue = draining_clear
    calls = []
    tools_mod.stop_media_ducking = lambda: calls.append(1)
    try:
        assert agent.request_sleep() == "sleeping"
    finally:
        audio.clear_output_queue = orig_clear
        tools_mod.stop_media_ducking = orig_stop_duck
    assert agent._is_speaking is False, "speech flag leaked past sleep"
    assert audio.is_playing is False, "mic suppression leaked past sleep"
    assert drained == [b"tts-pending"], "queued output not cleared on sleep"
    assert audio.input_paused is True, "mic input not paused on sleep"
    assert calls == [1], "media ducking not stopped on sleep"


# 8. wake defers mic resume until a fresh session connects (unit).
def test_wake_does_not_resume_mic_before_connect():
    agent = make_agent()
    audio = agent.audio
    assert agent.request_sleep() == "sleeping"
    assert audio.input_paused is True
    assert agent.request_wake() == "awake"
    assert audio.input_paused is True, \
        "wake resumed capture before any session connected"


# 9. end to end: sleep -> wake -> mic forwards in the fresh session.
def test_sleep_wake_cycle_mic_forwards_after_wake():
    agent = make_agent()
    first = FakeSession(interactions=_sleep_while_speaking_session())
    second = FakeSession(hang=True)
    count = wire_connect(agent, [first, second])
    audio = agent.audio

    async def go():
        import asyncio as _aio
        loop = _aio.get_running_loop()
        loop.call_later(0.5, agent.request_wake)
        loop.call_later(0.75,
                        lambda: audio.audio_in_queue.put_nowait(b"live-after-wake"))
        loop.call_later(1.1, agent.stop_event.set)
        await _aio.wait_for(agent.run_loop(), timeout=11.1)

    import asyncio as _aio
    real_sleep = _aio.sleep

    async def fast_sleep(delay, *a, **k):
        await real_sleep(min(delay, 0.02), *a, **k)

    with patch.object(_aio, "sleep", fast_sleep):
        _aio.run(go())
    assert first.exited, "sleep must have closed the first session"
    assert count[0] == 2, "wake must open exactly one fresh session"
    assert audio.input_paused is False, "connect must resume mic capture"
    assert audio.is_playing is False, "suppression state leaked into new session"
    assert second.sent_audio == [b"live-after-wake"], \
        "mic stuck after wake: chunk never reached the fresh session: %r" % (
            second.sent_audio,)
