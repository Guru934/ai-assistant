"""Echo-suppressor tests: pure DSP with synthetic signals, no hardware.

Conventions: int16 mono 16k PCM, 1024-frame chunks (see echo_suppress).
"""

import subprocess

import numpy as np

from cat_talker.echo_suppress import (
    BYTES_PER_CHUNK,
    CHUNK_FRAMES,
    MIC_DELAY_CHUNKS,
    TRIM_AT,
    EchoSuppressor,
    discover_monitor_source,
)

RATE = 16000


def _music_like(n, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(n) / RATE
    sig = (0.4 * np.sin(2 * np.pi * 440 * t)
           + 0.3 * np.sin(2 * np.pi * 880 * t + 1.0)
           + 0.1 * rng.standard_normal(n))
    return (np.clip(sig, -1, 1) * 30000).astype(np.int16)


def _chunks(a):
    n = (len(a) // CHUNK_FRAMES) * CHUNK_FRAMES
    return [a[i:i + CHUNK_FRAMES].tobytes()
            for i in range(0, n, CHUNK_FRAMES)]


def _run(sup, ref_pcm, mic_pcm):
    out = []
    for rc, mc in zip(_chunks(ref_pcm), _chunks(mic_pcm)):
        sup.feed_reference(rc)
        out.append(sup.process(mc))
    return out


def _run_aligned(sup, ref_pcm, mic_pcm):
    """Pipeline decisions keyed by DECIDED mic-chunk index.

    The suppressor holds mic chunks back MIC_DELAY_CHUNKS callbacks, so
    call i (0-based) returns the decision for chunk i - MIC_DELAY_CHUNKS
    once warmed; earlier calls are delay-fill passthrough. Returns
    ({index: (out, decision, reason)}, mic_chunks).
    """
    decided = {}
    mcs = _chunks(mic_pcm)
    for i, (rc, mc) in enumerate(zip(_chunks(ref_pcm), mcs)):
        sup.feed_reference(rc)
        out, d, r = sup.process(mc)
        if i >= MIC_DELAY_CHUNKS:
            decided[i - MIC_DELAY_CHUNKS] = (out, d, r)
    return decided, mcs


def test_pure_speaker_echo_is_suppressed():
    """Mic = delayed/attenuated speaker audio -> suppressed after warmup."""
    ref = _music_like(32000)
    rng = np.random.default_rng(0)
    echo = np.concatenate([np.zeros(1500), ref.astype(float) * 0.5])[:32000]
    echo += rng.standard_normal(32000) * 50
    mic = np.clip(echo, -32768, 32767).astype(np.int16)

    sup = EchoSuppressor()
    decided, mcs = _run_aligned(sup, ref, mic)
    decisions = [d for (_, d, _) in decided.values()]
    suppressed = decisions.count("suppress")
    assert suppressed >= len(decisions) // 2, decisions
    # Suppressed output is silence, same length (downstream timing intact).
    for j, (pcm, d, _) in decided.items():
        if d == "suppress":
            assert pcm == b"\x00" * len(mcs[j])


def test_user_speech_over_media_passes_through_untouched():
    """Loud near-end signal on top of echo -> every loud chunk passes."""
    ref = _music_like(32000)
    rng = np.random.default_rng(1)
    t = np.arange(32000) / RATE
    echo = np.concatenate([np.zeros(1500), ref.astype(float) * 0.5])[:32000]
    echo += rng.standard_normal(32000) * 50
    gate = (np.sin(2 * np.pi * 3 * t) > 0).astype(float)
    voice = 0.8 * np.sin(2 * np.pi * 220 * t) * gate * 20000
    mic = np.clip(echo + voice, -32768, 32767).astype(np.int16)

    sup = EchoSuppressor()
    decided, mcs = _run_aligned(sup, ref, mic)
    vcs = _chunks((voice).astype(np.int16))
    checked = 0
    for j, (out, decision, _) in decided.items():
        vrms = float(np.sqrt(np.mean(
            np.frombuffer(vcs[j], dtype=np.int16).astype(float) ** 2)))
        if vrms > 5000:
            checked += 1
            assert decision == "pass", "user speech chunk suppressed"
            assert out == mcs[j], "user speech chunk altered"
    assert checked > 0, "test signal had no loud voice chunks"


def _band_limited_noise(n, seed=0):
    """Non-periodic speech-band-ish signal (lowpassed noise)."""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n)
    kernel = np.hanning(65)
    kernel /= kernel.sum()
    y = np.convolve(x, kernel, mode="same")
    return (np.clip(y / max(1e-9, np.abs(y).max()), -1, 1)
            * 25000).astype(np.int16)


def test_long_delay_nonperiodic_echo_is_suppressed():
    """Regression (measured live: true echo lag ~1365 samples, 85ms).

    The NLMS filter only spans 256 taps, so the reference must be
    bulk-delay compensated before prediction. A previous revision fed
    the raw recent reference: correlation was ~0.99 yet echo prediction
    stayed near zero and suppression never fired. Non-periodic signal so
    a misaligned filter cannot cheat via periodicity.
    """
    # 8s of continuous media: the adaptive filter needs ~2-3s to
    # converge (as on hardware), so the signal must outlast warmup.
    n = 128000
    ref = _band_limited_noise(n, seed=4)
    true_delay = 1365  # beyond FILTER_TAPS, as measured on hardware
    rng = np.random.default_rng(5)
    echo = np.concatenate([np.zeros(true_delay),
                           ref.astype(float) * 0.6])[:n]
    echo += rng.standard_normal(n) * 60
    mic = np.clip(echo, -32768, 32767).astype(np.int16)

    sup = EchoSuppressor()
    decided, _ = _run_aligned(sup, ref, mic)
    decisions = [d for (_, d, _) in decided.values()]
    # Steady state (past convergence) must be mostly suppressed.
    tail = decisions[-20:]
    assert tail.count("suppress") >= 15, tail
    # The estimator must have found the real lag, not a periodic alias.
    # It measures decided-chunk vs current-history, so the mic delay line
    # (MIC_DELAY_CHUNKS held-back chunks) is part of the measured lag.
    expect = true_delay + MIC_DELAY_CHUNKS * CHUNK_FRAMES
    assert abs(sup._delay - expect) <= 64, (sup._delay, expect)


def _drive_decision(sup, ref_chunk, mic_chunk, calls=None):
    """Feed/process until the delay line yields a real decision.

    Returns the first decided (out, decision, reason), which applies to
    the first mic chunk submitted.
    """
    n = calls or (MIC_DELAY_CHUNKS + 1)
    last = (mic_chunk, "pass", "delay-fill")
    for _ in range(n):
        sup.feed_reference(ref_chunk)
        last = sup.process(mic_chunk)
    return last


def test_live_read_quantum_with_startup_skew_suppresses():
    """Regression for the live suppressed=0 failure with reference present.

    Live, the monitor reader delivers 2048-frame quanta while mic chunks
    are 512 frames, and the monitor starts ~2s before the first mic
    callback. Pre-fix the estimator faced a +-1024-sample sawtooth with
    the needed sample ~500 samples in the future: correlation flickered,
    the filter never converged, suppression stayed zero despite a loud,
    highly-correlated reference. The architecture must pace consumption
    to mic chunks, trim the startup skew, and decide mic chunks late
    enough for the slower reference path to arrive.
    """
    skew = 31744  # measured live startup skew (samples)
    n = 128000
    ref = _band_limited_noise(n + skew + 4096, seed=8)
    rng = np.random.default_rng(9)
    # Mic content matches ref 31728 samples ahead (measured live content
    # offset, acoustic lag included), gain 0.6 + sensor noise.
    off = 31728
    echo = np.concatenate([np.zeros(0),
                           ref[off:off + n].astype(float) * 0.6])
    echo += rng.standard_normal(n) * 60
    mic = np.clip(echo, -32768, 32767).astype(np.int16)

    sup = EchoSuppressor()
    fed = 0

    def feed_upto(idx):
        nonlocal fed
        while fed < idx:  # 2048-frame read quantum, as live
            nxt = min(fed + 2048, idx)
            sup.feed_reference(ref[fed:nxt].tobytes())
            fed = nxt

    feed_upto(skew)
    mcs = _chunks(mic)
    decided = {}
    delays = []
    for i, mc in enumerate(mcs):
        # Lumpy delivery: a 2048-frame burst every 4th chunk (same
        # per-chunk average and burst ratio as the old 4096-byte reader
        # produced at 1024-frame mic chunks; smooth readers are a subset:
        # bursts of one quantum each chunk). Average rate still matches
        # the mic.
        if i % 4 == 0:
            feed_upto(skew + (i + 4) * CHUNK_FRAMES + 64)
        out, d, r = sup.process(mc)
        delays.append(sup._delay)
        if i >= MIC_DELAY_CHUNKS:
            decided[i - MIC_DELAY_CHUNKS] = (out, d, r)
    decisions = [d for (_, d, _) in decided.values()]
    # Steady state must suppress (convergence needs seconds of media).
    tail = decisions[-20:]
    assert tail.count("suppress") >= 12, tail
    # Estimator locked (not wandering across the range each re-estimate).
    # Absolute value is geometry-dependent; stability is the invariant.
    second_half = delays[len(delays) // 2:]
    assert max(second_half) - min(second_half) <= 256, second_half[::10]
    # Staging stays bounded: skew trimmed, quantum wobble absorbed.
    assert sup.last_snapshot.get("fifo", 0) < TRIM_AT, sup.last_snapshot


def test_quiet_reference_passes_through():
    """Nothing playing -> nothing can be echo -> pass."""
    sup = EchoSuppressor()
    quiet = np.zeros(CHUNK_FRAMES, dtype=np.int16).tobytes()
    mic = _music_like(CHUNK_FRAMES, seed=7).tobytes()
    # 20 identical rounds: history fills past reference_ready (half a
    # second of reference = 8000 samples takes 16 chunks at 512 frames)
    # while the decided chunk (always the same bytes here) sees quiet
    # reference.
    out, decision, reason = _drive_decision(sup, quiet, mic, calls=20)
    assert decision == "pass"
    assert out == mic
    assert "reference-quiet" in reason


def test_no_reference_yet_is_fail_open():
    """Fresh suppressor (monitor not streaming yet) passes everything."""
    sup = EchoSuppressor()
    assert not sup.reference_ready
    mic = _music_like(CHUNK_FRAMES, seed=3).tobytes()
    out, decision, reason = _drive_decision(sup, b"", mic)
    assert (out, decision) == (mic, "pass")
    assert "no-reference" in reason
    assert sup.stats["frames_no_reference"] == 1


def test_disabled_suppressor_is_passthrough():
    sup = EchoSuppressor(enabled=False)
    mic = _music_like(CHUNK_FRAMES, seed=5).tobytes()
    out, decision, reason = sup.process(mic)
    assert (out, decision, reason) == (mic, "pass", "disabled")


def test_unexpected_chunk_size_passes_through():
    sup = EchoSuppressor()
    odd = b"\x01\x02\x03\x04"
    out, decision, _ = sup.process(odd)
    assert (out, decision) == (odd, "pass")


def test_silent_mic_passes_through():
    sup = EchoSuppressor()
    for _ in range(20):
        sup.feed_reference(_music_like(CHUNK_FRAMES, seed=9).tobytes())
    silent = b"\x00" * BYTES_PER_CHUNK
    out, decision, _ = sup.process(silent)
    assert (out, decision) == (silent, "pass")


def test_delay_estimator_finds_bulk_delay():
    """Non-stationary reference: estimated delay near the true delay."""
    rng = np.random.default_rng(42)
    n = 32000
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 0.7 * np.arange(n) / RATE
                                  + rng.standard_normal(n) * 0.3)
    ref = (np.clip(rng.standard_normal(n) * envelope, -1, 1)
           * 25000).astype(np.int16)
    true_delay = 2000
    mic = np.concatenate([np.zeros(true_delay),
                          ref.astype(float) * 0.5])[:n]
    mic = np.clip(mic, -32768, 32767).astype(np.int16)

    sup = EchoSuppressor()
    silent = b"\x00" * BYTES_PER_CHUNK
    # Drive transfers with silent mic input so the paced history fills
    # exactly as in live use (feed quantum == transfer quantum here).
    for rc in _chunks(ref):
        sup.feed_reference(rc)
        sup.process(silent)
    mic_f = (np.frombuffer(_chunks(mic)[-1], dtype=np.int16).astype(np.float64)
             / 32768.0)
    ref_hist = np.asarray(sup._ref, dtype=np.float64)
    delay, ncorr = sup._estimate_delay(mic_f, ref_hist)
    assert ncorr > 0.8, ncorr
    assert abs(delay - true_delay) <= 64, (delay, true_delay)


def test_summary_reports_counts():
    sup = EchoSuppressor()
    sup.stats["frames_passed"] = 3
    sup.stats["frames_suppressed"] = 2
    text = sup.summary()
    assert "passed=3" in text and "suppressed=2" in text


def test_mic_callback_wiring_suppresses_echo_passes_voice():
    """The AudioInterface hook (no hardware): echo chunks leave the
    callback as silence, voice chunks leave byte-identical."""
    import queue as _queue
    from cat_talker.audio import AudioInterface

    class FakeLoop:
        def __init__(self):
            self.calls = []

        def call_soon_threadsafe(self, cb, *args):
            self.calls.append((cb, args))

    loop = FakeLoop()
    iface = AudioInterface.__new__(AudioInterface)
    iface._running = True
    iface._loop_closed = False
    iface.is_playing = False
    iface.mic_active = False
    iface._echo_enabled = True
    iface.echo = EchoSuppressor(enabled=True)
    iface._last_echo_decision = None
    iface.loop = loop
    iface.audio_in_queue = _queue.Queue()

    ref = _music_like(32000)
    rng = np.random.default_rng(11)
    echo = np.concatenate([np.zeros(1500), ref.astype(float) * 0.5])[:32000]
    echo += rng.standard_normal(32000) * 50
    mic_echo = np.clip(echo, -32768, 32767).astype(np.int16)
    t = np.arange(32000) / RATE
    voice = (0.9 * np.sin(2 * np.pi * 220 * t) * 20000)
    mic_voice = np.clip(echo + voice, -32768, 32767).astype(np.int16)

    ref_chunks = _chunks(ref)
    echo_chunks = _chunks(mic_echo)
    voice_chunks = _chunks(mic_voice)
    for rc, mc in zip(ref_chunks, echo_chunks):
        iface.echo.feed_reference(rc)
        iface._mic_callback(mc, CHUNK_FRAMES, None, None)
    forwarded = [args[0] for (_, args) in loop.calls]
    assert forwarded, "mic callback forwarded nothing"
    # After warmup the echo-only tail must go out as silence.
    assert forwarded[-1] == b"\x00" * BYTES_PER_CHUNK
    assert forwarded[-2] == b"\x00" * BYTES_PER_CHUNK

    loop.calls.clear()
    # Voice rounds flush the pre-voice echo chunks still in the delay
    # line; forwarded[K:] are the decisions for voice chunks 0..4.
    from cat_talker.echo_suppress import MIC_DELAY_CHUNKS as _K
    n = 5 + _K
    v9 = (voice_chunks[-5:] * 3)[:n]
    r9 = (ref_chunks[-5:] * 3)[:n]
    for rc, mc in zip(r9, v9):
        iface.echo.feed_reference(rc)
        iface._mic_callback(mc, CHUNK_FRAMES, None, None)
    assert len(loop.calls) == n
    for (_, args), mc in zip(loop.calls[_K:], v9[:5]):
        assert args[0] == mc, "user voice chunk altered by mic path"


def test_discover_monitor_parses_default_sink(monkeypatch):
    class R:
        returncode = 0
        stdout = ("Server Name: PulseAudio (on PipeWire 1.6.9)\n"
                  "Default Sink: my_sink_name\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    assert discover_monitor_source() == "my_sink_name.monitor"


def test_discover_monitor_none_without_pactl(monkeypatch):
    def boom(*a, **k):
        raise OSError("no pactl")
    monkeypatch.setattr(subprocess, "run", boom)
    assert discover_monitor_source() is None
