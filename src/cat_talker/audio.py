import asyncio
import queue
import threading
import struct
import pyaudio
from ctypes import CFUNCTYPE, c_char_p, c_int, cdll

from cat_talker.logging_config import get_logger

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

        self.in_stream = self.pyaudio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=16000,
            input=True,
            frames_per_buffer=1024,
            stream_callback=self._mic_callback
        )

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

    def _mic_callback(self, in_data, frame_count, time_info, status):
        if not self._running or self._loop_closed:
            return (None, pyaudio.paComplete)
        try:
            if not self.is_playing:
                self.mic_active = True
                self.loop.call_soon_threadsafe(self.audio_in_queue.put_nowait, in_data)
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
        while self._running:
            time.sleep(2)
            if self._closed:
                break
            # Check if in_stream is active
            needs_recreate = False
            try:
                if not self.in_stream.is_active():
                    needs_recreate = True
            except:
                needs_recreate = True
                
            if needs_recreate and self._running:
                logger.warning("Input audio stream died or inactive, attempting to reconnect...")
                try:
                    try:
                        self.in_stream.stop_stream()
                        self.in_stream.close()
                    except:
                        pass
                    self._loop_closed = False # Reset loop closed flag
                    self.in_stream = self.pyaudio.open(
                        format=pyaudio.paInt16,
                        channels=1,
                        rate=16000,
                        input=True,
                        frames_per_buffer=1024,
                        stream_callback=self._mic_callback
                    )
                except Exception as e:
                    logger.error(f"Failed to recreate input stream: {e}")

    def queue_output(self, pcm_data: bytes):
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
        self.audio_out_queue.put(None)
        try:
            self.in_stream.stop_stream()
            self.in_stream.close()
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
