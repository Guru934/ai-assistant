import re

with open("src/cat_talker/main.py", "r") as f:
    content = f.read()

# Remove UI Scratchpad import
content = re.sub(r'from cat_talker\.ui_scratchpad import ScratchpadWindow\n?', '', content)

# Remove DB initialization
content = re.sub(r'    # Initialize database\n    from cat_talker\.db import init_db, purge_empty_sessions\n    init_db\(\)\n    purge_empty_sessions\(\)\n', '', content)

# Remove scratchpad instantiation and visibility logic
scratchpad_spawn_pattern = r'    # Launch Scratchpad.*?scratchpad\.show\(\)\n'
content = re.sub(scratchpad_spawn_pattern, '', content, flags=re.DOTALL)

# Re-write the handle_sigusr1 to solely toggle window
old_sigusr1 = """    def handle_sigusr1(signum, frame):
        if window.isHidden():
            window.show()
        else:
            window.hide()
            
        if scratchpad.isHidden():
            scratchpad.show()
            scratchpad.activateWindow()
        else:
            scratchpad.show()"""

new_sigusr1 = """    def handle_sigusr1(signum, frame):
        if window.isHidden():
            window.show()
        else:
            window.hide()"""

content = content.replace(old_sigusr1, new_sigusr1)

with open("src/cat_talker/main.py", "w") as f:
    f.write(content)
print("Stripped chatbox logic from main!")
