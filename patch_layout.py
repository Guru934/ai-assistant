with open("src/cat_talker/ui_scratchpad.py", "r") as f:
    content = f.read()

content = content.replace("chat_layout.addWidget(self.chat_feed_scroll_area)", "chat_layout.addWidget(self.chat_feed_scroll_area, stretch=1)")
content = content.replace("chat_layout.addWidget(input_bar_widget)", "chat_layout.addWidget(input_bar_widget, stretch=0)")
content = content.replace("input_bar_layout.setContentsMargins(10, 5, 10, 5)", "input_bar_layout.setContentsMargins(20, 8, 20, 16)")

with open("src/cat_talker/ui_scratchpad.py", "w") as f:
    f.write(content)
print("Updated stretch and padding")
