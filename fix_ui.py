import re

with open("src/cat_talker/ui_scratchpad.py", "r") as f:
    content = f.read()

# --- FIX 1: QScrollArea Background ---

# Add ID to chat_feed_content_widget
content = content.replace("self.chat_feed_content_widget = QWidget()", 
                          'self.chat_feed_content_widget = QWidget()\n        self.chat_feed_content_widget.setObjectName("chat_container")')

# Inject styles into QSS_STYLES
qss_scroll = """
QScrollArea#chat_feed_scroll_area {
    background-color: #16161e;
    border: none;
}
QScrollArea#chat_feed_scroll_area > QWidget > QWidget {
    background-color: #16161e;
}
QWidget#chat_container {
    background-color: #16161e;
}
"""
content = content.replace("QScrollArea#chat_feed_scroll_area {\n    border: none;\n    background-color: transparent;\n}", qss_scroll)

# Apply viewport style
old_scroll_setup = """        self.chat_feed_scroll_area = QScrollArea()
        self.chat_feed_scroll_area.setObjectName("chat_feed_scroll_area")
        self.chat_feed_scroll_area.setWidgetResizable(True)
        self.chat_feed_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)"""

new_scroll_setup = """        self.chat_feed_scroll_area = QScrollArea()
        self.chat_feed_scroll_area.setObjectName("chat_feed_scroll_area")
        self.chat_feed_scroll_area.setWidgetResizable(True)
        self.chat_feed_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chat_feed_scroll_area.viewport().setStyleSheet("background-color: #16161e;")"""
if "viewport().setStyleSheet" not in content:
    content = content.replace(old_scroll_setup, new_scroll_setup)

# --- FIX AI TEXT STYLES ---
ai_style = """QTextBrowser.ai-bubble {
    background-color: transparent; 
    color: #e0e6f8; 
    border: none;
    padding: 6px 12px;
}"""
content = re.sub(r'QTextBrowser\.ai-bubble \{[^}]+\}', ai_style, content)

# Change Pygments style to Dracula/Dark background for code blocks
# Assuming it was set to Dracula, but we can set ".codehilite" directly in the stylesheet:
code_hilite = """.codehilite {
    background-color: #1f2335;
    color: #c0caf5;
    padding: 10px;
    border-radius: 8px;
    font-family: monospace;
    font-size: 13px;
}"""
content = re.sub(r'\.codehilite\s*\{\s*background-color:[^}]+\}', code_hilite, content)

# --- FIX 2: Chronological Flow (Replace insertWidget with addWidget and remove addStretch) ---

# Remove initial stretches
content = content.replace("self.chat_feed_layout.addStretch()", "")
# Except we actually want no stretches at all.
content = content.replace("self.chat_feed_layout.insertWidget(self.chat_feed_layout.count() - 1,", "self.chat_feed_layout.addWidget(")

# The user bubble was right aligned, maybe its stretch was messed up?
# Let's fix the user bubble alignment in MessageBubble
bubble_layout_user = """        if self.role == "user":
            self.text_display.setProperty("class", "user-bubble")
            spacer = QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            layout.addSpacerItem(spacer)
            layout.addWidget(self.text_display, 72)
"""
bubble_layout_user_new = """        if self.role == "user":
            self.text_display.setProperty("class", "user-bubble")
            spacer = QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            layout.addSpacerItem(spacer)
            layout.addWidget(self.text_display)
"""
content = content.replace(bubble_layout_user, bubble_layout_user_new)

# Force the scroll to bottom correctly via QTimer or direct
scroll_method = """        self.chat_feed_scroll_area.verticalScrollBar().setValue(self.chat_feed_scroll_area.verticalScrollBar().maximum())"""
new_scroll_method = """        QTimer.singleShot(10, lambda: self.chat_feed_scroll_area.verticalScrollBar().setValue(self.chat_feed_scroll_area.verticalScrollBar().maximum()))"""
content = content.replace(scroll_method, new_scroll_method)

# --- FIX 3: Sidebar SessionRows ---

session_widget_code = """class SessionRowWidget(QWidget):
    delete_clicked = pyqtSignal(str)

    def __init__(self, session_id, title, is_active, parent=None):
        super().__init__(parent)
        self.session_id = session_id
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 8, 6)
        layout.setSpacing(8)
        
        # Session title label
        self.label = QLabel(title)
        if is_active:
            self.label.setStyleSheet("color: #bd93f9; font-size: 13px; font-weight: bold;")
        else:
            self.label.setStyleSheet("color: #c0caf5; font-size: 13px; font-weight: 500;")
        layout.addWidget(self.label, stretch=1)
        
        # Delete action button
        self.delete_btn = QPushButton("✕")
        self.delete_btn.setFixedSize(22, 22)
        self.delete_btn.setStyleSheet(\"\"\"
            QPushButton {
                background: transparent;
                color: #565f89;
                border: none;
                border-radius: 4px;
                font-weight: bold;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #f7768e;
                color: #ffffff;
            }
        \"\"\")
        self.delete_btn.clicked.connect(lambda: self.delete_clicked.emit(self.session_id))
        layout.addWidget(self.delete_btn, stretch=0)
        
        self.setFixedHeight(38)
"""
# Replace old SessionRowWidget
content = re.sub(r'class SessionRowWidget\(QWidget\):.*?layout\.addWidget\(self\.delete_btn\)', session_widget_code + "        # Replaced", content, flags=re.DOTALL)

# Find where it puts it in the list and set sizeHint manually
old_add_item = """                    row_widget = SessionRowWidget(s["id"], display_title, is_active)
                    row_widget.delete_clicked.connect(self.delete_session_handler)
                    item.setSizeHint(row_widget.sizeHint())"""
new_add_item = """                    row_widget = SessionRowWidget(s["id"], display_title, is_active)
                    row_widget.delete_clicked.connect(self.delete_session_handler)
                    item.setSizeHint(QSize(row_widget.sizeHint().width(), 38))"""
content = content.replace(old_add_item, new_add_item)

# Fix Category headers ("Today")
old_group = """                group_item.setForeground(QColor(COLORS['comment']))
                font = group_item.font()
                font.setBold(True)
                group_item.setFont(font)"""
new_group = """                group_item.setFlags(group_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                # Ensure height and style
                group_widget = QLabel(f"-- {title} --")
                group_widget.setStyleSheet("color: #7aa2f7; font-size: 11px; font-weight: bold; padding: 6px 0; background: transparent;")
                group_item.setSizeHint(QSize(200, 24))
                self.session_list.addItem(group_item)
                self.session_list.setItemWidget(group_item, group_widget)
                # Next we should intercept the default addItem below so it doesn't double add!"""

# To safely replace the add_session_group, I'll just rewrite it block
new_add_session_group = """        def add_session_group(title: str, session_list_data: List[Dict[str, Any]]):
            if session_list_data:
                group_item = QListWidgetItem()
                group_item.setFlags(group_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                self.session_list.addItem(group_item)
                group_widget = QLabel(f"-- {title} --")
                group_widget.setStyleSheet("color: #7aa2f7; font-size: 11px; font-weight: bold; padding: 6px 5px; background: transparent;")
                group_item.setSizeHint(QSize(200, 30))
                self.session_list.setItemWidget(group_item, group_widget)
                
                for s in session_list_data:
                    display_title = s["title"]
                    if display_title == "New Chat":
                        display_title = get_session_title_preview(s["id"], max_len=24)
                    
                    item = QListWidgetItem(self.session_list)
                    item.setData(Qt.ItemDataRole.UserRole, s["id"])
                    is_active = (s["id"] == self.current_session_id)
                    if is_active:
                        item.setData(Qt.ItemDataRole.WhatsThisRole, "active-session")
                    row_widget = SessionRowWidget(s["id"], display_title, is_active)
                    row_widget.delete_clicked.connect(self.delete_session_handler)
                    item.setSizeHint(QSize(row_widget.sizeHint().width(), 38))
                    self.session_list.setItemWidget(item, row_widget)"""

content = re.sub(r'        def add_session_group.*?self\.session_list\.setItemWidget\(item, row_widget\)', new_add_session_group, content, flags=re.DOTALL)


# --- FIX 4: Input Island Style ---
input_bar_style = """
QFrame#input_bar_widget {
    background-color: #16161e;
    border-top: 1px solid rgba(255, 255, 255, 0.06);
    padding: 10px;
}
QTextEdit#input_box {
    background-color: #1f2335; 
    border: 1px solid #3b4261; 
    border-radius: 12px; 
    padding: 10px 14px; 
    color: #ffffff;
    font-family: inherit;
}
QPushButton#send_btn {
    background-color: #7aa2f7; 
    color: #15161e; 
    border-radius: 8px; 
    font-weight: bold;
    padding: 10px 15px;
    border: none;
}
"""
content = re.sub(r'QFrame#input_bar_widget\s*\{[^}]+\}', "QFrame#input_bar_widget {\n    background-color: #16161e;\n    border-top: 1px solid rgba(255, 255, 255, 0.06);\n}", content)
content = content.replace("input_bar_layout.setContentsMargins(20, 8, 20, 16)", "input_bar_layout.setContentsMargins(18, 10, 18, 16)")

with open("src/cat_talker/ui_scratchpad.py", "w") as f:
    f.write(content)
print("Applied UI fixes.")
