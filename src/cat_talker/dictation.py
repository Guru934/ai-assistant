import io
import wave
import threading
import time
import subprocess
import shutil
import pyaudio
from google import genai
from google.genai import types

from cat_talker.tools import get_clipboard, set_clipboard, get_active_window
from cat_talker.logging_config import get_logger
from cat_talker.earcons import play_earcon

logger = get_logger("cat_talker.dictation")

class DictationManager:
    def __init__(self, state_callback):
        """
        state_callback: function that accepts a state string ('dictating', 'thinking', 'idle')
        """
        self.pyaudio_instance = pyaudio.PyAudio()
        self.stream = None
        self.frames = []
        self.is_recording = False
        self.client = genai.Client()
        self.state_callback = state_callback

    def start(self):
        if self.is_recording:
            return
            
        logger.info("🎤 Dictation started...")
        self.frames = []
        self.is_recording = True
        self.state_callback("dictating")
        
        play_earcon("blip")
        
        try:
            self.stream = self.pyaudio_instance.open(
                format=pyaudio.paInt16, 
                channels=1, 
                rate=16000, 
                input=True, 
                frames_per_buffer=1024
            )
            self._thread = threading.Thread(target=self._record, daemon=True)
            self._thread.start()
        except Exception as e:
            logger.error(f"Failed to start dictation stream: {e}")
            play_earcon("fail")
            self.is_recording = False
            self.state_callback("idle")

    def _record(self):
        while self.is_recording and self.stream:
            try:
                data = self.stream.read(1024, exception_on_overflow=False)
                self.frames.append(data)
            except IOError as e:
                # Disconnected? Try to reconnect audio stream
                logger.error(f"Dictation recording IOError: {e}, attempting to restart stream.")
                try:
                    self.stream.stop_stream()
                    self.stream.close()
                except Exception:
                    pass
                time.sleep(0.5)
                try:
                    self.stream = self.pyaudio_instance.open(
                        format=pyaudio.paInt16, 
                        channels=1, 
                        rate=16000, 
                        input=True, 
                        frames_per_buffer=1024
                    )
                except Exception as ex:
                    logger.error(f"Failed to restart dictation stream: {ex}")
                    self.is_recording = False
                    self.state_callback("idle")
                    play_earcon("fail")
                    break
            except Exception as e:
                logger.error(f"Dictation recording error: {e}")
                break

    def stop(self):
        if not self.is_recording:
            return
            
        logger.info("⏹️ Dictation stopped, processing audio...")
        self.is_recording = False
        if self.stream:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
            
        self.state_callback("thinking")
        
        if not self.frames:
            self.state_callback("idle")
            return

        # Save to wav in memory
        wav_io = io.BytesIO()
        with wave.open(wav_io, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(self.pyaudio_instance.get_sample_size(pyaudio.paInt16))
            wf.setframerate(16000)
            wf.writeframes(b''.join(self.frames))
            
        threading.Thread(target=self._process, args=(wav_io.getvalue(),), daemon=True).start()

    def _process(self, wav_data):
        prompt = (
            "You are an ultra-fast speech transcription and translation engine. "
            "Transcribe the audio exactly. If the speech is in Hindi or Hinglish, "
            "translate it directly into fluent, natural English. Fix punctuation and capitalization. "
            "Remove verbal fillers (um, uh, matlab, yaani, like). Output ONLY the final text. "
            "Never include explanations, markdown code blocks, quotes, or conversational commentary."
        )
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[
                        prompt,
                        types.Part.from_bytes(data=wav_data, mime_type='audio/wav')
                    ]
                )
                text = response.text.strip()
                if text:
                    logger.info(f"📝 Dictation result: {text}")
                    self._blast_paste(text)
                    play_earcon("pop")
                else:
                    logger.warning("Dictation returned empty string.")
                self.state_callback("idle")
                return # success
            except Exception as e:
                logger.error(f"Dictation API Error (attempt {attempt+1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(1) # wait before retry
                else:
                    play_earcon("fail")
                    self.state_callback("idle")

    def _blast_paste(self, text):
        old = get_clipboard()
        set_clipboard(text)
        time.sleep(0.05)
        
        active = get_active_window().lower()
        is_terminal = "kitty" in active or "alacritty" in active or "terminal" in active or "wezterm" in active
        
        # Prefer wtype if available
        if shutil.which("wtype"):
            if is_terminal:
                subprocess.run(["wtype", "-M", "ctrl", "-M", "shift", "-P", "v", "-p", "v", "-m", "shift", "-m", "ctrl"])
            else:
                subprocess.run(["wtype", "-M", "ctrl", "-P", "v", "-p", "v", "-m", "ctrl"])
        elif shutil.which("ydotool"):
            if is_terminal:
                subprocess.run(["ydotool", "key", "29:1", "42:1", "47:1", "47:0", "42:0", "29:0"])
            else:
                subprocess.run(["ydotool", "key", "29:1", "47:1", "47:0", "29:0"])
        else:
            logger.error("No injection utility (wtype or ydotool) found!")
                
        # Restore clipboard
        def restore():
            time.sleep(0.3)
            if old:
                set_clipboard(old)
        threading.Thread(target=restore, daemon=True).start()
