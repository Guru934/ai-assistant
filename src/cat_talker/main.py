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

        # ── Speech Bubble (separate widget overlay) ────────────────
        self.bubble_label = QLabel("", self)
        self.bubble_label.setWordWrap(True)
        self.bubble_label.setStyleSheet(f"""
            QLabel {{
                color: {COLORS["bubble_text"].name()};
                background-color: {COLORS["bubble_bg"].name()};
                border-radius: 12px;
                padding: 8px 12px;
                font-family: 'Outfit', 'Work Sans', sans-serif;
                font-size: 13px;
                font-weight: 400;
            }}
        """)
        self.bubble_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bubble_label.hide()

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
        self.bubble_label.setWindowOpacity(val)
        if val > 0.01:
            self.bubble_label.show()
        else:
            self.bubble_label.hide()

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
        self._position_bubble()
        self.bubble_label.setText(text)
        self.bubble_label.adjustSize()
        self.bubble_anim.stop()
        self.bubble_anim.setStartValue(0.0)
        self.bubble_anim.setEndValue(1.0)
        self.bubble_anim.start()
        QTimer.singleShot(3000, self._fade_bubble)

    def _fade_bubble(self):
        if self._bubble_opacity > 0.01:
            self.bubble_anim.stop()
            self.bubble_anim.setStartValue(self._bubble_opacity)
            self.bubble_anim.setEndValue(0.0)
            self.bubble_anim.start()

    def _on_glow(self, state: str):
        self.glow_state = state

    def _position_bubble(self):
        pos = self.vis_widget.pos()
        bubble_w = self.bubble_label.sizeHint().width()
        bubble_h = self.bubble_label.sizeHint().height()
        x = pos.x() + (self.vis_widget.width() - bubble_w) // 2
        y = pos.y() - bubble_h - 10
        self.bubble_label.move(x, y)

    # ─── Painting ─────────────────────────────────────────────────
    def paintEvent(self, event):
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
        act_capture = menu.addAction("📸 Capture Active Window (pkill -SIGUSR2)")
        act_quit = menu.addAction("❌ Quit Assistant")

        actions = {
            "pin": act_pin,
            "bottom_center": act_bottom_center,
            "bottom_right": act_bottom_right,
            "center": act_center,
            "hide": act_hide,
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
        elif action == actions["capture"]:
            # Re-use the existing logic by sending SIGUSR2 to ourselves
            os.kill(os.getpid(), signal.SIGUSR2)
        elif action == actions["quit"]:
            QApplication.quit()

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

    from cat_talker.config import load_config
    c = load_config()
    if not c.get("api_key") and not os.environ.get("GEMINI_API_KEY"):
        QMessageBox.critical(None, "Missing API Key", "API key missing! Check ~/.config/cat-talker/config.json")
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
