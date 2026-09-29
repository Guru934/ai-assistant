import re

with open("src/cat_talker/vision.py", "r") as f:
    content = f.read()

# Add get_active_window_region
new_funcs = """
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

    def capture_frame"""

content = content.replace("    def capture_frame", new_funcs)

# Modify capture_frame signature
content = content.replace(
    "def capture_frame(self, monitor: Optional[Union[str, int]] = None) -> Optional[bytes]:",
    "def capture_frame(self, monitor: Optional[Union[str, int]] = None, region: Optional[tuple[int, int, int, int]] = None) -> Optional[bytes]:"
)

# Modify grim command to use region
grim_logic_target = """                cmd = ["grim", "-t", "jpeg", "-q", "60"]
                if target_mon is not None and "name" in target_mon and target_mon["name"] != "default":
                    cmd.extend(["-o", target_mon["name"]])
                cmd.append("-")"""

grim_logic_replacement = """                cmd = ["grim", "-t", "jpeg", "-q", "60"]
                if region is not None:
                    # Format for grim -g is "x,y wxh"
                    cmd.extend(["-g", f"{region[0]},{region[1]} {region[2]}x{region[3]}"])
                elif target_mon is not None and "name" in target_mon and target_mon["name"] != "default":
                    cmd.extend(["-o", target_mon["name"]])
                cmd.append("-")"""

content = content.replace(grim_logic_target, grim_logic_replacement)

with open("src/cat_talker/vision.py", "w") as f:
    f.write(content)

print("Vision patched")
