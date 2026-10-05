import sys
import os
import signal
# X11 for dragging/snapping on Wayland/Hyprland; a pre-set platform (e.g.
# offscreen for tests) is honored.
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
import datetime
import threading
import math
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import QWidget, QMenu, QMessageBox, QLabel, QVBoxLayout
from PyQt6.QtWidgets import (QDialog, QCheckBox, QLineEdit, QDoubleSpinBox,
                             QFormLayout, QDialogButtonBox)
from PyQt6.QtGui import QPainter, QColor, QBrush, QAction, QPen, QFont, QPainterPath, QPixmap
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal, QPointF, QEasingCurve, QPropertyAnimation, pyqtProperty

from cat_talker.agent import start_agent_in_thread
from cat_talker.control import serve_forever
from cat_talker.dictation import DictationManager
from cat_talker.logging_config import get_logger

logger = get_logger("cat_talker.main")

# ─── Design Tokens ──────────────────────────────────────────────
COLORS = {
    "bubble_bg": QColor(30, 30, 40, 230),
    "bubble_text": QColor(240, 240, 245),
}

# Speech-bubble text zone: full-width box at the top of the window,
# wrapping inside these bounds (never clipped, never overlapping).
BUBBLE_TEXT_MAX_W = 268
BUBBLE_TEXT_MAX_H = 112
BUBBLE_PAD_X = 14
BUBBLE_PAD_Y = 10
BUBBLE_TOP = 8
# Avatar re-layout while the bubble shows: shift down + shrink so the
# bubble zone and the avatar never overlap. Identity when hidden.
BUBBLE_AVATAR_CY = 222
BUBBLE_AVATAR_SCALE = 0.85

class SettingsDialog(QDialog):
    """Chibi settings over the existing config/memory boundaries.

    Same process, same window hierarchy (parented to the overlay): no
    second application instance, main thread only. Save validates every
    field first - nothing persists when anything is invalid. Wake-word
    edits apply on the next sleep cycle (the detector reads config per
    nap); F2 always keeps working.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        from cat_talker import settings as settings_mod
        self._settings_mod = settings_mod
        self.setWindowTitle("Chibi Settings")
        self.setMinimumWidth(380)
        self.setStyleSheet(
            "QDialog { background-color: #1e1e28; }"
            "QLabel { color: #f0f0f5; }"
            "QCheckBox { color: #f0f0f5; }"
            "QLineEdit, QDoubleSpinBox { background-color: #2a2a38; "
            "color: #f0f0f5; border: 1px solid #4a4a5a; "
            "border-radius: 4px; padding: 3px; }"
            "QPushButton { background-color: #3a3a4c; color: #f0f0f5; "
            "border-radius: 4px; padding: 5px 14px; }"
            "QPushButton:hover { background-color: #4a4a5e; }")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.wake_enabled = QCheckBox("Listen for the wake phrase while asleep")
        self.wake_enabled.setToolTip(
            "Optional and off by default. F2 always wakes manually.")
        self.wake_phrase = QLineEdit()
        self.wake_model = QLineEdit()
        self.wake_model.setToolTip(
            "Built-in model key (e.g. hey_jarvis) or a custom .onnx path.")
        self.wake_threshold = QDoubleSpinBox()
        self.wake_threshold.setRange(0.01, 1.0)
        self.wake_threshold.setSingleStep(0.05)
        self.wake_threshold.setDecimals(2)
        self.monitor = QLineEdit()
        self.voice_approval = QCheckBox("Ask before risky actions")
        self.echo_suppress = QCheckBox("Suppress speaker echo from mic")
        self.auto_reconnect = QCheckBox("Auto-reconnect on drops")
        self.language = QLineEdit()
        self.response_style = QLineEdit()
        self.weather_location = QLineEdit()
        self.api_status = QLabel()
        self.api_new = QLineEdit()
        self.api_new.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_new.setPlaceholderText("Paste a replacement key here")
        self.api_clear = QCheckBox("Remove the stored key on save")
        form.addRow("Wake word", self.wake_enabled)
        form.addRow("Wake phrase", self.wake_phrase)
        form.addRow("Wake model", self.wake_model)
        form.addRow("Wake threshold", self.wake_threshold)
        form.addRow("Preferred monitor", self.monitor)
        form.addRow("Voice approval", self.voice_approval)
        form.addRow("Echo suppression", self.echo_suppress)
        form.addRow("Auto-reconnect", self.auto_reconnect)
        form.addRow("Language", self.language)
        form.addRow("Response style", self.response_style)
        form.addRow("Weather location", self.weather_location)
        form.addRow("API key", self.api_status)
        form.addRow("New API key", self.api_new)
        form.addRow("", self.api_clear)
        layout.addLayout(form)
        note = QLabel("Wake-word changes apply on the next sleep cycle. "
                      "F2 always wakes manually.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.error_label = QLabel()
        self.error_label.setStyleSheet("QLabel { color: #ff8080; }")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.load_from_snapshot(settings_mod.load_snapshot())

    def load_from_snapshot(self, snapshot):
        """Fill widgets from a settings snapshot (dict)."""
        self.wake_enabled.setChecked(bool(snapshot.get("wake_word_enabled", False)))
        self.wake_phrase.setText(str(snapshot.get("wake_word_phrase", "")))
        self.wake_model.setText(str(snapshot.get("wake_word_model", "")))
        try:
            self.wake_threshold.setValue(float(snapshot.get("wake_word_threshold", 0.5)))
        except (TypeError, ValueError):
            self.wake_threshold.setValue(0.5)
        self.monitor.setText(str(snapshot.get("preferred_monitor", "")))
        self.voice_approval.setChecked(bool(snapshot.get("voice_approval_enabled", True)))
        self.echo_suppress.setChecked(bool(snapshot.get("echo_suppress_enabled", True)))
        self.auto_reconnect.setChecked(bool(snapshot.get("auto_reconnect", True)))
        self.language.setText(str(snapshot.get("preferred_language", "")))
        self.response_style.setText(str(snapshot.get("response_style", "")))
        self.weather_location.setText(str(snapshot.get("weather_location", "")))
        if snapshot.get("api_key_configured"):
            self.api_status.setText("Configured (value hidden)")
        else:
            self.api_status.setText("Not configured")
        self.api_new.clear()
        self.api_clear.setChecked(False)
        self.error_label.hide()

    def gather_values(self):
        """Read widgets into a snapshot-shaped dict (no persistence)."""
        return {
            "wake_word_enabled": self.wake_enabled.isChecked(),
            "wake_word_phrase": self.wake_phrase.text(),
            "wake_word_model": self.wake_model.text(),
            "wake_word_threshold": self.wake_threshold.value(),
            "preferred_monitor": self.monitor.text(),
            "voice_approval_enabled": self.voice_approval.isChecked(),
            "echo_suppress_enabled": self.echo_suppress.isChecked(),
            "auto_reconnect": self.auto_reconnect.isChecked(),
            "preferred_language": self.language.text(),
            "response_style": self.response_style.text(),
            "weather_location": self.weather_location.text(),
            "api_key_new": self.api_new.text(),
            "api_key_clear": self.api_clear.isChecked(),
        }

    def _on_save(self):
        values = self.gather_values()
        errors = self._settings_mod.validate_settings(values)
        if errors:
            self.error_label.setText("; ".join(errors))
            self.error_label.show()
            return
        ok, message = self._settings_mod.apply_settings(values)
        if not ok:
            self.error_label.setText(message)
            self.error_label.show()
            return
        self.error_label.hide()
        self.accept()


class RadialVisualizerWindow(QWidget):
    # Signals from agent thread
    audio_signal = pyqtSignal(float, float, list)  # vol, bass, freq_bins
    quit_signal = pyqtSignal()
    text_signal = pyqtSignal(str, str)             # role, text
    state_signal = pyqtSignal(str)                 # idle|listening|thinking|talking
    bubble_signal = pyqtSignal(str)                # caption text
    glow_signal = pyqtSignal(str)                  # connected|processing|vision|thinking

    def __init__(self):
        import os as _os
        import threading as _threading
        super().__init__()
        # DEBUG-only lifecycle trace (no continuous logging, events only).
        logger.debug(
            "ui lifecycle: constructed RadialVisualizerWindow id=%s "
            "pid=%s thread=%s top_level=%d",
            id(self), _os.getpid(), _threading.current_thread().name,
            len(QApplication.topLevelWidgets()) if QApplication.instance()
            else -1,
        )
        try:
            self.destroyed.connect(
                lambda obj=None: logger.debug(
                    "ui lifecycle: destroyed RadialVisualizerWindow"))
        except Exception:
            pass
        
        # Audio state
        self.volume = 0.0
        self.bass_scale = 1.0
        self.target_bass_scale = 1.0
        self.freq_bins = [0.0] * 64
        self.target_freq_bins = [0.0] * 64


        # Other state
        self.always_on_top = False
        self.is_muted = False

        self.current_state = "idle"
        self.bubble_text = ""
        self._bubble_opacity = 0.0
        self._bubble_gen = 0
        self.glow_state = "connected"
        self.hue_phase = 0.0

        # ── Window: Frameless, Transparent, Always-on-Top ─────────
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent;")
        
        self.resize(320, 320)  

        # ── Avatar Image Loading ─────────────────────────────────
        self.avatar_pixmap = QPixmap("assets/avatar.png")
        if self.avatar_pixmap.isNull():
            self.avatar_pixmap = None

        # ── Layout: Just the visualizer widget ────────────────────
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        self.vis_widget = QWidget()
        self.vis_widget.setFixedSize(300, 300)
        self.vis_widget.setStyleSheet("background: transparent;")
        layout.addWidget(self.vis_widget, alignment=Qt.AlignmentFlag.AlignCenter)

        # ── Speech Bubble (painted in-window) ────────────────────────
        # A child widget or separate window above the avatar gets clipped
        # (negative y inside the 320x320 parent) or mismanaged by the
        # compositor. Painting the bubble directly in paintEvent keeps it
        # always inside the renderable window: no second window, no
        # positioning fights, transparency and fade behavior unchanged.
        self._bubble_visible = False

        self.bubble_anim = QPropertyAnimation(self, b"bubble_opacity")
        self.bubble_anim.setDuration(400)
        self.bubble_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # ── Animation Timer ────────────────────────────────────────
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self._tick_animation)
        self.anim_timer.start(16) # ~60 FPS

        # ── Signal connections ─────────────────────────────────────
        self.audio_signal.connect(self._on_audio)
        self.quit_signal.connect(self.close)
        self.text_signal.connect(self._on_text)
        self.state_signal.connect(self._on_state)
        self.bubble_signal.connect(self._on_bubble)
        self.glow_signal.connect(self._on_glow)

        self._drag_pos = None

    # ─── Properties for Animation ────────────────────────────────
    def _get_bubble_opacity(self):
        return self._bubble_opacity

    def _set_bubble_opacity(self, val):
        self._bubble_opacity = val
        self._bubble_visible = val > 0.01

    bubble_opacity = pyqtProperty(float, _get_bubble_opacity, _set_bubble_opacity)

    # ─── Positioning Definitions ──────────────────────────────────
    def position_bottom_center(self):
        screen = QApplication.primaryScreen().geometry()
        x = (screen.width() - self.width()) // 2
        y = screen.height() - self.height() - 40
        self.move(x, y)

    def position_bottom_right(self):
        screen = QApplication.primaryScreen().geometry()
        x = screen.width() - self.width() - 40
        y = screen.height() - self.height() - 40
        self.move(x, y)

    def position_center(self):
        screen = QApplication.primaryScreen().geometry()
        x = (screen.width() - self.width()) // 2
        y = (screen.height() - self.height()) // 2
        self.move(x, y)

    # ─── Animation Loop ──────────────────────────────────────────
    def _tick_animation(self):
        # Update Hue for rainbow colors
        self.hue_phase = (self.hue_phase + 0.008) % 1.0

        # Smoothly interpolate frequency bins
        for i in range(len(self.freq_bins)):
            diff = self.target_freq_bins[i] - self.freq_bins[i]
            if diff > 0:
                self.freq_bins[i] += diff * 0.4
            else:
                self.freq_bins[i] += diff * 0.15
            self.target_freq_bins[i] *= 0.8
            
        # Smoothly interpolate bass scale
        diff_bass = self.target_bass_scale - self.bass_scale
        if diff_bass > 0:
            self.bass_scale += diff_bass * 0.5
        else:
            self.bass_scale += diff_bass * 0.1
            
        self.target_bass_scale = max(1.0, self.target_bass_scale * 0.9)
        
        self.vis_widget.update()

    # ─── Signal Handlers ─────────────────────────────────────────
    def _on_audio(self, vol: float, bass: float, bins: list):
        self.volume = vol
        self.target_bass_scale = bass
        if len(bins) == 64:
            self.target_freq_bins = bins

    def _on_text(self, role: str, text: str):
        if role == "model_delta":
            # Streaming assistant text: live bubble display only, never
            # history (the completed "model" response is logged once).
            if text.strip():
                self._on_bubble(text)
            return
        if not text.strip():
            return
        try:
            log_path = os.path.expanduser("~/.cat_talker_history.txt")
            with open(log_path, "a", encoding="utf-8") as lf:
                timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                prefix = "📻 System: " if role == "model" else "🗣️ You: "
                lf.write(f"[{timestamp}] {prefix}{text}\n")
        except Exception:
            pass

    def _on_state(self, state: str):
        if state in ["idle", "listening", "thinking", "talking", "dictating"]:
            self.current_state = state

    def _on_bubble(self, text: str):
        self.bubble_text = text
        self._bubble_gen += 1
        gen = self._bubble_gen
        self.bubble_anim.stop()
        self.bubble_anim.setStartValue(0.0)
        self.bubble_anim.setEndValue(1.0)
        self.bubble_anim.start()
        QTimer.singleShot(
            3000, lambda gen=gen: self._fade_bubble(gen))

    def bubble_rect(self):
        """Bubble background box in window coords (for paint and tests).

        Kept for compatibility; identical to _bubble_box()."""
        return self._bubble_box()

    def _fade_bubble(self, gen=None):
        # Generation-guarded: a timer armed by an older message must never
        # fade a newer one (also makes overlapping bubbles deterministic).
        if gen is not None and gen != self._bubble_gen:
            return
        if self._bubble_opacity > 0.01:
            self.bubble_anim.stop()
            self.bubble_anim.setStartValue(self._bubble_opacity)
            self.bubble_anim.setEndValue(0.0)
            self.bubble_anim.start()

    def _on_glow(self, state: str):
        self.glow_state = state

    def _bubble_font(self):
        font = QFont("Outfit", 15)
        return font

    def _bubble_box(self):
        """Bubble background box in window coords, from current text.

        Always fully inside the window: full width with side margins,
        text wrapped and capped so the box ends well above the avatar
        zone. Returns (x, y, w, h). Empty text gives a zero box.
        """
        from PyQt6.QtGui import QFontMetrics
        if not self.bubble_text:
            return (0, 0, 0, 0)
        metrics = QFontMetrics(self._bubble_font())
        inner = metrics.boundingRect(
            0, 0, BUBBLE_TEXT_MAX_W, 10000,
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            self.bubble_text)
        text_h = min(inner.height(), BUBBLE_TEXT_MAX_H)
        box_w = BUBBLE_TEXT_MAX_W + BUBBLE_PAD_X * 2
        box_h = text_h + BUBBLE_PAD_Y * 2
        box_x = (self.width() - box_w) // 2
        return (box_x, BUBBLE_TOP, box_w, box_h)

    def _paint_bubble(self, painter):
        """Draw the speech bubble box + wrapped text (window coords).

        Lives entirely inside the 320x320 window above the (shifted)
        avatar: rounded semi-opaque background for contrast over any
        wallpaper, 15px light text, fade opacity applied to both.
        """
        from PyQt6.QtCore import QRect
        box_x, box_y, box_w, box_h = self._bubble_box()
        if box_w <= 0 or box_h <= 0:
            return
        painter.save()
        painter.setOpacity(self._bubble_opacity)
        painter.setBrush(COLORS["bubble_bg"])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(box_x, box_y, box_w, box_h, 12, 12)
        painter.setPen(COLORS["bubble_text"])
        painter.setFont(self._bubble_font())
        text_rect = QRect(box_x + BUBBLE_PAD_X,
                          box_y + BUBBLE_PAD_Y,
                          box_w - BUBBLE_PAD_X * 2,
                          box_h - BUBBLE_PAD_Y * 2)
        painter.drawText(text_rect,
                         Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                         self.bubble_text)
        painter.restore()

    # ─── Painting ─────────────────────────────────────────────────
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bubble_on = bool(self._bubble_visible and self.bubble_text)
        if bubble_on:
            self._paint_bubble(painter)

        painter.translate(self.vis_widget.pos())

        center_x = self.vis_widget.width() / 2
        center_y = self.vis_widget.height() / 2

        if bubble_on:
            # Make room: shift the avatar block down and shrink it so the
            # bubble zone above never overlaps the avatar. Identity when
            # the bubble is hidden (existing look untouched).
            painter.translate(150, BUBBLE_AVATAR_CY)
            painter.scale(BUBBLE_AVATAR_SCALE, BUBBLE_AVATAR_SCALE)
            painter.translate(-150, -150)
        
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

        elif self.current_state == 'dictating':
            # Solid amber ring indicating recording
            painter.setPen(QPen(QColor(255, 191, 0, 255), 6))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(0, 0), base_radius + 10, base_radius + 10)

        else: # idle
            # Breathing glow
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

        painter.end()

    # ─── Lifecycle tracing (DEBUG only, events only) ──────────────
    def showEvent(self, event):
        logger.debug("ui lifecycle: show id=%s visible=%s top_level=%d",
                     id(self), self.isVisible(),
                     len(QApplication.topLevelWidgets()))
        super().showEvent(event)

    def hideEvent(self, event):
        logger.debug("ui lifecycle: hide id=%s", id(self))
        super().hideEvent(event)

    def closeEvent(self, event):
        logger.debug("ui lifecycle: close id=%s", id(self))
        super().closeEvent(event)

    # ─── Mouse Drag ──────────────────────────────────────────────
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and hasattr(self, '_drag_pos') and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

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

    def _build_context_menu(self):
        """Build the right-click menu; returns (menu, actions by key).

        Extracted (no behavior change) so the wiring is testable without
        running the modal event loop. NOTE: the old "Mute Microphone"
        item is gone - its handler only ever showed an "Integration
        pending" popup, i.e. it was an unimplemented dead end.
        """
        menu = QMenu(self)

        # Pinning
        pin_text = "📌 Unpin from Top" if self.always_on_top else "📌 Pin to Top"
        act_pin = menu.addAction(pin_text)

        menu.addSeparator()

        act_bottom_center = menu.addAction("📍 Snap to Bottom Center")
        act_bottom_right = menu.addAction("📍 Snap to Bottom Right")
        act_center = menu.addAction("📍 Snap to Center")
        menu.addSeparator()

        act_hide = menu.addAction("👁️ Hide UI")
        act_settings = menu.addAction("⚙️ Settings")
        act_capture = menu.addAction("📸 Capture Active Window (pkill -SIGUSR2)")
        act_quit = menu.addAction("❌ Quit Assistant")

        actions = {
            "pin": act_pin,
            "bottom_center": act_bottom_center,
            "bottom_right": act_bottom_right,
            "center": act_center,
            "hide": act_hide,
            "settings": act_settings,
            "capture": act_capture,
            "quit": act_quit,
        }
        return menu, actions

    def _handle_menu_action(self, action, actions):
        if action == actions["pin"]:
            self.always_on_top = not self.always_on_top
            flags = self.windowFlags()
            if self.always_on_top:
                flags |= Qt.WindowType.WindowStaysOnTopHint
            else:
                flags &= ~Qt.WindowType.WindowStaysOnTopHint
            self.setWindowFlags(flags)
            self.show()
        elif action == actions["bottom_center"]:
            self.position_bottom_center()
        elif action == actions["bottom_right"]:
            self.position_bottom_right()
        elif action == actions["center"]:
            self.position_center()
        elif action == actions["hide"]:
            self.hide()
        elif action == actions["settings"]:
            self.open_settings()
        elif action == actions["capture"]:
            # Re-use the existing logic by sending SIGUSR2 to ourselves
            os.kill(os.getpid(), signal.SIGUSR2)
        elif action == actions["quit"]:
            QApplication.quit()

    def open_settings(self):
        """Show the settings dialog (main thread, modal, same process).

        Parented to the overlay: no second application or window
        instance. Modal exec() pumps the main-thread event loop, so the
        UI stays responsive and no worker ever touches these widgets.
        """
        dialog = SettingsDialog(self)
        dialog.exec()

    def contextMenuEvent(self, event):
        menu, actions = self._build_context_menu()
        action = menu.exec(event.globalPos())
        if action is not None:
            self._handle_menu_action(action, actions)

_overlay_window = None


def count_overlay_windows() -> int:
    """Testable invariant: top-level RadialVisualizerWindow instances."""
    app = QApplication.instance()
    if app is None:
        return 0
    return sum(isinstance(w, RadialVisualizerWindow)
               for w in QApplication.topLevelWidgets())


def get_overlay_window():
    """Process-wide singleton: exactly one overlay window, ever.

    Sleep/wake, F2, the control socket, and hide/show all reuse this
    instance - no path may construct a second top-level window. A repeat
    call returns the existing object (recreating only if it was deleted)
    and logs a warning so accidental second construction is visible.
    """
    global _overlay_window
    existing = _overlay_window
    if existing is not None:
        try:
            from PyQt6 import sip
            deleted = sip.isdeleted(existing)
        except Exception:
            deleted = False
        if not deleted:
            logger.warning(
                "ui lifecycle: get_overlay_window called twice - "
                "returning existing id=%s (no second window created)",
                id(existing),
            )
            return existing
        logger.debug("ui lifecycle: previous overlay was deleted; "
                     "creating a fresh singleton")
    _overlay_window = RadialVisualizerWindow()
    return _overlay_window


class UiBridge(QObject):
    """Thread-safe UI controls for the socket/control thread.

    Qt widgets live in the Qt/main thread and must only be touched
    there. This bridge exposes plain methods callable from ANY thread;
    each one emits a signal whose slot runs in the main thread via a
    queued connection. A cached `visible` flag (written in main-thread
    slots, read anywhere) answers status queries without touching QWidget
    off-thread.
    """

    show_requested = pyqtSignal()
    hide_requested = pyqtSignal()
    toggle_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(self, window_getter):
        super().__init__()
        self._window_getter = window_getter
        self.visible = True
        self.show_requested.connect(self._do_show)
        self.hide_requested.connect(self._do_hide)
        self.toggle_requested.connect(self._do_toggle)
        self.quit_requested.connect(self._do_quit)

    def _window(self):
        try:
            return self._window_getter()
        except Exception:
            return None

    def _do_show(self):
        window = self._window()
        if window is not None and window.isHidden():
            window.show()
        self.visible = False if window is None else not window.isHidden()

    def _do_hide(self):
        window = self._window()
        if window is not None and not window.isHidden():
            window.hide()
        self.visible = False if window is None else not window.isHidden()

    def _do_toggle(self):
        window = self._window()
        if window is None:
            return
        if window.isHidden():
            window.show()
        else:
            window.hide()
        self.visible = not window.isHidden()

    def _do_quit(self):
        QApplication.quit()

    # Callable from any thread; slots execute in the Qt/main thread.
    def show(self):
        self.show_requested.emit()

    def hide(self):
        self.hide_requested.emit()

    def toggle(self):
        self.toggle_requested.emit()

    def quit(self):
        self.quit_requested.emit()


def main():
    # Singleton: reuse an existing QApplication (tests, embedding) instead
    # of constructing a second one - Qt supports exactly one per process.
    app = QApplication.instance() or QApplication(sys.argv)
    
    



    app.setApplicationName("cat-talker-overlay")
    app.setDesktopFileName("cat-talker-overlay")
    app.setFont(QFont("Outfit", 10))

    from cat_talker.config import (
        load_config, is_api_key_configured, migrate_legacy_api_key)
    c = load_config()
    migrate_legacy_api_key()
    if not is_api_key_configured():
        QMessageBox.critical(None, "Missing API Key", "API key missing! Set GEMINI_API_KEY, or add one in Settings (stored in the OS keyring).")
        sys.exit(1)

    window = get_overlay_window()
    window.setWindowTitle("Audio Visualizer Widget")
    window.setObjectName("cat-talker-overlay")
    
    # Position at bottom center on startup
    window.position_bottom_center()
    window.show()


    # Single visibility mechanism: the UiBridge owns show/hide/toggle
    # (always executed in the Qt/main thread). SIGUSR1 and the socket
    # ui-* commands are just triggers into it - never competing paths.
    ui_bridge = UiBridge(lambda: window)

    def handle_sigusr1(signum, frame):
        ui_bridge.toggle()

    signal.signal(signal.SIGUSR1, handle_sigusr1)

    global_agent = []
    
    def handle_sigusr2(signum, frame):
        if global_agent:
            agent = global_agent[0]
            if agent.loop:
                agent.loop.call_soon_threadsafe(agent.synthetic_input_queue.put_nowait, "ACTIVE_WINDOW")
                
    signal.signal(signal.SIGUSR2, handle_sigusr2)

    # Dictionary Manager setup
    # Because we're in the main thread during initialization, we can create it
    # But it must call UI functions thread-safely
    def on_dictation_state(state):
        QTimer.singleShot(0, lambda: window.state_signal.emit(state))
        
    dictation_manager = DictationManager(on_dictation_state)

    def handle_sigrtmin(signum, frame):
        dictation_manager.start()
        
    def handle_sigrtmin1(signum, frame):
        dictation_manager.stop()
        
    signal.signal(signal.SIGRTMIN, handle_sigrtmin)
    signal.signal(signal.SIGRTMIN + 1, handle_sigrtmin1)

    # Bridge between threads and PyQt signals
    def on_volume(vol: float, bass: float = 1.0, bins: list = None) -> None:
        if bins is None:
            bins = [0.0]*64
        window.audio_signal.emit(vol, bass, bins)

    def on_quit():
        window.quit_signal.emit()

    def on_text(role: str, text: str):
        window.text_signal.emit(role, text)

    def on_state(state: str):
        window.state_signal.emit(state)

    def on_bubble(text: str):
        window.bubble_signal.emit(text)

    def on_glow(state: str):
        window.glow_signal.emit(state)

    # Startup order is structural: control socket FIRST so F2 always has
    # something to talk to, then the agent (born SLEEPING - run_loop
    # cannot connect until a wake request arrives). No Live session,
    # no reconnect, no mic forwarding before the first wake.
    # Local control socket for F2 sleep/wake (Hyprland ->
    # bin/assistant-control -> this process). No new instance is ever
    # started when one answers; the socket file is this instance's ID.
    def _get_agent():
        return global_agent[0] if global_agent else None

    # System tray: same process, same window, same control semantics
    # (F1/F2/F3). Falls back silently where no tray host exists.
    from cat_talker.tray import ChibiTray
    tray = ChibiTray(_get_agent, ui_bridge, window)
    tray.start()

    # Single-instance gate: if another live assistant owns the control
    # socket, this process must NOT continue headless (a second live
    # microphone pipeline fighting over one mic device fragments
    # transcription). Exit before the agent thread starts instead.
    control_socket_ready = threading.Event()
    control_socket_ok = []
    control_thread = threading.Thread(
        target=serve_forever,
        kwargs={"get_agent": _get_agent, "ui": ui_bridge,
                "on_ready": lambda ok: (control_socket_ok.append(ok),
                                        control_socket_ready.set())},
        daemon=True,
    )
    control_thread.start()

    # Single-instance gate: if another live assistant owns the control
    # socket, this process must NOT continue headless (a second live
    # microphone pipeline fighting over one mic device fragments
    # transcription). Exit before the agent thread starts instead.
    control_socket_ready.wait(timeout=10.0)
    if not control_socket_ok or control_socket_ok[0] is not True:
        logger.error(
            "control socket owned by another live instance; "
            "refusing to start a second assistant")
        raise SystemExit(2)

    agent_thread = threading.Thread(
        target=start_agent_in_thread,
        # hide_cb is UiBridge.hide: a queued Qt signal, so the agent
        # thread's auto-hide request marshals onto the main thread and
        # never touches widgets off-thread. Visibility only, not sleep.
        args=(on_volume, on_quit, on_text, on_state, on_bubble, on_glow, global_agent, ui_bridge.hide),
        daemon=True
    )
    agent_thread.start()

    # Graceful shutdown: Ctrl-C / SIGTERM (and any Qt quit path) must stop
    # the agent, unwind asyncio, close audio, join the agent thread, then
    # quit Qt. Without this, SIGINT lands as KeyboardInterrupt inside
    # app.exec() and the process dies via SIGABRT.
    shutdown_requested = False

    def request_agent_stop():
        for candidate in list(global_agent):
            try:
                candidate.request_stop()
            except Exception:
                pass

    def handle_shutdown(signum, frame):
        nonlocal shutdown_requested
        if shutdown_requested:
            return
        shutdown_requested = True
        request_agent_stop()
        QApplication.quit()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    app.aboutToQuit.connect(request_agent_stop)

    # Lets the interpreter service signals while app.exec() runs in C++.
    wakeup_timer = QTimer()
    wakeup_timer.timeout.connect(lambda: None)
    wakeup_timer.start(250)

    exit_code = app.exec()
    agent_thread.join(timeout=15)
    sys.exit(exit_code)

if __name__ == "__main__":
    main()
