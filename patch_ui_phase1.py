import re

with open("src/cat_talker/main.py", "r") as f:
    content = f.read()

# 1. Add edge snapping logic and animations to RadialVisualizerWindow
snap_logic = """
    def _snap_to_nearest_edge(self):
        screen_geo = self.screen().geometry()
        window_geo = self.frameGeometry()
        
        center_x = window_geo.center().x()
        center_y = window_geo.center().y()
        
        margin = 40
        
        # Determine closest X
        dist_left = center_x - screen_geo.left()
        dist_right = screen_geo.right() - center_x
        
        if dist_left < dist_right:
            target_x = screen_geo.left() + margin
        else:
            target_x = screen_geo.right() - window_geo.width() - margin + 1
            
        # Determine closest Y
        dist_top = center_y - screen_geo.top()
        dist_bottom = screen_geo.bottom() - center_y
        
        if dist_top < dist_bottom:
            target_y = screen_geo.top() + margin
        else:
            target_y = screen_geo.bottom() - window_geo.height() - margin + 1

        # Allow bottom-center snap if in the middle 30% of screen horizontally
        screen_width = screen_geo.width()
        if screen_geo.left() + screen_width * 0.35 < center_x < screen_geo.right() - screen_width * 0.35:
            target_x = screen_geo.left() + (screen_width - window_geo.width()) // 2
            
        # Smooth animation to target
        self.snap_anim = QPropertyAnimation(self, b"pos")
        self.snap_anim.setDuration(300)
        self.snap_anim.setStartValue(self.pos())
        self.snap_anim.setEndValue(QPointF(target_x, target_y).toPoint())
        self.snap_anim.setEasingCurve(QEasingCurve.Type.OutBack)
        self.snap_anim.start()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        self._snap_to_nearest_edge()
"""

# Replace the original mouseReleaseEvent
content = re.sub(r'    def mouseReleaseEvent\(self, event\):\n        self\._drag_pos = None', snap_logic.strip('\n'), content)

# 2. Add 'always_on_top' and 'is_muted' to __init__
init_vars = """
        # Other state
        self.always_on_top = False
        self.is_muted = False
"""
content = content.replace("        # Other state", init_vars)

# 3. Modify Context Menu
old_context_menu = """    def contextMenuEvent(self, event):
        menu = QMenu(self)
        
        act_bottom_center = menu.addAction("📍 Snap to Bottom Center")
        act_bottom_right = menu.addAction("📍 Snap to Bottom Right")
        act_center = menu.addAction("📍 Snap to Center")
        menu.addSeparator()
        
        act_hide = menu.addAction("👁️ Hide (Run `pkill -SIGUSR1 -f cat_talker.main` to unhide)")
        act_quit = menu.addAction("❌ Quit Assistant")

        action = menu.exec(event.globalPos())

        if action == act_bottom_center:
            self.position_bottom_center()
        elif action == act_bottom_right:
            self.position_bottom_right()
        elif action == act_center:
            self.position_center()
        elif action == act_hide:
            self.hide()
        elif action == act_quit:
            QApplication.quit()"""

new_context_menu = """    def contextMenuEvent(self, event):
        menu = QMenu(self)
        
        # Pinning
        pin_text = "📌 Unpin from Top" if self.always_on_top else "📌 Pin to Top"
        act_pin = menu.addAction(pin_text)
        
        # Muting
        mute_text = "🔊 Unmute Microphone" if self.is_muted else "🔇 Mute Microphone"
        act_mute = menu.addAction(mute_text)
        
        menu.addSeparator()
        
        act_bottom_center = menu.addAction("📍 Snap to Bottom Center")
        act_bottom_right = menu.addAction("📍 Snap to Bottom Right")
        act_center = menu.addAction("📍 Snap to Center")
        menu.addSeparator()
        
        act_hide = menu.addAction("👁️ Hide (Run `pkill -SIGUSR1 -f cat_talker.main` to unhide)")
        act_quit = menu.addAction("❌ Quit Assistant")

        action = menu.exec(event.globalPos())

        if action == act_pin:
            self.always_on_top = not self.always_on_top
            flags = self.windowFlags()
            if self.always_on_top:
                flags |= Qt.WindowType.WindowStaysOnTopHint
            else:
                flags &= ~Qt.WindowType.WindowStaysOnTopHint
            self.setWindowFlags(flags)
            self.show()
        elif action == act_mute:
            self.is_muted = not self.is_muted
            # Emit a signal or directly update config (we'll emit a global signal or handle it later)
            QMessageBox.information(self, "Mute", "Mute toggled! (Integration pending)")
        elif action == act_bottom_center:
            self.position_bottom_center()
        elif action == act_bottom_right:
            self.position_bottom_right()
        elif action == act_center:
            self.position_center()
        elif action == act_hide:
            self.hide()
        elif action == act_quit:
            QApplication.quit()"""

content = content.replace(old_context_menu, new_context_menu)

with open("src/cat_talker/main.py", "w") as f:
    f.write(content)

print("Patch applied to main.py")
