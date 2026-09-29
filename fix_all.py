import re
with open("src/cat_talker/ui_scratchpad.py", "r") as f:
    text = f.read()

# Fix all the extra brackets in styles
# For strings that don't start with f", we can safely shrink all {+ and }+ to { and }
def revert_brackets(match):
    s = match.group(0)
    if s.startswith('f'):
        return s
    
    # Not an f-string, so replace {{ with { and }} with }
    inner = s
    inner = re.sub(r'\{+', '{', inner)
    inner = re.sub(r'\}+', '}', inner)
    return inner

# Match strings (single or double quotes, multiline or single)
# Be careful not to replace python code. Just match .setStyleSheet( ... )
def fix_setstylesheet(match):
    content = match.group(1)
    if 'f"' in content or "f'" in content or 'f"""' in content or "f'''" in content:
        return match.group(0) # skip if there's an f string inside
    # Otherwise collapse brackets
    fixed = re.sub(r'\{+', '{', content)
    fixed = re.sub(r'\}+', '}', fixed)
    return f"setStyleSheet({fixed})"

text = re.sub(r'setStyleSheet\((.*?)\)', fix_setstylesheet, text, flags=re.DOTALL)

with open("src/cat_talker/ui_scratchpad.py", "w") as f:
    f.write(text)
