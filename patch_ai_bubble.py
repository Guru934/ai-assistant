with open("src/cat_talker/ui_scratchpad.py", "r") as f:
    content = f.read()

old_ai_bubble = """QTextBrowser.ai-bubble {{
    background-color: #1e2030; 
    color: #c0caf5; 
    border: 1px solid rgba(255, 255, 255, 0.06); 
    border-radius: 14px; 
    padding: 12px 16px;
}}"""

new_ai_bubble = """QTextBrowser.ai-bubble {{
    background-color: transparent; 
    color: #c0caf5; 
    border: none;
    padding: 6px 12px;
}}"""
content = content.replace(old_ai_bubble, new_ai_bubble)

# Change layout width to 100% (so remove the right spacer)
old_ai_layout = """        elif self.role == "assistant":
            if self.is_error:
                self.text_display.setProperty("class", "error-bubble")
            else:
                self.text_display.setProperty("class", "ai-bubble")
            layout.addWidget(self.text_display, 85)
            spacer = QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            layout.addSpacerItem(spacer)"""

new_ai_layout = """        elif self.role == "assistant":
            if self.is_error:
                self.text_display.setProperty("class", "error-bubble")
                layout.addWidget(self.text_display, 85)
                spacer = QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                layout.addSpacerItem(spacer)
            else:
                self.text_display.setProperty("class", "ai-bubble")
                layout.addWidget(self.text_display, 100)"""

content = content.replace(old_ai_layout, new_ai_layout)

with open("src/cat_talker/ui_scratchpad.py", "w") as f:
    f.write(content)
print("Updated AI bubble styling")
