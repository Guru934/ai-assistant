import json
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Union
import cv2
import mss
import numpy as np

from cat_talker.logging_config import get_logger

logger = get_logger("cat_talker.vision")


class VisionInterface:
    def __init__(self):
        """Initializes the Vision capture interface with multi-monitor support."""
        self.use_grim = bool(shutil.which("grim"))
        self.monitors: List[Dict[str, Any]] = []
        self.monitor_width = 1920
        self.monitor_height = 1080

        self.refresh_monitors()
        if self.use_grim:
            logger.info("👁️  Wayland detected! Using 'grim' for native multi-monitor screen capture.")
        else:
            logger.info("👁️  X11 detected. Using 'mss' for multi-monitor screen capture.")

    def refresh_monitors(self) -> List[Dict[str, Any]]:
        """Detects and returns all connected monitors with their properties."""
        monitors: List[Dict[str, Any]] = []

        if self.use_grim and shutil.which("hyprctl"):
            try:
                proc = subprocess.run(
                    ["hyprctl", "monitors", "-j"],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    data = json.loads(proc.stdout)
                    for item in data:
                        monitors.append({
                            "id": item.get("id", 0),
                            "name": item.get("name", "Unknown"),
                            "description": item.get("description", ""),
                            "width": int(item.get("width", 1920)),
                            "height": int(item.get("height", 1080)),
                            "x": int(item.get("x", 0)),
                            "y": int(item.get("y", 0)),
                            "scale": float(item.get("scale", 1.0)),
                            "focused": bool(item.get("focused", False)),
                        })
            except Exception as e:
                logger.warning(f"Failed to query hyprctl monitors: {e}")

        # Fallback to mss if hyprctl is not available or returned no monitors
        if not monitors:
            try:
                with mss.mss() as sct:
                    # sct.monitors[0] is the combined virtual screen
                    # sct.monitors[1:] are individual screens
                    for idx, mon in enumerate(sct.monitors[1:] if len(sct.monitors) > 1 else sct.monitors):
                        monitors.append({
                            "id": idx,
                            "name": mon.get("output", f"Screen-{idx}"),
                            "description": "",
                            "width": mon["width"],
                            "height": mon["height"],
                            "x": mon["left"],
                            "y": mon["top"],
                            "scale": 1.0,
                            "focused": idx == 0,
                        })
            except Exception as e:
                logger.warning(f"Failed to query mss monitors: {e}")

        if not monitors:
            monitors.append({
                "id": 0,
                "name": "default",
                "description": "",
                "width": 1920,
                "height": 1080,
                "x": 0,
                "y": 0,
                "scale": 1.0,
                "focused": True,
            })

        self.monitors = monitors

        # Set default dimensions from active or primary monitor
        active = self.get_active_monitor()
        self.monitor_width = active["width"]
        self.monitor_height = active["height"]

        return self.monitors

    def get_monitors(self) -> List[Dict[str, Any]]:
        """Returns the list of detected monitors."""
        if not self.monitors:
            return self.refresh_monitors()
        return self.monitors

    def get_active_monitor(self) -> Dict[str, Any]:
        """Returns the currently focused monitor or the first available monitor."""
        for mon in self.monitors:
            if mon.get("focused"):
                return mon
        return self.monitors[0] if self.monitors else {
            "id": 0,
            "name": "default",
            "width": 1920,
            "height": 1080,
            "focused": True
        }

    def _resolve_target_monitor(self, monitor: Optional[Union[str, int]] = None) -> Optional[Dict[str, Any]]:
        """Resolves monitor target by name, ID, or active status. Returns None for 'all'."""
        if monitor is None or str(monitor).strip().lower() in ("", "focused", "active", "current"):
            # Refresh to pick up latest focused monitor state
            self.refresh_monitors()
            return self.get_active_monitor()

        target_str = str(monitor).strip().lower()
        if target_str in ("all", "full", "desktop", "everything"):
            return None  # None indicates full desktop capture

        # Check by integer id
        try:
            target_id = int(target_str)
            for mon in self.monitors:
                if mon["id"] == target_id:
                    return mon
        except ValueError:
            pass

        # Check by name matching (case-insensitive)
        for mon in self.monitors:
            if mon["name"].lower() == target_str:
                return mon

        # Partial match on name
        for mon in self.monitors:
            if target_str in mon["name"].lower():
                return mon

        # Fallback to active monitor
        return self.get_active_monitor()


    def get_active_window_region(self) -> Optional[tuple[int, int, int, int]]:
        if not self.use_grim or not shutil.which("hyprctl"):
            return None
        try:
            proc = subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True, timeout=2)
            if proc.returncode == 0 and proc.stdout.strip():
                import json
                data = json.loads(proc.stdout)
                at = data.get("at", [0, 0])
                size = data.get("size", [0, 0])
                if size[0] > 0 and size[1] > 0:
                    return (int(at[0]), int(at[1]), int(size[0]), int(size[1]))
        except Exception:
            pass
        return None

    def capture_frame(self, monitor: Optional[Union[str, int]] = None, region: Optional[tuple[int, int, int, int]] = None) -> Optional[bytes]:
        """Captures a single frame for a specific monitor or entire desktop and returns JPEG bytes.

        Args:
            monitor: Monitor name ('eDP-1', 'HDMI-A-1'), monitor ID (0, 1), 'focused'/'active',
                     or 'all' for full virtual desktop.
        """
        target_mon = self._resolve_target_monitor(monitor)

        if not self.use_grim:
            # X11 via MSS
            try:
                with mss.mss() as sct:
                    if target_mon is None:
                        # Full desktop
                        mon_rect = sct.monitors[0]
                    else:
                        mon_idx = target_mon["id"] + 1
                        mon_rect = sct.monitors[mon_idx] if mon_idx < len(sct.monitors) else sct.monitors[0]

                    img_bgra = np.array(sct.grab(mon_rect))
                    orig_h, orig_w = img_bgra.shape[:2]
                    target_width = 1024
                    target_height = max(1, int(orig_h * (1024.0 / orig_w)))

                    resized = cv2.resize(img_bgra, (target_width, target_height))
                    bgr = cv2.cvtColor(resized, cv2.COLOR_BGRA2BGR)
                    _, encoded = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 60])
                    return encoded.tobytes()
            except Exception as e:
                logger.error(f"X11 Vision capture error: {e}", exc_info=True)
                return None
        else:
            # Wayland via Grim
            try:
                cmd = ["grim", "-t", "jpeg", "-q", "60"]
                if region is not None:
                    # Format for grim -g is "x,y wxh"
                    cmd.extend(["-g", f"{region[0]},{region[1]} {region[2]}x{region[3]}"])
                elif target_mon is not None and "name" in target_mon and target_mon["name"] != "default":
                    cmd.extend(["-o", target_mon["name"]])
                cmd.append("-")

                proc = subprocess.run(cmd, capture_output=True, timeout=5)
                if proc.returncode != 0 or not proc.stdout:
                    # Fallback to general capture without output specification
                    proc = subprocess.run(["grim", "-t", "jpeg", "-q", "60", "-"], capture_output=True, timeout=5)
                    if proc.returncode != 0 or not proc.stdout:
                        return None

                img_array = np.frombuffer(proc.stdout, np.uint8)
                img_bgr = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                if img_bgr is None:
                    return None

                orig_h, orig_w = img_bgr.shape[:2]
                target_width = 1024
                target_height = max(1, int(orig_h * (1024.0 / orig_w)))

                resized = cv2.resize(img_bgr, (target_width, target_height))
                _, encoded = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, 60])
                return encoded.tobytes()
            except Exception as e:
                logger.error(f"Wayland Vision capture error: {e}", exc_info=True)
                return None
