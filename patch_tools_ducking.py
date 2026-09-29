with open("src/cat_talker/tools.py", "a") as f:
    f.write('''

# --- MPRIS MEDIA DUCKING ---
class MediaDucker:
    def __init__(self):
        self.is_ducked = False
        self.original_volumes = {}

    def duck(self):
        if self.is_ducked:
            return
        if not shutil.which("playerctl"):
            return
            
        try:
            res = subprocess.run(["playerctl", "-l"], capture_output=True, text=True, timeout=1)
            players = [p.strip() for p in res.stdout.splitlines() if p.strip()]
            
            self.original_volumes = {}
            for player in players:
                try:
                    vol_res = subprocess.run(["playerctl", "-p", player, "volume"], capture_output=True, text=True, timeout=1)
                    if vol_res.stdout.strip():
                        current_vol = float(vol_res.stdout.strip())
                        # Only duck if volume is currently > 0.15
                        if current_vol > 0.15:
                            self.original_volumes[player] = current_vol
                            subprocess.run(["playerctl", "-p", player, "volume", "0.15"], timeout=1)
                except Exception:
                    pass
            if self.original_volumes:
                self.is_ducked = True
        except Exception:
            pass

    def unduck(self):
        if not self.is_ducked:
            return
        if not shutil.which("playerctl"):
            return
            
        for player, vol in self.original_volumes.items():
            try:
                subprocess.run(["playerctl", "-p", player, "volume", str(vol)], timeout=1)
            except Exception:
                pass
        self.original_volumes = {}
        self.is_ducked = False

global_ducker = MediaDucker()

def start_media_ducking():
    import threading
    threading.Thread(target=global_ducker.duck, daemon=True).start()

def stop_media_ducking():
    import threading
    threading.Thread(target=global_ducker.unduck, daemon=True).start()
''')

print("Tools patched.")
