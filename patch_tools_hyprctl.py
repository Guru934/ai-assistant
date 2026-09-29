import re

with open("/home/guru/cat-talker/src/cat_talker/tools.py", "r") as f:
    content = f.read()

old_focus = 'subprocess.run(["hyprctl", "dispatch", "focuswindow", f"address:{addr}"], check=True)'
new_focus = 'subprocess.run(["hyprctl", "eval", f"hl.dispatch(hl.dsp.exec_raw(\'focuswindow address:{addr}\'))"], check=True)'
content = content.replace(old_focus, new_focus)

old_workspace = 'subprocess.run(["hyprctl", "dispatch", "workspace", str(workspace_num)], check=True)'
new_workspace = 'subprocess.run(["hyprctl", "eval", f"hl.dispatch(hl.dsp.exec_raw(\'workspace {workspace_num}\'))"], check=True)'
content = content.replace(old_workspace, new_workspace)

with open("/home/guru/cat-talker/src/cat_talker/tools.py", "w") as f:
    f.write(content)
