with open("/home/guru/.config/hypr/hyprland/keybinds.lua", "r") as f:
    content = f.read()

target = '-- Override F12 to gracefully hide/show cat_talker cleanly\ncreate_bind("F12", hl.dsp.exec_cmd("pkill -SIGUSR1 -f cat_talker.main"))'
replacement = '-- Override F12 to gracefully hide/show cat_talker cleanly\ncreate_bind("F12", hl.dsp.exec_cmd("pkill -SIGUSR1 -f cat_talker.main"))\n\n-- Cat-Talker Active Window Capture\ncreate_bind("SUPER + SHIFT + A", hl.dsp.exec_cmd("pkill -SIGUSR2 -f cat_talker.main"))'

content = content.replace(target, replacement)

with open("/home/guru/.config/hypr/hyprland/keybinds.lua", "w") as f:
    f.write(content)

print("keybinds.lua patched")
