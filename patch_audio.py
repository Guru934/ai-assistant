import re

with open("/home/guru/cat-talker/src/cat_talker/audio.py", "r") as f:
    content = f.read()

# Make a helper method to recreate out_stream
recreate_out_stream = """
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
"""
if "_recreate_out_stream" not in content:
    content = content.replace("    def _play_audio(self):", recreate_out_stream + "\n    def _play_audio(self):")

# Modify play_audio loop to retry on failure
old_play_audio_try = """            try:
                self.out_stream.write(chunk)
            except Exception as e:
                logger.error(f"Audio write error: {e}")"""
                
new_play_audio_try = """            success = False
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
                        logger.error(f"Failed to recreate output stream: {ex}")"""
                        
content = content.replace(old_play_audio_try, new_play_audio_try)

# Similarly, for in_stream we need to recreate if it dies. A good place is a separate thread or just checking it in play_audio.
watchdog = """
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
"""

if "_audio_watchdog" not in content:
    content = content.replace("    def queue_output", watchdog + "\n    def queue_output")

# Start watchdog in __init__
old_init_end = """        self.out_thread = threading.Thread(target=self._play_audio, daemon=True)
        self.out_thread.start()"""
        
new_init_end = """        self.out_thread = threading.Thread(target=self._play_audio, daemon=True)
        self.out_thread.start()
        self.watchdog_thread = threading.Thread(target=self._audio_watchdog, daemon=True)
        self.watchdog_thread.start()"""
        
content = content.replace(old_init_end, new_init_end)

with open("/home/guru/cat-talker/src/cat_talker/audio.py", "w") as f:
    f.write(content)
