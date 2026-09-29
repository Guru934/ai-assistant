import re

with open("/home/guru/cat-talker/src/cat_talker/agent.py", "r") as f:
    content = f.read()

# Add import
if "cat_talker.earcons" not in content:
    content = content.replace("from cat_talker.logging_config import get_logger", "from cat_talker.logging_config import get_logger\nfrom cat_talker.earcons import play_earcon")

# Play 'fail' earcon when connection drops / reconnects
old_except = """                except Exception as e:
                    logger.error(f"Agent connection dropped (auto-reconnecting): {e}", exc_info=True)
                    if text_callback:
                        text_callback("system", f"Reconnecting... ({e})")"""
                        
new_except = """                except Exception as e:
                    logger.error(f"Agent connection dropped (auto-reconnecting): {e}", exc_info=True)
                    play_earcon("fail")
                    if text_callback:
                        text_callback("system", f"Reconnecting... ({e})")"""
                        
content = content.replace(old_except, new_except)

with open("/home/guru/cat-talker/src/cat_talker/agent.py", "w") as f:
    f.write(content)
