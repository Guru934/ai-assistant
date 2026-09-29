import re

with open("src/cat_talker/main.py", "r") as f:
    content = f.read()

# We want to replace the exact paintEvent block.
# We'll locate "def paintEvent(self, event):" and the end of the method (which ends at "painter.end()")
import re

start_idx = content.find("def paintEvent(self, event):")
end_idx = content.find("painter.end()", start_idx) + len("painter.end()")

replacement = """def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(self.vis_widget.pos())

        center_x = self.vis_widget.width() / 2
        center_y = self.vis_widget.height() / 2
        
        # Scale everything inside the visualizer widget by the bass if talking
        bass = self.bass_scale if self.current_state == 'talking' else 1.0
        painter.translate(center_x, center_y)
        painter.scale(bass, bass)
        
        base_radius = 75

        # ---- Draw outer state ring ----
        if self.current_state == 'talking':
            # Dynamic spectrum spikes (existing logic)
            max_spike_height = 50
            bins_count = len(self.freq_bins)
            angle_step = (2 * math.pi) / max(1, bins_count)

            for i, val in enumerate(self.freq_bins):
                spike_h = val * max_spike_height
                angle = i * angle_step - (math.pi / 2) # start top

                r_inner = base_radius + 5
                x1 = math.cos(angle) * r_inner
                y1 = math.sin(angle) * r_inner

                r_outer = r_inner + spike_h
                x2 = math.cos(angle) * r_outer
                y2 = math.sin(angle) * r_outer

                bar_hue = (self.hue_phase + (i / bins_count)) % 1.0
                color = QColor.fromHsvF(bar_hue, 0.9, 1.0)
                pen = QPen(color, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)

                painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
                
                # Glow
                glow_pen = QPen(color, 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                painter.setPen(glow_pen)
                painter.setOpacity(0.4)
                painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
                painter.setOpacity(1.0)

        elif self.is_muted:
            # Muted: Dim red ring
            painter.setPen(QPen(QColor(200, 40, 40, 180), 5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(0, 0), base_radius + 8, base_radius + 8)

        elif self.current_state == 'thinking':
            # Spinning dashed neon ring (e.g. waiting for API)
            painter.save()
            painter.rotate(self.hue_phase * 360 * 1.5) # Spin speed
            pen = QPen(QColor(180, 80, 255), 6, Qt.PenStyle.DashLine, Qt.PenCapStyle.RoundCap)
            pen.setDashPattern([3, 4])
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(0, 0), base_radius + 10, base_radius + 10)
            painter.restore()

        elif self.current_state == 'listening':
            # Steady cyan ring (active microphone)
            painter.setPen(QPen(QColor(0, 255, 200, 200), 5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(0, 0), base_radius + 8, base_radius + 8)

        else: # idle
            # Breathing glow
            import math
            breathe = (math.sin(self.hue_phase * math.pi * 4) + 1) / 2 # 0.0 to 1.0
            opacity = 0.15 + (breathe * 0.4) # 0.15 to 0.55
            painter.setPen(QPen(QColor(150, 150, 170, int(255 * opacity)), 4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(0, 0), base_radius + 6, base_radius + 6)

        # ---- Draw avatar ----
        avatar_radius = base_radius
        path = QPainterPath()
        path.addEllipse(QPointF(0, 0), avatar_radius, avatar_radius)
        painter.setClipPath(path)

        if self.avatar_pixmap:
            scaled_pixmap = self.avatar_pixmap.scaled(
                int(avatar_radius * 2), int(avatar_radius * 2),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation
            )
            # Need to cast radius to int for drawPixmap
            painter.drawPixmap(int(-avatar_radius), int(-avatar_radius), scaled_pixmap)
        else:
            painter.setBrush(QBrush(QColor(20, 20, 20)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(0, 0), avatar_radius, avatar_radius)
            
            painter.setBrush(Qt.BrushStyle.NoBrush)
            glow_ring_color = QColor.fromHsvF(self.hue_phase, 0.9, 1.0)
            painter.setPen(QPen(glow_ring_color, 4))
            painter.drawEllipse(QPointF(0, 0), avatar_radius - 2, avatar_radius - 2)

        painter.end()"""

new_content = content[:start_idx] + replacement + content[end_idx:]

with open("src/cat_talker/main.py", "w") as f:
    f.write(new_content)

print("Phase 2 patch applied")
