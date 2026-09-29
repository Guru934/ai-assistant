import re

with open("src/cat_talker/tools.py", "r") as f:
    content = f.read()

target = """def get_clipboard() -> str:
    try:
        if shutil.which("wl-paste"):
            res = subprocess.run(["wl-paste"], capture_output=True, text=True, timeout=1)
            return res.stdout.strip()
        elif shutil.which("xclip"):
            res = subprocess.run(["xclip", "-o", "-selection", "clipboard"], capture_output=True, text=True, timeout=1)
            return res.stdout.strip()
        return "Error: No clipboard utility found."
    except Exception as e:
        return f"Error accessing clipboard: {str(e)}" """

replacement = """def get_clipboard(primary_selection: bool = False) -> str:
    \"\"\"Reads the clipboard.
    
    Args:
        primary_selection: If True, reads the currently highlighted/selected text (X11/Wayland primary selection) instead of the regular copied clipboard.
    \"\"\"
    try:
        if shutil.which("wl-paste"):
            args = ["wl-paste", "-p"] if primary_selection else ["wl-paste"]
            res = subprocess.run(args, capture_output=True, text=True, timeout=1)
            return res.stdout.strip()
        elif shutil.which("xclip"):
            sel = "primary" if primary_selection else "clipboard"
            res = subprocess.run(["xclip", "-o", "-selection", sel], capture_output=True, text=True, timeout=1)
            return res.stdout.strip()
        return "Error: No clipboard utility found."
    except Exception as e:
        return f"Error accessing clipboard: {str(e)}" """

content = content.replace(target, replacement)

with open("src/cat_talker/tools.py", "w") as f:
    f.write(content)

print("get_clipboard updated.")
