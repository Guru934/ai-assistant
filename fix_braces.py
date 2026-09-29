import re
with open("src/cat_talker/ui_scratchpad.py", "r") as f:
    orig = f.read()

# I will recreate the QSS string dynamically using string concatenation or a separate multiline string (non-f-string) to avoid these issues.
# Then map format back. Or just replace { and } safely. Let's just find the QSS block and escape correctly.

start_tag = 'QSS_STYLES = f"""'
end_tag = '"""'
start_idx = orig.find(start_tag)
end_idx = orig.find(end_tag, start_idx + len(start_tag))

qss_content = orig[start_idx+len(start_tag):end_idx]

# Let's replace any single { or } with {{ }} EXCEPT {COLORS...}
# First, revert all multiple braces to single braces (to clean up my sed mess)
qss_content = re.sub(r'\{+', '{', qss_content)
qss_content = re.sub(r'\}+', '}', qss_content)

# Now, we carefully double brace everything that is not {COLORS[...]}
parts = re.split(r'(\{COLORS\[.*?\]\})', qss_content)
fixed_parts = []
for p in parts:
    if p.startswith('{COLORS'):
        fixed_parts.append(p)
    else:
        fixed_parts.append(p.replace('{', '{{').replace('}', '}}'))

fixed_qss = "".join(fixed_parts)

new_content = orig[:start_idx+len(start_tag)] + fixed_qss + orig[end_idx:]
with open("src/cat_talker/ui_scratchpad.py", "w") as f:
    f.write(new_content)
