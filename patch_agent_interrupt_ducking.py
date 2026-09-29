with open("src/cat_talker/agent.py", "r") as f:
    content = f.read()

target = """                                        if hasattr(msg.server_content, "interrupted") and msg.server_content.interrupted:
                                            self.audio.clear_output_queue()
                                            self._is_speaking = False
                                            self._set_state(state_callback, "listening")"""

replacement = """                                        if hasattr(msg.server_content, "interrupted") and msg.server_content.interrupted:
                                            self.audio.clear_output_queue()
                                            self._is_speaking = False
                                            self._set_state(state_callback, "listening")
                                            import cat_talker.tools
                                            cat_talker.tools.stop_media_ducking()"""

content = content.replace(target, replacement)

with open("src/cat_talker/agent.py", "w") as f:
    f.write(content)

print("Agent patched for interrupted ducking.")
