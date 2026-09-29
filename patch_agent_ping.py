import re

with open("/home/guru/cat-talker/src/cat_talker/agent.py", "r") as f:
    content = f.read()

# Instead of None for ping, set it to 30.0 for interval and 10.0 for timeout
content = content.replace("kwargs['ping_interval'] = None", "kwargs['ping_interval'] = 20.0")
content = content.replace("kwargs['ping_timeout'] = None", "kwargs['ping_timeout'] = 20.0")

# If it is already patched, it might have been replaced. Check it:
with open("/home/guru/cat-talker/src/cat_talker/agent.py", "w") as f:
    f.write(content)

