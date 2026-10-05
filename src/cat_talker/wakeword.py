"""Optional LOCAL wake-word detection (openwakeword, fully offline).

Design contract:
- Never touches Gemini: no sessions, no audio upload while sleeping.
- Never opens a microphone: audio arrives via AudioInterface.wake_tap,
  which fires inside the already-open capture callback while input is
  paused for sleep. Single-stream ownership is preserved by construction.
- Never wakes twice: detection only calls the agent's on_wake hook
  (which routes through the same request_wake() as F2); a cooldown plus
  the agent stopping the detector after wake make repeats harmless.
- Unavailable when the openwakeword dependency (or model) is missing:
  create_detector() returns None and the assistant behaves exactly as
  before (F2 still wakes).
"""

import os
import queue
import threading

import numpy as np

from cat_talker.logging_config import get_logger

logger = get_logger("cat_talker.wakeword")

# openwakeword consumes multiples of 1280 samples (80 ms @ 16 kHz).
FRAME_SAMPLES = 1280
# Consecutive over-threshold frames required (cuts false positives).
PATIENCE_FRAMES = 2
# Minimum seconds between two wake callbacks from one detector.
COOLDOWN_S = 2.0
# Longest a mic chunk waits before being dropped (detector never blocks
# the audio callback or grows memory while sleeping).
QUEUE_MAX_CHUNKS = 64


def available() -> bool:
    """True when the openwakeword dependency imports."""
    try:
        import openwakeword  # noqa: F401
        return True
    except Exception:
        return False


def builtin_models():
    """Built-in openwakeword model keys (empty when backend missing)."""
    try:
        import openwakeword
        return frozenset((openwakeword.models or {}).keys())
    except Exception:
        return frozenset()


# Loaded onnx sessions cached by model path: every sleep entry would
# otherwise pay a seconds-long reload for the same file. Inference
# sessions are safe for sequential use; detectors run one at a time
# (each is stopped before the next starts).
_MODEL_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def load_model_cached(model_path: str):
    """Return the shared (Model, key) for a model path, loading once."""
    with _CACHE_LOCK:
        hit = _MODEL_CACHE.get(model_path)
    if hit is not None:
        return hit
    from openwakeword.model import Model
    model = Model(wakeword_model_paths=[model_path])
    names = list(getattr(model, "models", {}).keys())
    base = os.path.basename(model_path)[0:-5]
    key = base if base in names else (names[0] if names else base)
    with _CACHE_LOCK:
        _MODEL_CACHE[model_path] = (model, key)
    return model, key


def resolve_model_path(model: str):
    """Map a configured model to (path, name).

    Accepts an absolute path to a custom trained .onnx, else a built-in
    openwakeword model key. Returns (None, "") when unresolvable.
    """
    try:
        import openwakeword
        if isinstance(model, str) and os.path.isfile(model) \
                and model.endswith(".onnx"):
            name = os.path.basename(model)[0:-5]
            return model, name
        builtin = (openwakeword.models or {}).get(model or "", None)
        if isinstance(builtin, dict) and os.path.isfile(
                builtin.get("model_path", "")):
            return builtin["model_path"], model
        return None, ""
    except Exception as e:
        logger.warning("wake detector: model resolution failed: %s", e)
        return None, ""


class WakeDetector:
    """Buffered openwakeword loop in a daemon thread.

    feed() is called from the PortAudio callback thread: it only
    enqueues (never blocks, never predicts). The worker thread batches
    1280-sample frames and calls predict(); over-threshold scores
    invoke on_wake() at most once per COOLDOWN_S.
    """

    def __init__(self, model_path, model_name, threshold=0.5,
                 on_wake=None):
        self._model_path = model_path
        self._model_name = model_name
        self._threshold = threshold
        self._on_wake = on_wake
        self._chunks: queue.Queue = queue.Queue(maxsize=QUEUE_MAX_CHUNKS)
        self._stop = threading.Event()
        self._thread = None
        self._running = False
        self._last_fire = 0.0

    @property
    def running(self) -> bool:
        return self._running and not self._stop.is_set()

    def feed(self, chunk: bytes):
        """Enqueue one mic chunk (audio-callback thread; never blocks)."""
        if not self.running or not chunk:
            return
        try:
            self._chunks.put_nowait(bytes(chunk))
        except queue.Full:
            pass  # drop: stale audio is useless for wake detection

    def start(self) -> bool:
        """Spawn the worker (loads the model there, cached after the
        first load). Non-blocking: failure is reported by the thread,
        and the caller falls back to F2-only. Returns False only if a
        start is already in flight."""
        if self._thread is not None:
            return self._running
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="wake-detector")
        self._thread.start()
        return True

    def stop(self):
        # Signal-only by design: the worker notices the flag within one
        # queue timeout and exits on its own (daemon thread). Joining
        # here would stall the agent event loop on every wake/shutdown
        # transition. Overlapping exit/entry for under a second is safe:
        # inference sessions are thread-safe and wake is idempotent.
        self._stop.set()
        self._running = False
        self._thread = None

    def _run(self):
        import time as _t
        try:
            model, key = load_model_cached(self._model_path)
            if self._model_name in (getattr(model, "models", {}) or {}):
                key = self._model_name
            self._key = key
            self._model = model
            self._running = True
            logger.info("wake detector: model %r loaded (threshold %.2f)",
                        key, self._threshold)
        except Exception as e:
            logger.warning("wake detector: model load failed: %s", e)
            self._running = False
            return
        buf = bytearray()
        while not self._stop.is_set():
            try:
                chunk = self._chunks.get(timeout=0.5)
            except queue.Empty:
                continue
            buf.extend(chunk)
            while len(buf) >= FRAME_SAMPLES * 2 and not self._stop.is_set():
                frame = bytes(buf[:FRAME_SAMPLES * 2])
                del buf[:FRAME_SAMPLES * 2]
                self._score_frame(frame, _t.monotonic())
            # Bound memory while sleeping for hours with no speech.
            if len(buf) > FRAME_SAMPLES * 2 * 32:
                del buf[:-FRAME_SAMPLES * 2 * 8]
        self._running = False

    def _score_frame(self, frame: bytes, now: float):
        try:
            audio = np.frombuffer(frame, dtype=np.int16)
            scores = self._model.predict(
                audio,
                patience={self._key: PATIENCE_FRAMES},
                threshold={self._key: self._threshold})
            score = float(scores.get(self._key, 0.0) or 0.0)
        except Exception as e:
            logger.warning("wake detector: predict failed: %s", e)
            return
        if score > 0 and now - self._last_fire >= COOLDOWN_S:
            self._last_fire = now
            logger.info("WAKE_WORD_DETECTED model=%r score=%.3f",
                        self._key, score)
            try:
                if self._on_wake is not None:
                    self._on_wake()
            except Exception as e:
                logger.warning("wake detector: on_wake failed: %s", e)


def create_detector(model="", phrase="", threshold=0.5, on_wake=None):
    """Build a WakeDetector, or None when unavailable/misconfigured.

    None means: backend missing, model unresolvable, or bad threshold -
    the caller keeps F2-only behavior unchanged.
    """
    if not available():
        logger.debug("wake detector unavailable: openwakeword not installed")
        return None
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        return None
    if not 0.0 < threshold <= 1.0:
        return None
    path, name = resolve_model_path(model)
    if not path:
        logger.warning("wake detector: unknown model %r", model)
        return None
    return WakeDetector(path, name, threshold=threshold, on_wake=on_wake)
