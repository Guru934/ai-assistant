with open("src/cat_talker/agent.py", "r") as f:
    content = f.read()

target = """                                        if msg.server_content.model_turn:"""
replacement = """                                        if hasattr(msg.server_content, 'turn_complete') and msg.server_content.turn_complete:
                                            self._is_speaking = False
                                            self._set_state(state_callback, "listening")
                                            self._set_glow(glow_callback, "connected")
                                            import cat_talker.tools
                                            cat_talker.tools.stop_media_ducking()

                                        if msg.server_content.model_turn:"""

content = content.replace(target, replacement)

# We also need to start ducking when thinking/talking
target2 = """                                            # Model started responding - thinking phase
                                            if not self._is_speaking:
                                                self._is_speaking = True
                                                self._set_state(state_callback, "thinking")
                                                self._set_glow(glow_callback, "thinking")"""

replacement2 = """                                            # Model started responding - thinking phase
                                            if not self._is_speaking:
                                                self._is_speaking = True
                                                self._set_state(state_callback, "thinking")
                                                self._set_glow(glow_callback, "thinking")
                                                import cat_talker.tools
                                                cat_talker.tools.start_media_ducking()"""

content = content.replace(target2, replacement2)

with open("src/cat_talker/agent.py", "w") as f:
    f.write(content)

print("Agent patched for turn_complete and ducking.")
