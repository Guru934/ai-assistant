import re

with open("src/cat_talker/main.py", "r") as f:
    content = f.read()

# Make sure DictationManager is imported at top
content = content.replace("from cat_talker.agent import start_agent_in_thread", "from cat_talker.agent import start_agent_in_thread\nfrom cat_talker.dictation import DictationManager")

# Add the UI state definition for dictating
old_paint_idle = """        else: # idle
            # Breathing glow"""
new_paint_dictating = """        elif self.current_state == 'dictating':
            # Solid amber ring indicating recording
            painter.setPen(QPen(QColor(255, 191, 0, 255), 6))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(0, 0), base_radius + 10, base_radius + 10)

        else: # idle
            # Breathing glow"""
content = content.replace(old_paint_idle, new_paint_dictating)

# Update state list validator
content = content.replace('["idle", "listening", "thinking", "talking"]', '["idle", "listening", "thinking", "talking", "dictating"]')

# Hook in signals right after SIGUSR2
old_sigusr2 = """                agent.loop.call_soon_threadsafe(agent.synthetic_input_queue.put_nowait, "ACTIVE_WINDOW")
                
    signal.signal(signal.SIGUSR2, handle_sigusr2)"""
    
new_sigusr2 = """                agent.loop.call_soon_threadsafe(agent.synthetic_input_queue.put_nowait, "ACTIVE_WINDOW")
                
    signal.signal(signal.SIGUSR2, handle_sigusr2)

    # Dictionary Manager setup
    # Because we're in the main thread during initialization, we can create it
    # But it must call UI functions thread-safely
    def on_dictation_state(state):
        QTimer.singleShot(0, lambda: window.state_signal.emit(state))
        
    dictation_manager = DictationManager(on_dictation_state)

    def handle_sigrtmin(signum, frame):
        dictation_manager.start()
        
    def handle_sigrtmin1(signum, frame):
        dictation_manager.stop()
        
    signal.signal(signal.SIGRTMIN, handle_sigrtmin)
    signal.signal(signal.SIGRTMIN + 1, handle_sigrtmin1)"""

content = content.replace(old_sigusr2, new_sigusr2)

with open("src/cat_talker/main.py", "w") as f:
    f.write(content)

print("UI patched for phase 5")
