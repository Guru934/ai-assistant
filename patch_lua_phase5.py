with open("/home/guru/.config/hypr/hyprland/keybinds.lua", "r") as f:
    content = f.read()

replacement = """-- Cat-Talker Active Window Capture
create_bind("SUPER + SHIFT + A", hl.dsp.exec_cmd("pkill -SIGUSR2 -f cat_talker.main"))

-- Cat-Talker Push-To-Talk Dictation (F8)
create_bind("F8", hl.dsp.exec_cmd("pkill -34 -f cat_talker.main"))
create_bind("F8", hl.dsp.exec_cmd("pkill -35 -f cat_talker.main"), release)
"""

content = content.replace("""-- Cat-Talker Active Window Capture
create_bind("SUPER + SHIFT + A", hl.dsp.exec_cmd("pkill -SIGUSR2 -f cat_talker.main"))""", replacement)

with open("/home/guru/.config/hypr/hyprland/keybinds.lua", "w") as f:
    f.write(content)

print("keybinds.lua updated with F8 dictation shortcut")
