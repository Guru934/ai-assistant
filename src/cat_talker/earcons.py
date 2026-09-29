import pyaudio
import math
import struct
import threading

def play_earcon(chime_type):
    def _play():
        p = pyaudio.PyAudio()
        try:
            stream = p.open(format=pyaudio.paInt16,
                            channels=1,
                            rate=16000,
                            output=True)
            frames = []
            rate = 16000
            
            if chime_type == 'blip': # start dictation
                duration = 0.05
                freq = 1200.0
                for i in range(int(rate * duration)):
                    t = float(i) / rate
                    decay = math.exp(-t * 30)
                    val = int(32767 * decay * math.sin(2 * math.pi * freq * t))
                    frames.append(struct.pack('h', val))
            
            elif chime_type == 'pop': # text pastes
                duration = 0.05
                freq = 600.0
                for i in range(int(rate * duration)):
                    t = float(i) / rate
                    decay = math.exp(-t * 60)
                    val = int(32767 * decay * math.sin(2 * math.pi * freq * t))
                    frames.append(struct.pack('h', val))
                    
            elif chime_type == 'fail': # request fails
                duration = 0.1
                freq = 250.0
                for i in range(int(rate * duration)):
                    t = float(i) / rate
                    decay = math.exp(-t * 10)
                    val = int(16000 * decay * math.sin(2 * math.pi * freq * t))
                    frames.append(struct.pack('h', val))
                    
            if frames:
                stream.write(b''.join(frames))
            stream.stop_stream()
            stream.close()
        except Exception as e:
            print(f"Earcon error: {e}")
        finally:
            p.terminate()

    threading.Thread(target=_play, daemon=True).start()
