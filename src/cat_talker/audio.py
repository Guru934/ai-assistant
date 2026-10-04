import asyncio
import queue
import threading
import struct
import pyaudio
from ctypes import CFUNCTYPE, c_char_p, c_int, cdll

from cat_talker.logging_config import get_logger
from cat_talker.echo_suppress import (
    EchoSuppressor,
    discover_monitor_source,
    BYTES_PER_CHUNK,
    CHUNK_FRAMES,
)

logger = get_logger("cat_talker.audio")

# --- SUPPRESS ALSA C-LEVEL ERRORS (pcm_dsnoop unable to open slave, etc) ---
try:
    ERROR_HANDLER_FUNC = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)
    def py_error_handler(filename, line, function, err, fmt):
        pass
    c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)
    asound = cdll.LoadLibrary('libasound.so.2')
    asound.snd_lib_error_set_handler(c_error_handler)
except Exception:
    pass
# --------------------------------------------------------------------------

class AudioInterface:
    def __init__(self):
        self.pyaudio = pyaudio.PyAudio()
        self.audio_in_queue = asyncio.Queue()
        self.audio_out_queue = queue.Queue()
        self.loop = asyncio.get_event_loop()
        self.volume_cb = None

        self.is_playing = False
        self.mic_active = False
        self._running = True
        self._loop_closed = False
        self._closed = False
        # Input-stream ownership: exactly one authoritative in_stream.
        # Recreation (watchdog) takes _stream_lock so a dying stream is
        # stopped+closed before its replacement opens, the callback is
        # never attached twice, and every open/close is logged with a
        # stream id for ownership audits.
        self._stream_lock = threading.Lock()
        self._stream_id = 0

        self.in_stream = self.pyaudio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=16000,
            input=True,
            frames_per_buffer=CHUNK_FRAMES,
            stream_callback=self._mic_callback
        )
        self._stream_id = 1
        logger.info("mic stream OPEN stream_id=1 reason=initial")

        self.out_stream = self.pyaudio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=24000,
            output=True,
            frames_per_buffer=1024
        )

        self.out_thread = threading.Thread(target=self._play_audio, daemon=True)
        self.out_thread.start()
        self.watchdog_thread = threading.Thread(target=self._audio_watchdog, daemon=True)
        self.watchdog_thread.start()

        # Reference-based echo suppression (speaker/media audio leaking
        # into the mic). The suppressor compares each mic chunk against
        # what is currently playing on the speakers (default-sink
        # monitor) and drops echo-dominated chunks. Fail-open: without a
        # reference, or on any error, mic audio passes through exactly
        # as before. Assistant-turn muting (is_playing) stays primary.
        self._echo_enabled = True
        try:
            from cat_talker.config import get_echo_suppress
            self._echo_enabled = bool(get_echo_suppress())
        except Exception:
            pass
        self.echo = EchoSuppressor(enabled=self._echo_enabled)
        self._monitor_proc = None
        self._monitor_stop = threading.Event()
        self._last_echo_decision = None
        self._playback_chunks = 0
        # DEBUG-only live sync telemetry (counts + monotonic timestamps;
        # no audio content). Compares the mic callback clock against the
        # reference reader clock to expose buffering offsets/drift/bursts.
        # Created lazily via _sync_state(): the mic callback can fire
        # before __init__ finishes, and __new__-built test instances skip
        # __init__ entirely.
        self._sync = self._new_sync()
        if self._echo_enabled:
            self.monitor_thread = threading.Thread(
                target=self._monitor_reference_loop, daemon=True)
            self.monitor_thread.start()
        else:
            logger.info("mic input: echo suppression disabled by config")
        logger.info(f"mic input: capture open (echo suppression "
                    f"{'on' if self._echo_enabled else 'off'})")

    @staticmethod
    def _new_sync():
        return {
            "mic_chunks": 0, "mic_samples": 0, "mic_first_t": None,
            "mic_last_t": None, "mic_sizes": {},
            "ref_reads": 0, "ref_samples": 0, "ref_first_t": None,
            "ref_last_t": None, "ref_last_n": 0, "ref_max_gap": 0.0,
            "ref_short_reads": 0,
        }

    def _sync_state(self):
        s = getattr(self, "_sync", None)
        if s is None:
            s = self._sync = self._new_sync()
        return s

    def _monitor_reference_loop(self):
        """Feed the echo suppressor with speaker output (sink monitor).

        Runs `pw-record` on the default sink's monitor so the reference
        contains everything hitting the speakers - assistant TTS, YouTube,
        system sounds - regardless of source app. Self-healing with
        backoff; if no reference is available the suppressor simply
        passes mic audio through (fail-open).
        """
        import subprocess
        import time
        while not self._monitor_stop.is_set() and self._running:
            monitor = discover_monitor_source()
            if monitor is None:
                logger.warning("mic input: no sink monitor found (pactl "
                               "missing?) - echo suppression inactive, "
                               "mic passes through")
                self._monitor_stop.wait(30.0)
                continue
            try:
                logger.info(f"mic input: echo reference from '{monitor}'")
                self._monitor_proc = subprocess.Popen(
                    ["pw-record", "--target", monitor,
                     "--rate", "16000", "--channels", "1",
                     "--format", "s16", "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                stdout = self._monitor_proc.stdout
                if stdout is None:
                    logger.warning("mic input: echo reference has no "
                                   "stdout, retrying...")
                else:
                    import time as _t
                    prev_t = None
                    # Small reads (128 frames): pw-record delivers a smooth
                    # stream, so small reads complete every ~8ms. Large
                    # reads (e.g. 2048 frames) instead batch delivery into
                    # 128ms lumps, swinging the true echo lag by +-1024
                    # samples at the read cadence - far outside the
                    # 256-tap filter span - which the estimator aliases and
                    # the filter can never track (measured live:
                    # suppressed=0 with reference present). Never ask for
                    # more than the DSP can absorb per mic chunk.
                    while not self._monitor_stop.is_set() and self._running:
                        data = stdout.read(256)
                        if not data:
                            break
                        now = _t.monotonic()
                        n = len(data) // 2
                        s = self._sync_state()
                        s["ref_reads"] += 1
                        s["ref_samples"] += n
                        if s["ref_first_t"] is None:
                            s["ref_first_t"] = now
                        if prev_t is not None:
                            s["ref_max_gap"] = max(s["ref_max_gap"],
                                                   now - prev_t)
                        prev_t = now
                        s["ref_last_t"] = now
                        s["ref_last_n"] = n
                        if n < BYTES_PER_CHUNK:
                            s["ref_short_reads"] += 1
                        self.echo.feed_reference(bytes(data))
                logger.warning("mic input: echo reference stream ended, "
                               "retrying...")
            except (OSError, subprocess.SubprocessError) as e:
                logger.warning(f"mic input: cannot start echo reference "
                               f"(pw-record missing?): {e} - mic passes through")
            except Exception as e:
                logger.error(f"mic input: echo reference error: {e}")
            finally:
                proc, self._monitor_proc = self._monitor_proc, None
                if proc is not None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            self._monitor_stop.wait(5.0)

    def pause_input(self):
        """Sleeping: drop mic chunks at the callback (counted)."""
        self._input_paused = True

    def resume_input(self):
        self._input_paused = False

    def _mic_callback(self, in_data, frame_count, time_info, status):
        if not self._running or self._loop_closed:
            return (None, pyaudio.paComplete)
        if getattr(self, "_input_paused", False):
            dropped = getattr(self, "_paused_dropped", 0) + 1
            self._paused_dropped = dropped
            return (None, pyaudio.paContinue)
        try:
            if not self.is_playing:
                self.mic_active = True
                out_data = in_data
                import time as _t
                _now = _t.monotonic()
                _s = self._sync_state()
                _s["mic_chunks"] += 1
                _s["mic_samples"] += len(in_data) // 2
                if _s["mic_first_t"] is None:
                    _s["mic_first_t"] = _now
                _s["mic_last_t"] = _now
                _s["mic_sizes"][len(in_data)] = (
                    _s["mic_sizes"].get(len(in_data), 0) + 1)
                if self._echo_enabled and len(in_data) == BYTES_PER_CHUNK:
                    try:
                        out_data, decision, reason = self.echo.process(in_data)
                        # DEBUG-only numeric diagnostics (no audio content).
                        logger.debug(f"mic input: {self.echo.snapshot_line()}")
                        # DEBUG-only live sync record: wall-clock
                        # relationship between this mic chunk and the
                        # newest reference sample.
                        try:
                            _in_q = self.audio_in_queue.qsize()
                        except Exception:
                            _in_q = -1
                        _ref_ahead = (_s["ref_samples"] - _s["mic_samples"])
                        _ref_stale = ((_now - _s["ref_last_t"]) * 1000.0
                                      if _s["ref_last_t"] is not None else -1.0)
                        logger.debug(
                            f"live sync: mic_t={_now:.3f} "
                            f"ref_ahead_samples={_ref_ahead} "
                            f"ref_stale_ms={_ref_stale:.1f} "
                            f"ref_reads={_s['ref_reads']} "
                            f"ref_max_gap={_s['ref_max_gap']:.3f}s "
                            f"mic_in_q={_in_q}")
                        if decision != self._last_echo_decision:
                            self._last_echo_decision = decision
                            logger.debug(f"mic input: echo decision -> "
                                         f"{decision} ({reason})")
                        if decision != "pass":
                            logger.debug("mic input: echo-detected chunk "
                                         "suppressed (speaker/media audio, "
                                         "not user speech)")
                    except Exception as e:
                        logger.error(f"mic input: suppressor error ({e}) - "
                                     f"passing audio through")
                        out_data = in_data
                self.loop.call_soon_threadsafe(self.audio_in_queue.put_nowait, out_data)
            else:
                silence = b'\x00' * len(in_data)
                self.loop.call_soon_threadsafe(self.audio_in_queue.put_nowait, silence)
        except (RuntimeError, AttributeError):
            # Event loop closed or invalid, stop the stream gracefully
            self._loop_closed = True
            return (None, pyaudio.paComplete)
        return (None, pyaudio.paContinue)


    def _recreate_out_stream(self):
        try:
            self.out_stream.stop_stream()
            self.out_stream.close()
        except:
            pass
        self.out_stream = self.pyaudio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=24000,
            output=True,
            frames_per_buffer=1024
        )

    def suppress_mic(self):
        """Assistant output started: mute the mic until release_mic().

        Output-level (not chunk-level): the agent calls this once when the
        model starts responding, so capture stays muted across ALL audio
        chunks of the turn with no per-chunk toggling. Plain-bool writes
        are atomic; readers are the mic callback and this module's threads.
        """
        self.is_playing = True

    def release_mic(self):
        """Model turn finished and output drained: capture may resume."""
        self.is_playing = False

    def _play_audio(self):
        while True:
            chunk = self.audio_out_queue.get()
            if chunk is None:
                break

            # NOTE: playback never touches is_playing. Mic suppression is
            # owned by the agent at output level (suppress_mic/release_mic)
            # so state cannot flap between individual chunks.
            if self.volume_cb:
                import struct
                import numpy as np
                count = len(chunk) // 2
                if count > 0:
                    try:
                        shorts = struct.unpack(f"{count}h", chunk)
                        rms = sum(s*s for s in shorts) / count
                        vol_db = min(1.0, (rms ** 0.5) * 100.0 / 32768.0)

                        # FFT processing for visualizer
                        audio_data = np.array(shorts, dtype=np.float32) / 32768.0
                        fft_result = np.abs(np.fft.rfft(audio_data))

                        # Map into 64 bins
                        target_bins = 64
                        if len(fft_result) >= target_bins:
                            # Simple downsampling by averaging blocks
                            block_size = max(1, len(fft_result) // target_bins)
                            # Only take the first target_bins * block_size elements
                            truncated = fft_result[:target_bins * block_size]
                            binned = truncated.reshape(-1, block_size).mean(axis=1)
                            # Normalize slightly to make it look good
                            freq_bins = np.clip(binned * 10.0, 0, 1.0).tolist()
                        else:
                            freq_bins = [0.0] * target_bins

                        # Bass calculation (average of first 6 bins)
                        bass_scale = 1.0
                        if len(freq_bins) >= 6:
                            bass_avg = sum(freq_bins[:6]) / 6.0
                            # Map bass avg (0-1) to (1.0-1.15)
                            bass_scale = 1.0 + (bass_avg * 0.15)

                        self.volume_cb(vol_db, bass_scale, freq_bins)
                    except Exception as e:
                        logger.error(f"Audio volume calc error: {e}")

            success = False
            retries = 3
            while not success and retries > 0:
                try:
                    if self.out_stream is None:
                        self._recreate_out_stream()
                    self.out_stream.write(chunk)
                    success = True
                except Exception as e:
                    logger.error(f"Audio write error ({retries} retries left): {e}")
                    retries -= 1
                    import time
                    time.sleep(0.1)
                    try:
                        self._recreate_out_stream()
                    except Exception as ex:
                        logger.error(f"Failed to recreate output stream: {ex}")

            self.audio_out_queue.task_done()


    def _audio_watchdog(self):
        import time
        ticks = 0
        while self._running:
            time.sleep(2)
            ticks += 1
            # Periodic echo-suppressor summary (~every 30s): distinguishes
            # mic/user input passed vs speaker/media input suppressed.
            if self._echo_enabled and ticks % 15 == 0:
                logger.info(f"mic input: {self.echo.summary()} "
                            f"(assistant playback chunks={self._playback_chunks})")
            if self._closed:
                break
            # Single authoritative input stream: recreate only if dead,
            # under lock, exactly once (see helper for ordering).
            try:
                self._maybe_recreate_input_stream()
            except Exception:
                pass

    def _maybe_recreate_input_stream(self) -> str:
        """Recreate a dead/inactive input stream under lock, exactly once.

        Returns "ok" (recreated), "healthy" (no action), or "skipped"
        (closed/not running). Order inside the lock: stop old, close old,
        open exactly one replacement, bump the stream id. Never raises.
        """
        if getattr(self, "_closed", True) or not getattr(self, "_running", False):
            return "skipped"
        lock = getattr(self, "_stream_lock", None)
        if lock is None:
            import threading as _threading
            lock = self._stream_lock = _threading.Lock()
        try:
            active = bool(self.in_stream.is_active())
        except Exception:
            active = False
        if active:
            return "healthy"
        if not self._running or getattr(self, "_closed", False):
            return "skipped"
        with lock:
            try:
                active = bool(self.in_stream.is_active())
            except Exception:
                active = False
            if active:
                return "healthy"
            if not self._running or getattr(self, "_closed", False):
                return "skipped"
            logger.warning("Input audio stream died or inactive, attempting to reconnect...")
            try:
                try:
                    self.in_stream.stop_stream()
                    self.in_stream.close()
                except Exception:
                    pass
                self._loop_closed = False  # Reset loop closed flag
                self.in_stream = self.pyaudio.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=16000,
                    input=True,
                    frames_per_buffer=CHUNK_FRAMES,
                    stream_callback=self._mic_callback
                )
                self._stream_id = getattr(self, "_stream_id", 0) + 1
                logger.info("mic stream OPEN stream_id=%d reason=reconnect",
                            self._stream_id)
                return "ok"
            except Exception as e:
                logger.error(f"Failed to recreate input stream: {e}")
                return "skipped"
        return "skipped"

    def queue_output(self, pcm_data: bytes):
        self._playback_chunks += 1
        if self._playback_chunks % 200 == 1:
            logger.debug(f"assistant playback: {self._playback_chunks} "
                         f"chunks queued (speaker output, excluded from mic)")
        self.audio_out_queue.put(pcm_data)

    def clear_output_queue(self):
        while not self.audio_out_queue.empty():
            try:
                self.audio_out_queue.get_nowait()
                self.audio_out_queue.task_done()
            except queue.Empty:
                break

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._running = False
        self._loop_closed = True
        try:
            self._monitor_stop.set()
        except AttributeError:
            pass
        proc = getattr(self, "_monitor_proc", None)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        if getattr(self, "_echo_enabled", False):
            logger.info(f"mic input: closing ({self.echo.summary()})")
        self.audio_out_queue.put(None)
        try:
            self.in_stream.stop_stream()
            self.in_stream.close()
            logger.info("mic stream CLOSE stream_id=%s",
                        getattr(self, "_stream_id", "?"))
        except Exception:
            pass
        try:
            self.out_stream.stop_stream()
            self.out_stream.close()
        except Exception:
            pass
        try:
            self.pyaudio.terminate()
        except Exception:
            pass
