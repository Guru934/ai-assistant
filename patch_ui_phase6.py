import re

with open("src/cat_talker/main.py", "r") as f:
    content = f.read()

# Modify handle_sigusr1 area to also include handle_sigusr2
sigusr1_target = """    def handle_sigusr1(signum, frame):
        if window.isHidden():
            window.show()
        else:
            window.hide()
            
    signal.signal(signal.SIGUSR1, handle_sigusr1)"""

sigusr_replacement = """    def handle_sigusr1(signum, frame):
        if window.isHidden():
            window.show()
        else:
            window.hide()
            
    signal.signal(signal.SIGUSR1, handle_sigusr1)

    global_agent = []
    
    def handle_sigusr2(signum, frame):
        if global_agent:
            agent = global_agent[0]
            if agent.loop:
                agent.loop.call_soon_threadsafe(agent.synthetic_input_queue.put_nowait, "ACTIVE_WINDOW")
                
    signal.signal(signal.SIGUSR2, handle_sigusr2)"""

content = content.replace(sigusr1_target, sigusr_replacement)

# Modify agent_thread.start
thread_target = """    agent_thread = threading.Thread(
        target=start_agent_in_thread,
        args=(on_volume, on_quit, on_text, on_state, on_bubble, on_glow),
        daemon=True
    )"""

thread_replacement = """    agent_thread = threading.Thread(
        target=start_agent_in_thread,
        args=(on_volume, on_quit, on_text, on_state, on_bubble, on_glow, global_agent),
        daemon=True
    )"""

content = content.replace(thread_target, thread_replacement)

# Update context menu to mention the shortcut
menu_target = 'act_hide = menu.addAction("👁️ Hide (Run `pkill -SIGUSR1 -f cat_talker.main` to unhide)")'
menu_replacement = 'act_hide = menu.addAction("👁️ Toggle Vis (pkill -SIGUSR1)")\n        act_capture = menu.addAction("📸 Capture Active Window (pkill -SIGUSR2)")'

content = content.replace(menu_target, menu_replacement)

menu_action_target = """        elif action == act_hide:
            self.hide()"""
menu_action_replacement = """        elif action == act_hide:
            self.hide()
        elif action == act_capture:
            # Re-use the existing logic by sending SIGUSR2 to ourselves
            os.kill(os.getpid(), signal.SIGUSR2)"""

content = content.replace(menu_action_target, menu_action_replacement)

with open("src/cat_talker/main.py", "w") as f:
    f.write(content)

print("Main UI patched for Phase 6")
