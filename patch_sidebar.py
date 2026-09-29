import re
import sys

with open("src/cat_talker/ui_scratchpad.py", "r") as f:
    content = f.read()

# 1. Add SessionRowWidget
session_row_code = """
class SessionRowWidget(QWidget):
    delete_clicked = pyqtSignal(str)

    def __init__(self, session_id: str, title: str, is_active: bool, parent=None):
        super().__init__(parent)
        self.session_id = session_id
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        
        self.title_label = QLabel(title)
        if is_active:
            self.title_label.setStyleSheet(f"color: {COLORS['purple']}; font-weight: bold;")
        else:
            self.title_label.setStyleSheet(f"color: {COLORS['foreground']};")
            
        self.delete_btn = QPushButton("✕")
        self.delete_btn.setObjectName("delete_btn")
        self.delete_btn.setFixedSize(24, 24)
        self.delete_btn.setStyleSheet(f\"\"\"
            QPushButton#delete_btn {{
                background: transparent;
                border: none;
                color: {COLORS['comment']};
                border-radius: 4px;
            }}
            QPushButton#delete_btn:hover {{
                color: {COLORS['red']};
                background: rgba(255, 85, 85, 0.1);
            }}
        \"\"\")
        self.delete_btn.clicked.connect(lambda: self.delete_clicked.emit(self.session_id))
        
        layout.addWidget(self.title_label)
        layout.addStretch()
        layout.addWidget(self.delete_btn)

"""

if "class SessionRowWidget" not in content:
    content = content.replace("class MarkdownTextBrowser", session_row_code + "class MarkdownTextBrowser")

# 2. Add toggle_sidebar and delete_session_handler to ScratchpadWindow
handlers_code = """
    def toggle_sidebar(self):
        self.sidebar_widget.setVisible(not self.sidebar_widget.isVisible())

    def delete_session_handler(self, session_id: str):
        from cat_talker.db import delete_session
        delete_session(session_id)
        if self.current_session_id == session_id:
            self.current_session_id = None
            while self.chat_feed_layout.count() > 0:
                item = self.chat_feed_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self.chat_history = []
            self.chat_feed_layout.addStretch()
            self.session_title_label.setText("<b>Chat with Assistant</b>")
            
            sessions = get_sessions_with_counts()
            if sessions:
                self.current_session_id = sessions[0]["id"]
                messages = get_messages(self.current_session_id)
                self.chat_history = messages
                display_title = sessions[0]["title"]
                if display_title == "New Chat":
                    display_title = get_session_title_preview(self.current_session_id, max_len=24)
                self.session_title_label.setText(f"<b>{display_title}</b>")
                
                # Render messages
                while self.chat_feed_layout.count() > 0:
                    item = self.chat_feed_layout.takeAt(0)
                    if item.widget():
                        item.widget().deleteLater()
                        
                for msg in messages:
                    is_error_msg = msg["content"].startswith("ERROR:")
                    bubble = MessageBubble(msg["role"], msg["content"], is_error=is_error_msg)
                    self.chat_feed_layout.insertWidget(self.chat_feed_layout.count() - 1, bubble)
                
                self.chat_feed_scroll_area.verticalScrollBar().setValue(self.chat_feed_scroll_area.verticalScrollBar().maximum())
        
        self.refresh_sidebar()

"""

if "def toggle_sidebar" not in content:
    content = content.replace("    def adjust_input_bar_height", handlers_code + "    def adjust_input_bar_height")

# 3. Add sidebar toggle button to top bar
sidebar_tgl_code = """
        self.sidebar_toggle_btn = QPushButton("☰")
        self.sidebar_toggle_btn.setObjectName("sidebar_toggle_btn")
        self.sidebar_toggle_btn.setStyleSheet("QPushButton { background: transparent; border: none; font-size: 18px; color: #c0caf5; margin-right: 10px; } QPushButton:hover { color: #ffffff; }")
        self.sidebar_toggle_btn.clicked.connect(self.toggle_sidebar)
        top_bar_layout.addWidget(self.sidebar_toggle_btn)
"""
if "self.sidebar_toggle_btn" not in content:
    content = content.replace('self.session_title_label = QLabel("<b>Chat with Assistant</b>")', sidebar_tgl_code + '\n        self.session_title_label = QLabel("<b>Chat with Assistant</b>")')

# 4. Save sidebar_widget as self.sidebar_widget
content = content.replace("sidebar_widget = QWidget()", "self.sidebar_widget = QWidget()\n        sidebar_widget = self.sidebar_widget")

# 5. Fix refresh_sidebar to use SessionRowWidget
old_add_item_logic = """                    item = QListWidgetItem(display_title)
                    item.setData(Qt.ItemDataRole.UserRole, s["id"])
                    if s["id"] == self.current_session_id:
                        item.setData(Qt.ItemDataRole.WhatsThisRole, "active-session") 
                    self.session_list.addItem(item)"""

new_add_item_logic = """                    item = QListWidgetItem(self.session_list)
                    item.setData(Qt.ItemDataRole.UserRole, s["id"])
                    is_active = (s["id"] == self.current_session_id)
                    if is_active:
                        item.setData(Qt.ItemDataRole.WhatsThisRole, "active-session")
                    row_widget = SessionRowWidget(s["id"], display_title, is_active)
                    row_widget.delete_clicked.connect(self.delete_session_handler)
                    item.setSizeHint(row_widget.sizeHint())
                    self.session_list.setItemWidget(item, row_widget)"""

content = content.replace(old_add_item_logic, new_add_item_logic)


# Let's fix window flags in main.py
with open("src/cat_talker/main.py", "r") as f:
    main_content = f.read()

old_flags = """        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | 
            Qt.WindowType.WindowStaysOnTopHint | 
            Qt.WindowType.SubWindow |
            Qt.WindowType.ToolTip
        )"""
new_flags = """        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
        )"""

main_content = main_content.replace(old_flags, new_flags)

with open("src/cat_talker/ui_scratchpad.py", "w") as f:
    f.write(content)
    
with open("src/cat_talker/main.py", "w") as f:
    f.write(main_content)

print("Patched.")
