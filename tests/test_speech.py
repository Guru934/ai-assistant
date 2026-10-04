"""Focused tests for the standalone speech-output boundary.

Everything runs against fakes: no speakers, no speech engine, no
microphone. Subprocess and engine discovery are mocked; WAV fixtures
are synthesized in-memory with the stdlib wave module.
"""

import io
import os
import socket
import struct
import sys
import wave

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cat_talker.agent import SIDE_EFFECT_TOOLS, build_system_instructions
from cat_talker.tools import ALL_TOOLS
import cat_talker.speech as sp
from cat_talker.speech import (
    MAX_CHARS_PER_CHUNK,
    MAX_TOTAL_CHARS,
    EspeakProvider,
    SpeechError,
    split_text,
    speak,
    wav_to_pcm24k,
)


@pytest.fixture(autouse=True)
def _no_real_sockets(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("real network access blocked in tests")
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)


@pytest.fixture(autouse=True)
def _clean_sink():
    sp.set_output_sink(None)
    yield
    sp.set_output_sink(None)


def _wav_bytes(rate=22050, channels=1, seconds=0.1):
    n = int(rate * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = struct.pack(f"<{n * channels}h", *([1000] * n * channels))
        w.writeframes(frames)
    return buf.getvalue()


class _FakeProvider(sp.SpeechProvider):
    name = "fake"

    def __init__(self, pcm=b"\x01\x02" * 240, fail=False):
        self.pcm = pcm
        self.fail = fail
        self.calls = []

    def synthesize(self, text):
        self.calls.append(text)
        if self.fail:
            raise SpeechError("engine boom")
        return self.pcm


# ─── chunking ─────────────────────────────────────────────────────

def test_split_short_text_single_chunk():
    assert split_text("Hello world.") == ["Hello world."]


def test_split_empty_and_invalid():
    assert split_text("") == []
    assert split_text("   ") == []
    assert split_text(None) == []
    assert split_text(42) == []


def test_split_long_text_bounded_chunks():
    text = " ".join(f"Sentence number {i} here." for i in range(40))
    chunks = split_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= MAX_CHARS_PER_CHUNK for c in chunks)
    assert len(chunks) <= sp.MAX_CHUNKS
    assert " ".join(chunks)[:50] in text


def test_split_caps_total_input():
    chunks = split_text("word " * 5000)
    assert len(" ".join(chunks)) <= MAX_TOTAL_CHARS + MAX_CHARS_PER_CHUNK


# ─── speak() ──────────────────────────────────────────────────────

def test_basic_invocation_feeds_sink():
    fed = []
    provider = _FakeProvider()
    out = speak("Hello there.", provider=provider,
                sink=fed.append)
    assert "Read aloud 1 part" in out
    assert fed == [provider.pcm]
    assert provider.calls == ["Hello there."]


def test_long_text_speaks_each_chunk():
    fed = []
    provider = _FakeProvider()
    text = " ".join(f"Sentence number {i} here." for i in range(20))
    out = speak(text, provider=provider, sink=fed.append)
    assert "Read aloud" in out and "1 part" not in out
    assert len(fed) == len(provider.calls) > 1
    assert all(len(c) <= MAX_CHARS_PER_CHUNK for c in provider.calls)


def test_empty_text_never_calls_provider():
    provider = _FakeProvider()
    assert "provide text" in speak("   ", provider=provider,
                                   sink=lambda b: None).lower()
    assert provider.calls == []


def test_provider_failure_is_honest():
    fed = []
    out = speak("Hi.", provider=_FakeProvider(fail=True),
                sink=fed.append)
    assert "boom" in out
    assert fed == []


def test_missing_sink_is_honest():
    out = speak("Hi.", provider=_FakeProvider())
    assert "not available" in out.lower()


def test_engine_missing_gives_install_guidance(monkeypatch):
    monkeypatch.setattr(sp.shutil, "which", lambda *_: None)
    out = speak("Hi.", sink=lambda b: None)
    assert "espeak-ng" in out


# ─── no shell injection ───────────────────────────────────────────

def test_hostile_text_passed_as_argv_element(monkeypatch):
    hostile = '$(rm -rf /); `evil` | cat /etc/passwd && "quoted"'
    seen = {}

    class _Proc:
        returncode = 0
        stdout = _wav_bytes()
        stderr = b""

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(sp.subprocess, "run", _fake_run)
    monkeypatch.setattr(sp.shutil, "which", lambda *_: "/usr/bin/espeak-ng")
    fed = []
    out = speak(hostile, sink=fed.append)
    assert "Read aloud" in out
    assert seen["kwargs"].get("shell") in (None, False)
    assert isinstance(seen["argv"], list)
    assert hostile in seen["argv"]
    assert len(fed) == 1


def test_no_shell_anywhere_in_module():
    import inspect
    import re
    src = inspect.getsource(sp)
    assert not re.search(r"\bshell\s*=", src)
    assert "os.system" not in src
    assert "os.popen" not in src


# ─── wav conversion ───────────────────────────────────────────────

def test_wav_to_pcm24k_resamples_and_monos():
    pcm = wav_to_pcm24k(_wav_bytes(rate=22050, channels=2, seconds=0.1))
    expect_n = round(2205 * 24000 / 22050)
    assert len(pcm) == expect_n * 2  # int16 mono
    import numpy as np
    assert np.frombuffer(pcm, dtype=np.int16).shape == (expect_n,)


def test_wav_to_pcm24k_rejects_garbage():
    with pytest.raises(SpeechError):
        wav_to_pcm24k(b"not a wav file at all............")


# ─── isolation from microphone code ───────────────────────────────

def test_no_microphone_imports():
    """speech.py must not import (or drive) microphone/input code.

    Documentation may name the output sink; only real imports and
    input-control calls are forbidden.
    """
    import inspect
    src = inspect.getsource(sp)
    imports = [ln.strip() for ln in src.splitlines()
               if ln.strip().startswith(("import ", "from "))]
    joined = "\n".join(imports).lower()
    for token in ("pyaudio", "audiointerface", "echo_suppress",
                  "echosuppressor"):
        assert token not in joined, token
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith(("#", '"""')))
    for call in ("pause_input", "suppress_mic(", "mic_callback",
                 "pw-record", "pw_record"):
        assert call not in code, call


# ─── tool registration ────────────────────────────────────────────

def test_read_aloud_registered_exactly_once():
    names = [f.__name__ for f in ALL_TOOLS]
    assert names.count("read_aloud") == 1


def test_read_aloud_classified_as_output_side_effect():
    # Audible speaker output, like send_notification: dedupe repeats.
    assert "read_aloud" in SIDE_EFFECT_TOOLS
    assert "send_notification" in SIDE_EFFECT_TOOLS


def test_system_guidance_limits_read_aloud_to_explicit_requests():
    text = build_system_instructions()
    assert "read_aloud" in text
    assert "already arrive as speech" in text
