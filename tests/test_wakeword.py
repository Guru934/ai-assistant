"""Focused tests for OPTIONAL local wake-word activation.

The detector never opens Gemini, never opens a second mic stream, and
wakes only through the same request_wake() F2 uses. A FakeDetector stands
in for openwakeword (its real import/model path is covered by small
integration tests); run_loop flows use fake sessions and FakeAudio.
"""

import asyncio
import logging
import os
import sys
import types as pytypes
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cat_talker.agent as agent_mod
import cat_talker.config as config_mod
import cat_talker.wakeword as wakeword_mod
from cat_talker.agent import GeminiDesktopAgent
from test_session_lifecycle import (
    FakeSession,
    make_agent,
    wire_connect,
    _fast_sleep,
    _input_transcription_msg,
)


class FakeDetector:
    """Test double for WakeDetector: explicit trigger, no audio, no ML."""

    instances = []

    def __init__(self, on_wake=None):
        self.on_wake = on_wake
        self.started = False
        self.stopped = False
        self.feed_calls = 0
        FakeDetector.instances.append(self)

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.stopped = True

    def feed(self, chunk):
        self.feed_calls += 1

    @property
    def running(self):
        return self.started and not self.stopped

    def trigger(self):
        if self.on_wake is not None:
            self.on_wake()


def _wake_config(**over):
    cfg = {"wake_word_enabled": True,
           "wake_word_phrase": "Hey Jarvis",
           "wake_word_model": "hey_jarvis",
           "wake_word_threshold": 0.5}
    cfg.update(over)
    return cfg


def _patch_wake_factory(monkeypatch, cfg):
    """Route agent config + detector creation to fakes (opts back in
    over the suite-wide CAT_TALKER_WAKEWORD=0 default)."""
    FakeDetector.instances.clear()
    monkeypatch.setenv("CAT_TALKER_WAKEWORD", "1")
    monkeypatch.setattr(config_mod, "load_config", lambda: dict(cfg))

    def _factory(**kwargs):
        return FakeDetector(kwargs.get("on_wake"))

    monkeypatch.setattr(wakeword_mod, "create_detector", _factory)


async def _drive(agent, stop_after, actions=()):
    loop = asyncio.get_running_loop()
    for delay, fn in actions:
        loop.call_later(delay, fn)
    loop.call_later(stop_after, agent.stop_event.set)
    await asyncio.wait_for(agent.run_loop(), timeout=stop_after + 10.0)


def _run_drive(agent, stop_after, actions=()):
    async def go():
        await _drive(agent, stop_after, actions)

    with patch.object(asyncio, "sleep", _fast_sleep):
        asyncio.run(go())


def _sleeping_agent():
    agent = make_agent()  # awake per contract
    assert agent.request_sleep() == "sleeping"
    return agent


# ---------------------------------------------------------------------------
# 1-3: unavailable / disabled / F2 fallback
# ---------------------------------------------------------------------------

def test_detector_unavailable_sleep_unchanged(monkeypatch):
    """openwakeword missing: sleep works, F2 wakes, no detector, no tap."""
    monkeypatch.setitem(sys.modules, "openwakeword", None)
    assert wakeword_mod.available() is False
    assert wakeword_mod.create_detector(model="hey_jarvis") is None
    agent = _sleeping_agent()
    session = FakeSession(interactions=["hang"])
    count = wire_connect(agent, session)
    _run_drive(agent, 0.4, actions=[(0.15, agent.request_wake)])
    assert count[0] == 1, "F2-equivalent wake must open exactly one session"
    assert getattr(agent.audio, "wake_tap", None) is None


def test_wake_disabled_never_creates_detector(monkeypatch):
    FakeDetector.instances.clear()
    monkeypatch.setattr(config_mod, "load_config",
                        lambda: _wake_config(wake_word_enabled=False))

    def _boom(**kwargs):
        raise AssertionError("detector must not be created when disabled")

    monkeypatch.setattr(wakeword_mod, "create_detector", _boom)
    agent = _sleeping_agent()
    session = FakeSession(interactions=["hang"])
    count = wire_connect(agent, session)
    _run_drive(agent, 0.4, actions=[(0.15, agent.request_wake)])
    assert count[0] == 1
    assert getattr(agent.audio, "wake_tap", None) is None


def test_sleeping_creates_no_gemini_session():
    agent = _sleeping_agent()
    session = FakeSession(interactions=["hang"])
    count = wire_connect(agent, session)
    _run_drive(agent, 0.3)
    assert count[0] == 0, "sleeping must never connect to Gemini"
    assert agent.sleep.is_sleeping() is True


# ---------------------------------------------------------------------------
# 4-7: wake event, duplicates, stop-on-session, resume-on-sleep
# ---------------------------------------------------------------------------

def test_wake_event_triggers_exactly_one_wake(monkeypatch, caplog):
    _patch_wake_factory(monkeypatch, _wake_config())
    agent = _sleeping_agent()
    session = FakeSession(interactions=["hang"])
    count = wire_connect(agent, session)

    def _trigger():
        assert FakeDetector.instances, "detector never started"
        FakeDetector.instances[0].trigger()

    with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
        _run_drive(agent, 0.5, actions=[(0.15, _trigger)])
    assert count[0] == 1, "one wake event must open exactly one session"
    assert agent.sleep.is_sleeping() is False
    det = FakeDetector.instances[0]
    assert det.started is True and det.stopped is True, \
        "detector must stop when the Gemini session starts"
    assert getattr(agent.audio, "wake_tap", None) is None
    for marker in ("SLEEPING", "WAKE_DETECTOR_STARTED", "WAKE_WORD_DETECTED",
                   "WAKE_DETECTOR_STOPPED", "GEMINI_SESSION_STARTING"):
        assert any(marker in r.message for r in caplog.records), \
            f"lifecycle missing {marker}"


def test_duplicate_wake_events_no_duplicate_sessions(monkeypatch):
    _patch_wake_factory(monkeypatch, _wake_config())
    agent = _sleeping_agent()
    session = FakeSession(interactions=["hang"])
    count = wire_connect(agent, session)

    def _trigger_twice():
        FakeDetector.instances[0].trigger()
        FakeDetector.instances[0].trigger()

    _run_drive(agent, 0.5, actions=[(0.15, _trigger_twice)])
    assert count[0] == 1, "duplicate detections must not duplicate sessions"
    assert agent._mic_peak <= 1, "mic worker accounting: %r" % agent._mic_peak


def test_detector_resumes_after_return_to_sleep(monkeypatch):
    _patch_wake_factory(monkeypatch, _wake_config())
    agent = _sleeping_agent()
    session = FakeSession(interactions=["hang"])
    count = wire_connect(agent, session)

    def _first():
        FakeDetector.instances[0].trigger()

    def _second():
        assert len(FakeDetector.instances) == 2, FakeDetector.instances
        FakeDetector.instances[1].trigger()

    _run_drive(agent, 0.9, actions=[
        (0.15, _first),
        (0.35, agent.request_sleep),
        (0.55, _second),
    ])
    assert count[0] == 2, "each wake must open exactly one session"
    assert agent.sleep.is_sleeping() is False
    assert all(d.stopped for d in FakeDetector.instances)


def test_graceful_shutdown_with_detector_active(monkeypatch):
    _patch_wake_factory(monkeypatch, _wake_config())
    agent = _sleeping_agent()
    session = FakeSession(interactions=["hang"])
    wire_connect(agent, session)
    _run_drive(agent, 0.4)  # stop_event only; never wakes
    assert agent.sleep.is_sleeping() is True
    assert FakeDetector.instances, "detector should have started"
    assert FakeDetector.instances[0].stopped is True, \
        "shutdown must stop the detector"


# ---------------------------------------------------------------------------
# 8-9: mic ownership - same stream, same worker budget
# ---------------------------------------------------------------------------

def test_same_audio_object_no_second_stream(monkeypatch):
    _patch_wake_factory(monkeypatch, _wake_config())
    agent = _sleeping_agent()
    audio_before = agent.audio
    session = FakeSession(interactions=["hang"])
    wire_connect(agent, session)

    def _trigger():
        assert agent.audio is audio_before, \
            "audio device must never be replaced"
        assert getattr(agent.audio, "wake_tap", None) is not None, \
            "sleeping detector must tap the existing stream"
        FakeDetector.instances[0].trigger()

    _run_drive(agent, 0.5, actions=[(0.15, _trigger)])
    assert audio_before.wake_tap is None, "tap must be cleared after wake"
    assert agent._mic_peak <= 1


def test_wake_tap_only_while_detector_runs():
    import pyaudio
    import queue as _queue
    from cat_talker.audio import AudioInterface

    class _Loop:
        def call_soon_threadsafe(self, *a, **k):
            pass

    audio = AudioInterface.__new__(AudioInterface)
    audio._running = True
    audio._loop_closed = False
    audio._input_paused = True
    audio.is_playing = False
    setattr(audio, "loop", _Loop())
    setattr(audio, "audio_in_queue", _queue.Queue())
    assert getattr(audio, "wake_tap", None) is None
    seen = []
    setattr(audio, "wake_tap", seen.append)
    rc = audio._mic_callback(b"\x01\x02" * 256, 512, None, 0)
    assert rc[1] == pyaudio.paContinue
    assert seen == [b"\x01\x02" * 256]
    audio._input_paused = False  # awake: tap must not fire
    audio._mic_callback(b"\x03" * 1024, 512, None, 0)
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# 10-11: config boundary + real backend integration
# ---------------------------------------------------------------------------

def test_config_persistence_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(tmp_path / "c.json"))
    assert config_mod.get_wake_word_enabled() is False  # conservative
    assert config_mod.get_wake_word_phrase() == "Hey Jarvis"
    config_mod.set_wake_word_enabled(True)
    assert config_mod.get_wake_word_enabled() is True
    config_mod.set_wake_word("Hey Chibi", model="hey_mycroft", threshold=0.7)
    assert config_mod.get_wake_word_phrase() == "Hey Chibi"
    assert config_mod.get_wake_word_model() == "hey_mycroft"
    assert config_mod.get_wake_word_threshold() == 0.7
    config_mod.set_wake_word("x", threshold=99)  # clamped, never stored raw
    assert config_mod.get_wake_word_threshold() == 0.5


def test_real_backend_resolves_builtin_model():
    assert wakeword_mod.available() is True
    path, name = wakeword_mod.resolve_model_path("hey_jarvis")
    assert path is not None and path.endswith(".onnx")
    assert wakeword_mod.create_detector(model="no-such-model") is None
    assert wakeword_mod.create_detector(model="hey_jarvis",
                                        threshold=0) is None
    det = wakeword_mod.create_detector(model="hey_jarvis", threshold=0.5)
    assert det is not None
    det.stop()


def test_on_wake_word_uses_f2_transition():
    agent = make_agent()  # awake
    assert agent._on_wake_word() == "already"
    assert agent.sleep.is_sleeping() is False
    assert agent.request_sleep() == "sleeping"
    assert agent._on_wake_word() == "awake"
    assert agent.sleep.is_sleeping() is False
