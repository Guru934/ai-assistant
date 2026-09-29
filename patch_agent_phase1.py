import re

with open("src/cat_talker/agent.py", "r") as f:
    content = f.read()

# Add time import if not present
if "import time" not in content:
    content = content.replace("import asyncio\n", "import asyncio\nimport time\n")

target = """                                                    if isinstance(args, dict):
                                                        result = func(**args)
                                                    else:
                                                        result = func()"""

replacement = """                                                    start_time = time.time()
                                                    if isinstance(args, dict):
                                                        result = func(**args)
                                                    else:
                                                        result = func()
                                                    duration = time.time() - start_time
                                                    
                                                    # Send notification if task took more than 3 seconds
                                                    if duration > 3.0:
                                                        from cat_talker.tools import send_notification
                                                        res_str = str(result)
                                                        if len(res_str) > 100: res_str = res_str[:97] + "..."
                                                        send_notification("Task Completed", f"{function_call.name}: {res_str}")"""

content = content.replace(target, replacement)

with open("src/cat_talker/agent.py", "w") as f:
    f.write(content)

print("Patch applied to agent.py")
