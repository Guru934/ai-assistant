import re

with open("src/cat_talker/agent.py", "r") as f:
    content = f.read()

content = content.replace("read the clipboard, check the active window", "read the clipboard (including currently highlighted text via primary_selection=True), check the active window")

content = content.replace(
    "Never say you cannot see or control the PC. Use your tools immediately to fulfill the request!\"",
    "Never say you cannot see or control the PC. Use your tools immediately to fulfill the request! \"\n            \"If the user asks to format/fix highlighted text, use get_clipboard(primary_selection=True), process it, and use set_clipboard(text) to copy the result.\""
)

with open("src/cat_talker/agent.py", "w") as f:
    f.write(content)

print("Agent instructions patched.")
