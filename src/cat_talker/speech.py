"""Standalone text-to-speech output boundary (replaceable, stdlib+numpy).

Architecture:
    read_aloud(text) [tools.py] -> speak() -> split_text() chunks
    -> SpeechProvider.synthesize() PCM -> registered output sink
    (AudioInterface.queue_output: the existing 24 kHz playback path).

This fills exactly one gap: Gemini Live already speaks normal turns,
but long/arbitrary tool and web text cannot be read aloud reliably
through a model turn. This module is output-only and never touches
microphone capture, sleep/wake, or the input pipeline.

Default provider shells to a LOCAL espeak-ng/espeak binary (argv list
only, never via a shell) and converts its WAV stdout to 24 kHz mono
int16 PCM with numpy (already a project dependency). No cloud, no
credentials, no new dependencies. If no engine binary is installed,
speak() fails honestly with install guidance instead of faking audio.
"""

import io
import re
import shutil
import subprocess
import wave

import numpy as np

# Engine binaries probed in order; first one found wins.
ENGINE_BINARIES = ("espeak-ng", "espeak")

# Playback contract of AudioInterface: 24 kHz mono int16 PCM chunks.
OUTPUT_RATE = 24000

# Bounded synthesis: one engine call per chunk, never a huge string.
MAX_CHARS_PER_CHUNK = 200
MAX_TOTAL_CHARS = 2000
MAX_CHUNKS = 10
SYNTH_TIMEOUT = 30


class SpeechError(Exception):
    """Synthesis or output failure (reported honestly, never raised to UI)."""


class SpeechProvider:
    """Boundary: text chunk -> 24 kHz mono int16 PCM bytes."""

    name = "base"

    def synthesize(self, text: str) -> bytes:
        raise NotImplementedError


def _find_engine() -> str:
    for binary in ENGINE_BINARIES:
        path = shutil.which(binary)
        if path:
            return path
    raise SpeechError(
        "Speech engine not installed. Install espeak-ng "
        "(`sudo apt install espeak-ng`) to enable read-aloud.")


class EspeakProvider(SpeechProvider):
    """Local espeak-ng/espeak over argv-only subprocess (no shell)."""

    name = "espeak"

    def __init__(self, binary: str | None = None, voice: str | None = None):
        self.binary = binary or _find_engine()
        if voice is not None and (not isinstance(voice, str)
                                  or not voice.strip()):
            raise SpeechError("Speech error: invalid voice name.")
        self.voice = voice.strip() if isinstance(voice, str) else None

    def synthesize(self, text: str) -> bytes:
        if not isinstance(text, str) or not text.strip():
            raise SpeechError("Speech error: nothing to speak.")
        argv = [self.binary, "--stdout"]
        if self.voice:
            argv += ["-v", self.voice]
        argv += [text.strip()]
        try:
            proc = subprocess.run(
                argv, capture_output=True, timeout=SYNTH_TIMEOUT,
                check=False)
        except FileNotFoundError as e:
            raise SpeechError(
                "Speech engine not installed. Install espeak-ng "
                "(`sudo apt install espeak-ng`).") from e
        except subprocess.TimeoutExpired as e:
            raise SpeechError("Speech synthesis timed out.") from e
        except OSError as e:
            raise SpeechError(f"Speech engine failed: {e}") from e
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode("utf-8",
                                                 errors="replace").strip()
            raise SpeechError(
                f"Speech engine failed (exit {proc.returncode})"
                + (f": {detail[:200]}" if detail else "."))
        if not proc.stdout:
            raise SpeechError("Speech engine produced no audio.")
        return wav_to_pcm24k(proc.stdout)


def wav_to_pcm24k(wav_bytes: bytes) -> bytes:
    """Convert WAV bytes to 24 kHz mono int16 PCM bytes."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            rate = wav.getframerate()
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError, ValueError) as e:
        raise SpeechError(f"Speech engine returned bad audio: {e}") from e
    if not frames:
        raise SpeechError("Speech engine produced no audio.")
    if width != 2:
        raise SpeechError(
            f"Speech engine returned unsupported audio ({width * 8}-bit).")
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float64)
    if channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)
    elif channels != 1:
        raise SpeechError(
            f"Speech engine returned {channels}-channel audio.")
    if rate != OUTPUT_RATE:
        src_x = np.linspace(0.0, 1.0, num=len(samples))
        dst_n = max(1, int(round(len(samples) * OUTPUT_RATE / rate)))
        dst_x = np.linspace(0.0, 1.0, num=dst_n)
        samples = np.interp(dst_x, src_x, samples)
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…।])\s+")


def split_text(text: str) -> list[str]:
    """Split text into bounded speakable chunks (sentence-aware).

    Returns [] for empty/invalid input. Caps total input at
    MAX_TOTAL_CHARS and chunk count at MAX_CHUNKS.
    """
    if not isinstance(text, str):
        return []
    collapsed = " ".join(text.split())
    if not collapsed:
        return []
    collapsed = collapsed[:MAX_TOTAL_CHARS]
    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE_SPLIT.split(collapsed):
        while len(sentence) > MAX_CHARS_PER_CHUNK:
            piece, sentence = (sentence[:MAX_CHARS_PER_CHUNK],
                               sentence[MAX_CHARS_PER_CHUNK:])
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece)
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= MAX_CHARS_PER_CHUNK:
            current += " " + sentence
        else:
            chunks.append(current)
            current = sentence
        if len(chunks) >= MAX_CHUNKS:
            break
    if current and len(chunks) < MAX_CHUNKS:
        chunks.append(current)
    return chunks[:MAX_CHUNKS]


# Output sink: set once by the assistant startup path to
# AudioInterface.queue_output. Stays None (honest failure) otherwise,
# so this module never reaches into lifecycle or audio-input code.
_output_sink = None


def set_output_sink(fn) -> None:
    """Register the PCM consumer (e.g. AudioInterface.queue_output)."""
    global _output_sink
    _output_sink = fn


def speak(text: str, provider: SpeechProvider | None = None,
          sink=None) -> str:
    """Speak text through the output sink. Never raises.

    Returns a human-readable success/failure message.
    """
    try:
        if not isinstance(text, str) or not text.strip():
            return "Speech error: please provide text to read aloud."
        out = sink if sink is not None else _output_sink
        if out is None:
            return ("Speech error: speech output is not available "
                    "in this session.")
        chunks = split_text(text)
        if not chunks:
            return "Speech error: please provide text to read aloud."
        engine = provider if provider is not None else EspeakProvider()
        spoken = 0
        for chunk in chunks:
            try:
                pcm = engine.synthesize(chunk)
            except NotImplementedError as e:
                return f"Speech error: {e}"
            if not pcm:
                return "Speech error: engine produced no audio."
            out(pcm)
            spoken += 1
        total = len(" ".join(chunks))
        note = (" (truncated to the speech limit)"
                if len(" ".join(text.split())) > MAX_TOTAL_CHARS else "")
        return (f"Read aloud {spoken} part(s), {total} characters{note}.")
    except SpeechError as e:
        return str(e)
    except Exception as e:
        return f"Speech failed: {e}"
