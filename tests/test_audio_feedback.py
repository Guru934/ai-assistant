"""Focused tests for the mic/output feedback race in AudioInterface.

No hardware is touched: AudioInterface.__init__ (PyAudio streams) is never
run. Instances are built via __new__ with stubbed collaborators, so these
tests run on any machine with or without a sound card.
"""

import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pyaudio

from cat_talker.audio import AudioInterface
from cat_talker.echo_suppress import EchoSuppressor


class FakeLoop:
    def __init__(self):
        self.calls = []

    def call_soon_threadsafe(self, cb, *args):
        self.calls.append((cb, args))


class FakeOutStream:
    def __init__(self):
        self.written = []

    def write(self, chunk):
        self.written.append(chunk)


def _bare_iface(**overrides):
    """An AudioInterface with no hardware: collaborators stubbed."""
    iface = AudioInterface.__new__(AudioInterface)
    iface._running = True
    iface._loop_closed = False
    iface._closed = False
    iface.is_playing = False
    iface.mic_active = False
    iface.volume_cb = None
    iface.audio_out_queue = queue.Queue()
    iface.out_stream = FakeOutStream()
    # Echo-suppression collaborators (same defaults as __init__): a real
    # suppressor, enabled. Odd-size test chunks bypass it by size guard,
    # so these tests exercise the hook without altering expectations.
    iface._echo_enabled = True
    iface.echo = EchoSuppressor(enabled=True)
    iface._last_echo_decision = None
    iface._playback_chunks = 0
    for key, value in overrides.items():
        setattr(iface, key, value)
    return iface


def test_mic_forwards_user_speech_when_idle():
    """Normal user speech reaches Gemini: idle mic passes audio through."""
    loop = FakeLoop()
    iface = _bare_iface(loop=loop, audio_in_queue=queue.Queue())
    out = iface._mic_callback(b"ABCD", 1, None, None)
    assert out == (None, pyaudio.paContinue)
    assert len(loop.calls) == 1
    _, args = loop.calls[0]
    assert args == (b"ABCD",)
    assert iface.mic_active is True


def test_mic_substitutes_silence_while_playing():
    """During the protected playback window the mic must NOT forward what it
    hears (the assistant's own speech) - silence goes to Gemini instead."""
    loop = FakeLoop()
    iface = _bare_iface(
        loop=loop, audio_in_queue=queue.Queue(), is_playing=True
    )
    out = iface._mic_callback(b"ABCD", 1, None, None)
    assert out == (None, pyaudio.paContinue)
    assert len(loop.calls) == 1
    _, args = loop.calls[0]
    assert args == (b"\x00" * 4,)
    assert iface.mic_active is False


def _run_player(iface):
    thread = threading.Thread(target=iface._play_audio, daemon=True)
    thread.start()
    return thread


def _wait_for(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.005)
    return pred()


def test_playback_does_not_toggle_mic_state():
    """Output-level ownership: playing chunks must never change is_playing.
    The agent mutes via suppress_mic() when the model starts responding and
    releases after the turn; playback itself leaves the flag alone, so no
    per-chunk toggling can reopen the mic mid-turn."""
    iface = _bare_iface()
    thread = _run_player(iface)
    try:
        iface.audio_out_queue.put(b"c1")
        assert _wait_for(lambda: iface.out_stream.written == [b"c1"]), \
            "first chunk was never played"
        time.sleep(0.03)  # inter-chunk gap: state must not move
        assert iface.is_playing is False
        iface.audio_out_queue.put(b"c2")
        assert _wait_for(lambda: len(iface.out_stream.written) == 2), \
            "second chunk was never played"
        time.sleep(0.03)
        assert iface.is_playing is False, \
            "playback toggled mic state (chunk-level behavior regressed)!"
    finally:
        iface.audio_out_queue.put(None)
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "playback thread did not stop"


def test_output_level_suppression_across_chunks():
    """suppress_mic() holds the mic muted across ALL chunks of the turn
    (sampled continuously, gap included); release_mic() hands capture back
    and the mic callback forwards real speech again."""
    loop = FakeLoop()
    iface = _bare_iface(loop=loop, audio_in_queue=queue.Queue())
    thread = _run_player(iface)
    try:
        iface.suppress_mic()
        iface.audio_out_queue.put(b"c1")
        assert _wait_for(lambda: iface.out_stream.written == [b"c1"])
        time.sleep(0.02)
        iface.audio_out_queue.put(b"c2")
        samples = []
        gap_start = time.monotonic()
        while len(iface.out_stream.written) < 2:
            samples.append(iface.is_playing)
            if time.monotonic() - gap_start > 2.0:
                break
            time.sleep(0.005)
        assert samples, "no samples taken during the gap"
        assert all(samples), "mic opened mid-turn!"

        iface.release_mic()
        assert iface.is_playing is False
        iface._mic_callback(b"ABCD", 1, None, None)
        _, args = loop.calls[0]
        assert args == (b"ABCD",), "speech not forwarded after release"
    finally:
        iface.audio_out_queue.put(None)
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "playback thread did not stop"
