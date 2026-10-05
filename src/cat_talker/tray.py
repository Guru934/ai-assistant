"""System-tray icon and menu for the running Chibi process.

Same process, same QApplication, same window: the tray is a second
VIEW onto the existing control/state architecture, never a second
controller. Every action routes through the mechanisms F1/F2/F3 use:

- Show Chibi  -> UiBridge.show()      (F1 visibility semantics)
- Wake/Sleep  -> agent.request_wake()/request_sleep() (F2 semantics)
- Settings    -> the existing SettingsDialog on the overlay window
- ydotool     -> read-only format_ydotool_status() in a message box
                (never performs input, never manages the daemon)
- Quit        -> QApplication.quit()  (F3 graceful path via aboutToQuit)

Status and wake-word rows are disabled display items refreshed from
live state every time the menu opens (menu.aboutToShow): agent sleep
flag + overlay state for status, config file for the wake-word flag.
No guessed variables, no second sleep machine, no background threads.
When Qt reports no tray host (headless/offscreen/bare compositors),
start() returns False and the assistant runs exactly as before.
"""

from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from cat_talker.logging_config import get_logger

logger = get_logger("cat_talker.tray")


def build_tray_icon():
    """Small Chibi-style glyph drawn in code (no asset files)."""
    size = 32
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(255, 170, 60))
        painter.setPen(QColor(60, 30, 10))
        painter.drawEllipse(3, 3, size - 6, size - 6)
        painter.setPen(QColor(60, 30, 10))
        font = painter.font()
        font.setBold(True)
        font.setPointSize(16)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), 0x84, "C")
    finally:
        painter.end()
    return QIcon(pixmap)


def probe_state(agent, window):
    """Single state probe for status row, tooltip, and enablement.

    Returns (state_text, sleeping) where sleeping is True/False, or None
    when the state genuinely cannot be determined (no agent, no sleep
    controller, or an unexpected error). Sleeping stays distinct from
    every awake sub-state. No second state machine: this only reads the
    existing sleep controller and UI state.
    """
    try:
        sleep = getattr(agent, "sleep", None) if agent is not None else None
        if sleep is None:
            return "Unavailable", None
        sleeping = bool(sleep.is_sleeping())
    except Exception:
        return "Unavailable", None
    if sleeping:
        return "Sleeping", True
    try:
        ui_state = (getattr(window, "current_state", "") or "").lower()
    except Exception:
        ui_state = ""
    return ({
        "listening": "Listening",
        "thinking": "Thinking",
        "talking": "Speaking",
        "dictating": "Dictating",
        "idle": "Idle",
    }.get(ui_state, "Awake"), False)


def describe_state(agent, window) -> str:
    """Concise real state for the status row and tooltip."""
    state, _ = probe_state(agent, window)
    return state


def wake_word_label() -> str:
    """Wake-word indicator (config flag only; never audio or secrets)."""
    try:
        from cat_talker.config import get_wake_word_enabled
        return "Wake word: On" if get_wake_word_enabled() else "Wake word: Off"
    except Exception:
        return "Wake word: Off"


class ChibiTray:
    """Tray controller owned by the Qt main thread (no workers)."""

    def __init__(self, get_agent, ui_bridge, window):
        self._get_agent = get_agent
        self._ui = ui_bridge
        self._window = window
        self.tray = None
        self._actions = {}

    # -- construction -------------------------------------------------
    def start(self) -> bool:
        """Show the tray icon. False when no tray host is available."""
        try:
            available = QSystemTrayIcon.isSystemTrayAvailable()
        except Exception as e:
            logger.warning("tray unavailable: %s", e)
            return False
        if not available:
            logger.warning("tray unavailable on this desktop - "
                           "F1/F2/F3 keep working without it")
            return False
        tray = QSystemTrayIcon(build_tray_icon())
        tray.setToolTip("Chibi")
        menu = tray.contextMenu() or tray_menu_holder(tray)
        self._build_menu(menu)
        menu.aboutToShow.connect(self.refresh)
        tray.activated.connect(self._on_activated)
        tray.show()
        self.tray = tray
        self.refresh()
        logger.info("tray started")
        return True

    def _build_menu(self, menu):
        def _item(text, slot, enabled=True):
            action = QAction(text, menu)
            action.triggered.connect(slot)
            action.setEnabled(enabled)
            menu.addAction(action)
            return action

        self._actions["status"] = _item("Status: …", lambda: None,
                                        enabled=False)
        self._actions["wakeword"] = _item("Wake word: …", lambda: None,
                                          enabled=False)
        menu.addSeparator()
        self._actions["show"] = _item("Show Chibi", self.show_window)
        self._actions["wake"] = _item("Wake", self.wake)
        self._actions["sleep"] = _item("Sleep", self.sleep)
        self._actions["settings"] = _item("Settings…", self.open_settings)
        self._actions["ydotool"] = _item("ydotool status…",
                                         self.show_ydotool_status)
        menu.addSeparator()
        self._actions["quit"] = _item("Quit", self.quit)
        return menu

    # -- live state ---------------------------------------------------
    def _agent(self):
        try:
            return self._get_agent()
        except Exception:
            return None

    def refresh(self):
        """Recompute every dynamic row from live state (menu opening)."""
        if self.tray is None:
            return
        state, sleeping = probe_state(self._agent(), self._window)
        logger.debug("tray refresh: state=%s sleeping=%s", state, sleeping)
        self._actions["status"].setText(f"Status: {state}")
        self.tray.setToolTip(f"Chibi — {state}")
        self._actions["wakeword"].setText(wake_word_label())
        # Fail-safe enablement: only disable an action the known state
        # positively contradicts. Unknown state leaves both usable, so a
        # transient probe failure can never strand the user without Wake.
        self._actions["wake"].setEnabled(sleeping is not False)
        self._actions["sleep"].setEnabled(sleeping is not True)

    # -- actions (F1/F2/F3 semantics) ----------------------------------
    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_window()

    def show_window(self):
        """F1-show semantics through the existing visibility bridge."""
        try:
            self._ui.show()
        except Exception as e:
            logger.warning("tray show failed: %s", e)

    def wake(self):
        """F2-wake semantics through the agent's own transition."""
        agent = self._agent()
        if agent is None:
            logger.warning("tray wake: assistant unavailable")
            return
        try:
            agent.request_wake()
        except Exception as e:
            logger.warning("tray wake failed: %s", e)

    def sleep(self):
        """F2-sleep semantics through the agent's own transition."""
        agent = self._agent()
        if agent is None:
            logger.warning("tray sleep: assistant unavailable")
            return
        try:
            agent.request_sleep()
        except Exception as e:
            logger.warning("tray sleep failed: %s", e)

    def open_settings(self):
        """The existing SettingsDialog on the overlay window."""
        try:
            self._window.open_settings()
        except Exception as e:
            logger.warning("tray settings failed: %s", e)

    def show_ydotool_status(self):
        """Read-only backend health in a high-contrast dialog. Never
        input, never daemon management, never sudo."""
        try:
            from cat_talker.ydotool_health import format_ydotool_status
            text, _ = format_ydotool_status()
        except Exception as e:
            text = f"ydotool status unavailable: {e}"
        try:
            dialog = build_ydotool_status_dialog(self._window, text)
            dialog.exec()
        except Exception as e:
            logger.warning("tray ydotool dialog failed: %s", e)

    def quit(self):
        """F3 semantics: the existing graceful shutdown path."""
        try:
            QApplication.quit()
        except Exception as e:
            logger.warning("tray quit failed: %s", e)

    def stop(self):
        """Hide and release the icon (shutdown path; workers untouched)."""
        tray, self.tray = self.tray, None
        if tray is not None:
            try:
                tray.hide()
            except Exception:
                pass


def tray_menu_holder(tray):
    """QSystemTrayIcon without an existing menu needs an explicit one."""
    from PyQt6.QtWidgets import QMenu
    menu = QMenu()
    tray.setContextMenu(menu)
    return menu


def build_ydotool_status_dialog(window, text):
    """High-contrast read-only status dialog.

    A plain QMessageBox inherits the desktop theme, which on this setup
    renders its text effectively invisible. This dialog states every
    color explicitly (light text on dark background, visible title and
    button), wraps long diagnostics, and stays a reasonable size. It
    only DISPLAYS the read-only health output it is given.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout
    dialog = QDialog(window)
    dialog.setWindowTitle("ydotool status")
    # Fixed width keeps long diagnostics wrapping in a readable column;
    # height stays content-driven (grows with the text, never clipped).
    dialog.setFixedWidth(480)
    dialog.setStyleSheet(
        "QDialog { background-color: #1e1e28; }"
        "QLabel { color: #f0f0f5; font-size: 14px; }"
        "QPushButton { color: #f0f0f5; background-color: #3a3a4c; "
        "border: 1px solid #5a5a6e; border-radius: 4px; padding: 6px 18px; }"
        "QPushButton:hover { background-color: #4a4a5e; }")
    layout = QVBoxLayout(dialog)
    label = QLabel(text if isinstance(text, str) and text else "(no status)")
    label.setObjectName("ydotool_status_text")
    label.setWordWrap(True)
    label.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse)
    layout.addWidget(label)
    ok_button = QPushButton("OK")
    ok_button.setObjectName("ydotool_status_ok")
    ok_button.setDefault(True)
    ok_button.clicked.connect(dialog.accept)
    layout.addWidget(ok_button)
    dialog.setLayout(layout)
    return dialog
