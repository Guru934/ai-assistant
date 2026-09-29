with open("src/cat_talker/main.py", "r") as f:
    content = f.read()

# Remove the inline import math
content = content.replace("            import math\n", "")

with open("src/cat_talker/main.py", "w") as f:
    f.write(content)

print("Fix applied")
