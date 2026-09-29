import subprocess
import shutil
import os
import time
import urllib.request
import urllib.parse
import re
import json
import cv2
import mss
import numpy as np

from cat_talker.logging_config import get_logger

logger = get_logger("cat_talker.tools")

# --- STATE FOR VOICE CONFIRMATION FLOW ---
PENDING_RISKY_ACTION = None
PENDING_TIME = 0

def confirm_action() -> str:
    """Call this tool whenever the user says 'yes' or confirms a paused risky action."""
    global PENDING_RISKY_ACTION
    if not PENDING_RISKY_ACTION:
        return "Error: No pending action to confirm."
    
    action = PENDING_RISKY_ACTION
    PENDING_RISKY_ACTION = None
    
    try:
        return action['func'](**action['args'])
    except Exception as e:
        return f"Execution failed: {str(e)}"

def cancel_action() -> str:
    """Call this tool if the user says 'no', 'stop', or 'cancel' to a paused action."""
    global PENDING_RISKY_ACTION
    if PENDING_RISKY_ACTION:
        PENDING_RISKY_ACTION = None
        return "Action safely cancelled."
    return "No pending action to cancel."

def _handle_risky(func_name, func_ref, args, desc):
    global PENDING_RISKY_ACTION, PENDING_TIME
    now = time.time()
    
    if PENDING_RISKY_ACTION and PENDING_RISKY_ACTION['name'] == func_name and PENDING_RISKY_ACTION['args'] == args:
        if now - PENDING_TIME < 120:
            PENDING_RISKY_ACTION = None
            return func_ref(**args)
            
    PENDING_RISKY_ACTION = {"name": func_name, "func": func_ref, "args": args}
    PENDING_TIME = now
    return f"PAUSED FOR SAFETY. You MUST ask the user out loud: 'Do you confirm I should {desc}?' Wait for them to say yes. If they say yes, call the 'confirm_action' tool."

# ----------------------------------------------------

def open_application(app_name: str) -> str:
    if not app_name: return "Error: No application name provided."
    app_name = app_name.lower().strip()
    mapping = {'brave': 'brave-browser', 'chrome': 'google-chrome', 'terminal': 'gnome-terminal' if shutil.which('gnome-terminal') else 'kitty', 'calculator': 'gnome-calculator' if shutil.which('gnome-calculator') else 'kcalc', 'notepad': 'gedit' if shutil.which('gedit') else 'mousepad'}
    executable = mapping.get(app_name, app_name)
    if not shutil.which(executable): return f"Error: '{executable}' application could not be found."
    try:
        subprocess.Popen([executable], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Successfully opened {app_name}."
    except Exception as e:
        return f"Failed to open {app_name}. Exception: {str(e)}"

def open_website(url: str) -> str:
    if not url.startswith('http'): url = 'https://' + url
    try:
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Successfully opened website: {url}"
        elif shutil.which("brave-browser"):
            subprocess.Popen(["brave-browser", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Opened website in brave: {url}"
        return "Error: xdg-open not found to launch URL."
    except Exception as e:
        return f"Failed to open website. Exception: {str(e)}"

def get_clipboard() -> str:
    try:
        if shutil.which("wl-paste"):
            res = subprocess.run(["wl-paste"], capture_output=True, text=True, timeout=1)
            return res.stdout.strip()
        elif shutil.which("xclip"):
            res = subprocess.run(["xclip", "-o", "-selection", "clipboard"], capture_output=True, text=True, timeout=1)
            return res.stdout.strip()
        return "Error: No clipboard utility found."
    except Exception as e:
        return f"Error accessing clipboard: {str(e)}"

def get_active_window() -> str:
    try:
        if shutil.which("hyprctl"):
            res = subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True, timeout=1)
            if res.stdout.strip():
                import json
                data = json.loads(res.stdout)
                app = data.get("class", "Unknown App")
                title = data.get("title", "Unknown Title")
                return f"Active window: {app} - {title}"
        return "Active window information not available."
    except Exception as e:
        return f"Error reading window info: {str(e)}"

def list_directory(path: str) -> str:
    target_path = os.path.expanduser(path)
    if not os.path.exists(target_path): return f"Error: Path {target_path} does not exist"
    if not os.path.isdir(target_path): return f"Error: Path {target_path} is not a directory"
    try:
        return f"Contents of {target_path}: {', '.join(os.listdir(target_path))}"
    except Exception as e:
        return f"Error listing directory: {e}"

def open_file(path: str) -> str:
    target_path = os.path.expanduser(path)
    if not os.path.exists(target_path): return f"Error: File {target_path} does not exist"
    try:
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", target_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Successfully opened file {target_path}"
        return "Error: xdg-open not found."
    except Exception as e:
        return f"Failed to open file: {e}"

def set_volume(level_percent: int) -> str:
    try:
        level_percent = max(0, min(100, level_percent))
        if shutil.which("wpctl"):
            subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level_percent}%"], check=True)
            return f"System volume set to {level_percent}%"
        return "Error: wpctl tool not found."
    except Exception as e:
        return f"Error setting volume: {e}"

def set_brightness(level_percent: int) -> str:
    try:
        level_percent = max(0, min(100, level_percent))
        if shutil.which("brightnessctl"):
            subprocess.run(["brightnessctl", "set", f"{level_percent}%"], check=True)
            return f"System brightness set to {level_percent}%"
        return "Error: brightnessctl tool not found."
    except Exception as e:
        return f"Error setting brightness: {e}"

def take_screenshot(filename: str = "screenshot.jpg", monitor: str = "") -> str:
    """Takes a screenshot of the full desktop or a specific monitor.

    Args:
        filename: The file path to save the screenshot to.
        monitor: Monitor name (e.g. 'eDP-1'), ID (e.g. '0'), 'focused', or 'all' for full desktop.
                 If empty, captures the focused monitor.
    """
    try:
        pictures_dir = os.path.expanduser("~/Pictures/Screenshots")
        os.makedirs(pictures_dir, exist_ok=True)
        if not filename.endswith(".jpg") and not filename.endswith(".png"): filename += ".jpg"
        filepath = os.path.join(pictures_dir, filename)

        # Resolve which monitor to capture
        from cat_talker.vision import VisionInterface
        vision = VisionInterface()
        target_mon = vision._resolve_target_monitor(monitor) if monitor else vision.get_active_monitor()

        # Determine the capture area
        use_grim = bool(shutil.which("grim"))

        if use_grim and target_mon is not None and "name" in target_mon and target_mon["name"] != "default":
            # Capture specific monitor using grim -o option
            subprocess.run(["grim", "-o", target_mon["name"], filepath], check=True)
        else:
            # Fallback: capture full desktop or use mss
            # If target_mon is None or grim not available, use default grim capture
            if not use_grim:
                # X11 via mss - capture focused monitor
                with mss.mss() as sct:
                    mon_idx = 1  # focused/monitor 1
                    if target_mon and target_mon.get("id") is not None:
                        mon_idx = target_mon["id"] + 1
                    mon = sct.monitors[mon_idx] if mon_idx < len(sct.monitors) else sct.monitors[0]
                    try:
                        img_bgra = np.array(sct.grab(mon))
                        resized = cv2.resize(img_bgra, (int(mon["width"] * 0.5), int(mon["height"] * 0.5)))
                        bgr = cv2.cvtColor(resized, cv2.COLOR_BGRA2BGR)
                        _, encoded = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 60])
                        # Save the encoded bytes
                        with open(filepath, "wb") as f:
                            f.write(encoded.tobytes())
                        return f"Saved screenshot to {filepath} (X11 monitor {target_mon['name'] if target_mon else 'focused'})"
                    except Exception as e2:
                        return f"Error X11 screenshot: {e2}"
            subprocess.run(["grim", filepath], check=True)

        return f"Saved screenshot to {filepath}"
    except Exception as e:
        return f"Error saving screenshot: {e}"

def search_and_play_youtube(query: str) -> str:
    try:
        encoded_query = urllib.parse.quote(query)
        url = f"https://www.youtube.com/results?search_query={encoded_query}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            html = response.read().decode('utf-8')
        video_ids = re.findall(r"watch\?v=([a-zA-Z0-9_-]{11})", html)
        if not video_ids: return open_website(url)
        return open_website(f"https://www.youtube.com/watch?v={video_ids[0]}")
    except Exception as e:
        return f"Failed to search and play YouTube: {e}"

def click_screen(x: int, y: int) -> str:
    def _execute(x, y):
        try:
            if shutil.which("ydotool"):
                subprocess.run(["ydotool", "mousemove", "--absolute", str(x), str(y)], check=True)
                subprocess.run(["ydotool", "click", "0xC0"], check=True)
                return f"Clicked at coordinate '{x}, {y}'"
            return "Error: ydotool not found."
        except Exception as e:
            return f"Failed to click: {e}"
    return _handle_risky("click_screen", _execute, {"x": x, "y": y}, f"click at {x}, {y}")

def type_text(text: str) -> str:
    def _execute(text):
        try:
            if shutil.which("ydotool"):
                p = subprocess.Popen(["ydotool", "type", text])
                p.wait()
                return f"Successfully typed: {text}"
            return "Error: ydotool not found."
        except Exception as e:
            return f"Failed to type: {e}"
    return _handle_risky("type_text", _execute, {"text": text}, f"type \"{text}\"")

def press_key(key: str) -> str:
    def _execute(key):
        try:
            if shutil.which("ydotool"):
                key_map = {"enter": "28", "esc": "1", "escape": "1", "space": "57", "tab": "15", "backspace": "14", "up": "103", "left": "105", "right": "106", "down": "108", "super": "125", "win": "125", "ctrl": "29", "alt": "56", "shift": "42"}
                key_code = key_map.get(key.lower(), key.lower())
                subprocess.run(["ydotool", "key", f"{key_code}:1", f"{key_code}:0"], check=True)
                return f"Pressed key {key}"
            return "Error: ydotool not found."
        except Exception as e:
            return f"Failed to press key: {e}"
    return _handle_risky("press_key", _execute, {"key": key}, f"press the {key} key")

# ─── NEW: Native Wayland Desktop Integrations ───────────────────

def focus_or_launch(app_name: str) -> str:
    """Focuses an already running application window, or launches it if not running.
    
    Args:
        app_name: Name of the application (e.g., 'brave', 'code', 'terminal').
    """
    if not app_name:
        return "Error: No application name provided."
    
    app_name = app_name.lower().strip()
    
    # Mapping of friendly names to window class strings (as seen by hyprctl)
    class_map = {
        'brave': 'brave-browser',
        'chrome': 'google-chrome',
        'code': 'code',
        'vscode': 'code',
        'terminal': 'kitty',
        'kitty': 'kitty',
        'alacritty': 'alacritty',
        'discord': 'discord',
        'spotify': 'spotify',
        'obsidian': 'obsidian',
    }
    
    target_class = class_map.get(app_name, app_name)
    
    try:
        if shutil.which("hyprctl"):
            # Check for existing window
            res = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=2)
            if res.stdout.strip():
                import json
                clients = json.loads(res.stdout)
                for client in clients:
                    if client.get("class", "").lower() == target_class.lower():
                        addr = client.get("address")
                        if addr:
                            subprocess.run(["hyprctl", "eval", f"hl.dispatch(hl.dsp.exec_raw('focuswindow address:{addr}'))"], check=True)
                            return f"Focused existing {app_name} window."
        
        # Not found - launch it
        return open_application(app_name)
    except Exception as e:
        return f"Error focusing/launching {app_name}: {e}"

def switch_workspace(workspace_num: int) -> str:
    """Switches to a specific Hyprland workspace.
    
    Args:
        workspace_num: Workspace number (1-10 typically).
    """
    try:
        if shutil.which("hyprctl"):
            workspace_num = max(1, min(10, workspace_num))  # Clamp to reasonable range
            subprocess.run(["hyprctl", "eval", f"hl.dispatch(hl.dsp.exec_raw('workspace {workspace_num}'))"], check=True)
            return f"Switched to workspace {workspace_num}."
        return "Error: hyprctl not found."
    except Exception as e:
        return f"Error switching workspace: {e}"

def media_action(command: str) -> str:
    """Controls media playback via playerctl.
    
    Args:
        command: One of 'play', 'pause', 'play-pause', 'next', 'previous', 'status', 'metadata'.
    """
    valid_commands = {'play', 'pause', 'play-pause', 'next', 'previous', 'status', 'metadata'}
    cmd = command.lower().strip()
    
    if cmd not in valid_commands:
        return f"Error: Invalid command. Valid: {', '.join(valid_commands)}"
    
    try:
        if shutil.which("playerctl"):
            if cmd == 'metadata':
                res = subprocess.run(["playerctl", "metadata", "--format", "{{title}} - {{artist}}"], capture_output=True, text=True, timeout=2)
                return f"Now playing: {res.stdout.strip()}" if res.stdout.strip() else "No media playing."
            elif cmd == 'status':
                res = subprocess.run(["playerctl", "status"], capture_output=True, text=True, timeout=2)
                return f"Playback status: {res.stdout.strip()}"
            else:
                subprocess.run(["playerctl", cmd], check=True)
                return f"Media command '{cmd}' executed."
        return "Error: playerctl not found."
    except Exception as e:
        return f"Error controlling media: {e}"

def set_clipboard(text: str) -> str:
    """Sets the system clipboard text.
    
    Args:
        text: The text to copy to clipboard.
    """
    try:
        if shutil.which("wl-copy"):
            proc = subprocess.run(["wl-copy"], input=text, text=True, capture_output=True, timeout=1)
            return "Text copied to clipboard."
        return "Error: wl-copy not found."
    except Exception as e:
        return f"Error setting clipboard: {e}"

def send_notification(title: str, body: str) -> str:
    """Sends a desktop notification via notify-send.
    
    Args:
        title: Notification title.
        body: Notification body text.
    """
    try:
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", title, body], check=True)
            return "Notification sent."
        return "Error: notify-send not found."
    except Exception as e:
        return f"Error sending notification: {e}"


def inspect_screen(query: str = "", monitor: str = "") -> str:
    """Takes a crisp desktop screenshot to analyze errors, documents, or websites.
    
    This magic string tells the agent loop to fetch a frame and send it to Gemini.
    
    Args:
        query: What you are looking for on the screen (used for your internal context).
        monitor: Monitor name (e.g. 'eDP-1'), ID (e.g. '0'), 'focused', or 'all' for full desktop.
                 If empty, captures the focused monitor.
    """
    # This magic string tells the agent loop to fetch a frame and send it.
    return "SCREEN_INSPECT_REQUESTED"

def save_user_preference(key: str, value: str) -> str:
    """Saves a user preference or fact to the local memory file.
    
    Args:
        key: The category or preference name (e.g., 'preferred_name', 'music_app')
        value: The value to save.
    """
    mem_path = os.path.expanduser("~/.config/cat-talker/memory.json")
    os.makedirs(os.path.dirname(mem_path), exist_ok=True)
    try:
        data = {}
        if os.path.exists(mem_path):
            with open(mem_path, 'r') as f:
                data = json.load(f)
        data[key] = value
        with open(mem_path, 'w') as f:
            json.dump(data, f, indent=4)
        return f"Successfully saved {key}={value}"
    except Exception as e:
        return f"Failed to save preference: {e}"

# ─── COMPLETE TOOL REGISTRY ─────────────────────────────────────

ALL_TOOLS = [
    open_application, open_website, get_clipboard, get_active_window, list_directory,
    open_file, set_volume, set_brightness, take_screenshot, search_and_play_youtube,
    focus_or_launch, switch_workspace, media_action, set_clipboard, send_notification,
    confirm_action, cancel_action, click_screen, type_text, press_key,
    inspect_screen, save_user_preference
]


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
