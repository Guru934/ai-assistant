"""Reference-based echo suppression for the microphone path.

Problem: the microphone captures whatever comes out of the speakers -
assistant TTS, YouTube/media audio, system sounds - and that speaker audio
is forwarded to Gemini as if the user said it ("User said: ...").

What this module does:
  - Consumes a *reference* signal: PCM captured from the default sink's
    monitor (i.e. exactly what is being played on the speakers, whatever
    the source application is). No system routing changes, no new deps.
  - Per mic chunk it estimates the bulk delay between reference and mic via
    normalized cross-correlation, tracks the coupling with a block-NLMS
    adaptive filter, and SUPPRESSES the chunk (emits silence, same
    convention as the existing half-duplex assistant mute) only when the
    microphone is demonstrably echo-dominated.
  - Near-end protection: if the mic carries significantly more energy than
    the predicted echo (user speaking over music), the chunk PASSES
    through untouched. Ambiguous frames also pass: fail-open by design, so
    this can never be worse than having no suppression.

This is the primary defence for *external* media. The assistant's own
speech is still muted output-level (half-duplex) in AudioInterface; this
module additionally covers the echo tail during the release cooldown.

Pure DSP + stdlib/numpy only: fully testable without audio hardware.
Only energies/counters are ever logged - never raw audio.
"""

import collections

import numpy as np

RATE = 16000
CHUNK_FRAMES = 1024
BYTES_PER_CHUNK = CHUNK_FRAMES * 2  # int16 mono

# Reference history: 1s, enough for delay search + filter context.
REF_HISTORY_FRAMES = RATE
# Consumption pacing: the reader STAGES samples in _pending (in whatever
# quantum the pipe yields) and each mic chunk transfers exactly one
# chunk-worth into the history, so history and mic advance in lockstep.
# Without this, a lumped delivery (e.g. one 2048-frame read per two mic
# chunks) swings the true echo lag by +-1024 samples at the read cadence -
# far outside the 256-tap filter span - which the estimator aliases and
# the filter can never track (measured live: suppressed=0 with reference
# present). Pacing only works if delivery itself is smooth, hence the
# small (128-frame) reads on the producer side.
PACE_FRAMES = CHUNK_FRAMES
# Mic decision delay (chunks): the reference path delivers audio later
# than the mic callback needs it. Holding mic chunks back this many
# callbacks lets the reference arrive, shifting the mean lag solidly into
# estimator range. Cost: 64ms of mic-path latency (chunks still stream
# continuously).
MIC_DELAY_CHUNKS = 1
# Staging trim: while the reader outruns consumption (startup skew where
# the monitor starts seconds before the first mic callback, capture
# stalls, assistant turns with no process() calls), the oldest staged
# samples predate any alignable window. Past this depth, drop oldest down
# to KEEP so the history always tracks fresh reference. Steady delivery
# wobble (~+-128 samples with 128-frame reads, measured) never reaches
# the threshold; any bigger growth is stale skew by definition.
TRIM_AT = 4800
TRIM_KEEP = 2048
# Gap that proves callbacks stopped (stall/recreate/turn) rather than
# normal cadence: clear the mic delay line so pre-gap audio is never
# decided against post-gap history.
STALL_GAP_S = 1.5
# Bulk-delay search range: 0..300ms (PipeWire buffering + air path).
# Coarse step 16 samples (1ms) plus a fine pass: white-noise-like content
# decorrelates within a few samples, so a coarse-only grid can miss the
# peak entirely.
MAX_DELAY_FRAMES = 4800
DELAY_STEP = 16
FINE_WINDOW = 32
FINE_STEP = 4
# Block-NLMS filter length: 16ms of residual misalignment after the bulk
# delay is removed. Deliberately short: we estimate echo *energy* for a
# suppress/pass decision, not sample-perfect cancellation.
FILTER_TAPS = 256
NLMS_MU = 0.5
FILTER_LEAK = 0.999

# Monitor RMS below this => nothing playing => nothing to suppress.
REF_MIN_RMS = 200.0
# Normalized correlation at/above this => mic tracks the reference.
NCORR_SUPPRESS = 0.65
# Residual (mic minus predicted echo) energy at/below this fraction of the
# mic energy => echo explains the mic => echo-dominated => suppress.
RESIDUAL_RATIO = 0.25
# Re-estimate the bulk delay every N chunks, or sooner if tracking is poor.
REESTIMATE_EVERY = 10
REESTIMATE_NCORR = 0.5
# Need at least this much reference history before any decision.
MIN_HISTORY_FRAMES = RATE // 2

DECISION_PASS = "pass"
DECISION_SUPPRESS = "suppress"


def discover_monitor_source(timeout: float = 3.0):
    """Return the default sink's monitor source name, or None.

    Pure discovery helper (subprocess only): '<default-sink>.monitor'.
    Returns None when pactl is missing or the default sink is unknown, in
    which case echo suppression stays disabled and capture behaves as
    before.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["pactl", "info"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if line.startswith("Default Sink:"):
            sink = line.split(":", 1)[1].strip()
            return f"{sink}.monitor" if sink else None
    return None


class EchoSuppressor:
    """Decides per mic chunk: pass through or suppress as speaker echo."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        # Staged reference samples (reader quantum) not yet consumed into
        # the paced history. Holds the natural stream lead (seconds);
        # trimmed only on measured stalls or the hard cap.
        self._pending = collections.deque()
        # Mic decision delay line: chunks wait here MIC_DELAY_CHUNKS
        # callbacks so the slower reference path catches up.
        self._mic_buf = collections.deque()
        self._last_process_t = None
        self._ref = collections.deque(maxlen=REF_HISTORY_FRAMES)
        self._filter = np.zeros(FILTER_TAPS, dtype=np.float64)
        self._delay = 0
        self._best_ncorr = 0.0
        self._chunks_since_estimate = REESTIMATE_EVERY  # force first estimate
        self.stats = {
            "frames_passed": 0,
            "frames_suppressed": 0,
            "frames_no_reference": 0,
        }
        self._last_decision = None
        # DEBUG-only numeric diagnostics for the last processed chunk.
        # Energies as int16-unit RMS floats, ratios unitless. Never audio.
        self.block_index = 0
        self.last_snapshot = {}

    # -- reference feed (monitor thread) ---------------------------------
    def feed_reference(self, pcm: bytes):
        """Stage monitor PCM (int16 mono 16k) for paced consumption.

        Only stages: transfer into the history happens in process(), one
        mic-chunk worth per call, so history and mic advance in lockstep
        regardless of reader quantum.
        """
        if not pcm:
            return
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float64) / 32768.0
        self._pending.extend(arr.tolist())

    def _transfer_paced(self):
        """Move one chunk-worth from staging to history; trim stale skew.

        Returns staged depth after transfer (diagnostics).
        """
        pending = self._pending
        if len(pending) > TRIM_AT:
            for _ in range(len(pending) - TRIM_KEEP):
                pending.popleft()
        take = min(PACE_FRAMES, len(pending))
        for _ in range(take):
            self._ref.append(pending.popleft())
        return len(pending)

    @property
    def reference_ready(self) -> bool:
        return len(self._ref) >= MIN_HISTORY_FRAMES

    # -- per-chunk decision (mic callback thread) -------------------------
    def snapshot_line(self) -> str:
        """One-line DEBUG rendering of the last snapshot (no audio)."""
        s = self.last_snapshot
        if not s:
            return "echo blk=? (no chunk processed yet)"
        return ("echo blk=%(block)d mic_rms=%(mic_rms).0f ref_rms=%(ref_rms).0f "
                "delay=%(delay)s ncorr=%(ncorr)s echo_rms=%(echo_rms)s "
                "resid_rms=%(resid_rms)s ratio=%(ratio)s fifo=%(fifo)s -> "
                "%(decision)s (%(reason)s)" % {k: (s.get(k, "?")) for k in (
                    "block", "mic_rms", "ref_rms", "delay", "ncorr",
                    "echo_rms", "resid_rms", "ratio", "fifo",
                    "decision", "reason")})

    def process(self, mic_pcm: bytes):
        """Return (output_pcm, decision, reason).

        output_pcm is the input unchanged on pass, silence on suppress.
        Fail-open: any ambiguity or error => pass the original audio.
        Side effect: refreshes last_snapshot (numeric diagnostics only).
        """
        snap = {"block": self.block_index, "mic_rms": 0.0, "ref_rms": 0.0,
                "delay": None, "ncorr": None, "echo_rms": None,
                "resid_rms": None, "ratio": None, "decision": None,
                "reason": "", "fifo": len(self._pending)}
        self.block_index += 1
        try:
            return self._process_inner(mic_pcm, snap)
        finally:
            self.last_snapshot = snap

    def _process_inner(self, mic_pcm: bytes, snap: dict):
        if not self.enabled:
            snap.update(decision=DECISION_PASS, reason="disabled")
            return mic_pcm, DECISION_PASS, "disabled"
        if len(mic_pcm) != BYTES_PER_CHUNK:
            snap.update(decision=DECISION_PASS, reason="unexpected-size")
            return mic_pcm, DECISION_PASS, "unexpected-size"
        # Paced consumption: history advances one mic-chunk per call so
        # the history/mic lag stays constant across reader quanta.
        import time as _t
        _now = _t.monotonic()
        _prev = self._last_process_t
        self._last_process_t = _now
        snap["fifo"] = self._transfer_paced()
        if _prev is not None and _now - _prev > STALL_GAP_S:
            # Callbacks stopped (stall/recreate/turn): drop pre-gap mic
            # chunks, they no longer align with post-gap history.
            self._mic_buf.clear()
        # Mic decision delay line: hold this chunk back MIC_DELAY_CHUNKS
        # callbacks so the slower reference path catches up; decide the
        # chunk at the head. Startup/stall transients pass through.
        self._mic_buf.append(mic_pcm)
        if len(self._mic_buf) <= MIC_DELAY_CHUNKS:
            self.stats["frames_passed"] += 1
            snap.update(decision=DECISION_PASS, reason="delay-fill")
            return mic_pcm, DECISION_PASS, "delay-fill"
        mic_pcm = self._mic_buf.popleft()
        if not self.reference_ready:
            self.stats["frames_no_reference"] += 1
            snap.update(decision=DECISION_PASS, reason="no-reference-yet")
            return mic_pcm, DECISION_PASS, "no-reference-yet"

        mic = np.frombuffer(mic_pcm, dtype=np.int16).astype(np.float64) / 32768.0
        mic_e = float(np.mean(mic * mic))
        snap["mic_rms"] = (mic_e ** 0.5) * 32768.0
        if mic_e == 0.0:
            self.stats["frames_passed"] += 1
            snap.update(decision=DECISION_PASS, reason="mic-silent")
            return mic_pcm, DECISION_PASS, "mic-silent"

        ref = np.asarray(self._ref, dtype=np.float64)

        if (self._chunks_since_estimate >= REESTIMATE_EVERY
                or self._best_ncorr < REESTIMATE_NCORR):
            self._delay, self._best_ncorr = self._estimate_delay(mic, ref)
            self._chunks_since_estimate = 0
        self._chunks_since_estimate += 1
        snap["delay"] = self._delay

        ref_seg = self._aligned_segment(ref, self._delay, len(mic))
        ref_e = float(np.mean(ref_seg * ref_seg))
        snap["ref_rms"] = (ref_e ** 0.5) * 32768.0
        if ref_e < (REF_MIN_RMS / 32768.0) ** 2:
            self.stats["frames_passed"] += 1
            snap.update(decision=DECISION_PASS, reason="reference-quiet")
            return mic_pcm, DECISION_PASS, "reference-quiet"

        ncorr = self._ncorr(mic, ref_seg)
        self._best_ncorr = ncorr
        snap["ncorr"] = round(ncorr, 3)

        # Block-NLMS echo estimate on the BULK-DELAY-COMPENSATED
        # reference: the filter only spans FILTER_TAPS (16ms) of residual
        # misalignment, so the reference must first be shifted back by the
        # estimated echo delay (typically ~85ms on this machine). Feeding
        # the raw recent reference leaves the true echo lag outside the
        # filter span -> echo_pred ~= 0 -> residual ~= mic -> never
        # suppress (measured live: ncorr 0.95+ yet ratio ~1.0).
        if self._delay > 0:
            comp = ref[:len(ref) - self._delay]
        else:
            comp = ref
        echo_pred, residual = self._nlms_step(mic, comp)
        echo_e = float(np.mean(echo_pred * echo_pred))
        err_e = float(np.mean(residual * residual))
        snap["echo_rms"] = round((echo_e ** 0.5) * 32768.0, 1)
        snap["resid_rms"] = round((err_e ** 0.5) * 32768.0, 1)
        snap["ratio"] = round(err_e / mic_e, 3)

        if ncorr >= NCORR_SUPPRESS and err_e <= RESIDUAL_RATIO * mic_e:
            self.stats["frames_suppressed"] += 1
            reason = (f"echo-dominated ncorr={ncorr:.2f} "
                      f"residual={err_e / mic_e:.2f}")
            snap.update(decision=DECISION_SUPPRESS, reason=reason)
            return (b"\x00" * len(mic_pcm), DECISION_SUPPRESS, reason)
        self.stats["frames_passed"] += 1
        if ncorr >= NCORR_SUPPRESS:
            reason = (f"near-end-present ncorr={ncorr:.2f} "
                      f"residual={err_e / mic_e:.2f}")
        else:
            reason = f"uncorrelated ncorr={ncorr:.2f}"
        snap.update(decision=DECISION_PASS, reason=reason)
        return mic_pcm, DECISION_PASS, reason

    # -- internals ---------------------------------------------------------
    def _aligned_segment(self, ref, delay: int, n: int):
        end = len(ref) - delay
        start = end - n
        if start < 0:
            seg = np.zeros(n, dtype=np.float64)
            avail = ref[:end] if end > 0 else np.zeros(0)
            seg[n - len(avail):] = avail
            return seg
        return ref[start:end]

    def _ncorr(self, a, b) -> float:
        denom = float(np.sqrt(np.mean(a * a) * np.mean(b * b)))
        if denom == 0.0:
            return 0.0
        return abs(float(np.mean(a * b) / denom))

    def _score_delay(self, mic, mic_n, ref, d):
        seg = self._aligned_segment(ref, d, len(mic))
        seg_n = float(np.mean(seg * seg))
        if seg_n == 0.0:
            return 0.0
        return abs(float(np.mean(mic * seg) / np.sqrt(mic_n * seg_n)))

    def _estimate_delay(self, mic, ref):
        mic_n = float(np.mean(mic * mic))
        if mic_n == 0.0:
            return 0, 0.0
        limit = min(MAX_DELAY_FRAMES, len(ref) - len(mic))
        if limit < 0:
            return 0, 0.0
        best_d, best_s = 0, -1.0
        for d in range(0, limit + 1, DELAY_STEP):
            s = self._score_delay(mic, mic_n, ref, d)
            if s > best_s:
                best_s, best_d = s, d
        # Fine pass around the coarse peak (peak can be samples wide).
        for d in range(max(0, best_d - FINE_WINDOW),
                       min(limit, best_d + FINE_WINDOW) + 1, FINE_STEP):
            s = self._score_delay(mic, mic_n, ref, d)
            if s > best_s:
                best_s, best_d = s, d
        return best_d, max(best_s, 0.0)

    def _nlms_step(self, mic, ref):
        hist = int(FILTER_TAPS + len(mic))
        if len(ref) < hist:
            pad = np.zeros(hist - len(ref))
            buf = np.concatenate([pad, np.asarray(ref, dtype=np.float64)])
        else:
            buf = np.asarray(ref[-hist:], dtype=np.float64)
        # X[i] = ref window ending at sample i (most recent FILTER_TAPS).
        stride = buf.strides[0]
        X = np.lib.stride_tricks.as_strided(
            buf, shape=(len(mic), FILTER_TAPS),
            strides=(stride, stride))[:, ::-1].copy()
        echo_pred = X @ self._filter
        residual = mic - echo_pred
        norm = float(np.sum(X * X)) + 1e-9
        self._filter = (FILTER_LEAK * self._filter
                        + (NLMS_MU / norm) * (X.T @ residual))
        return echo_pred, residual

    def summary(self) -> str:
        s = self.stats
        total = s["frames_passed"] + s["frames_suppressed"]
        return (f"echo-suppressor: passed={s['frames_passed']} "
                f"suppressed={s['frames_suppressed']} "
                f"no-reference={s['frames_no_reference']} "
                f"total={total}")
