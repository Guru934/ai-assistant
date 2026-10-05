"""Focused tests for the system-tray integration.

The tray is a view onto existing mechanisms (UiBridge, agent wake/sleep,
SettingsDialog, read-only ydotool health, graceful quit). Qt runs
offscreen; a missing tray host is the normal case here and is tested
as the fallback. No real tray, daemon, or assistant process needed.
"""

import os
import sys
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

_qt_app = QApplication.instance() or QApplication([])

import cat_talker.config as config_mod
from cat_talker.main import count_overlay_windows, get_overlay_window
from cat_talker.tray import ChibiTray, build_tray_icon, describe_state, wake_word_label
from test_session_lifecycle import make_agent


class FakeUi:
    def __init__(self):
        self.calls = []

    def show(self):
        self.calls.append("show")

    def hide(self):
        self.calls.append("hide")

    def toggle(self):
        self.calls.append("toggle")


def _harness(**over):
    """Real window + agent, fake visibility bridge."""
    agent = make_agent()  # awake per contract
    if over.get("sleeping"):
        assert agent.request_sleep() == "sleeping"
    window = get_overlay_window()
    ui = FakeUi()
    tray = ChibiTray(lambda: agent, ui, window)
    return agent, window, ui, tray


def _with_host(monkeypatch):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable",
                        classmethod(lambda cls: True))


# ---------------------------------------------------------------------------
# Construction + unavailable fallback
# ---------------------------------------------------------------------------

def test_tray_unavailable_fallback_is_silent(monkeypatch):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable",
                        classmethod(lambda cls: False))
    agent, window, ui, tray = _harness()
    assert tray.start() is False
    assert tray.tray is None
    assert ui.calls == []
    assert count_overlay_windows() == 1


def test_tray_construction_with_host(monkeypatch):
    _with_host(monkeypatch)
    agent, window, ui, tray = _harness()
    assert tray.start() is True
    assert tray.tray is not None
    labels = [tray._actions[k].text() for k in
              ("status", "show", "wake", "sleep", "settings", "ydotool", "quit")]
    assert labels[1] == "Show Chibi"
    assert labels[2] == "Wake"
    assert labels[3] == "Sleep"
    assert labels[6] == "Quit"
    assert count_overlay_windows() == 1
    tray.stop()
    assert tray.tray is None


def test_tray_icon_builds():
    icon = build_tray_icon()
    assert not icon.isNull()


# ---------------------------------------------------------------------------
# Status reflects real state, not guesses
# ---------------------------------------------------------------------------

def test_status_sleeping_and_awake(monkeypatch):
    _with_host(monkeypatch)
    agent, window, ui, tray = _harness(sleeping=True)
    window.current_state = "idle"
    tray.start()
    tray.refresh()
    assert tray._actions["status"].text() == "Status: Sleeping"
    assert tray._actions["wake"].isEnabled() is True
    assert tray._actions["sleep"].isEnabled() is False
    agent.request_wake()
    window.current_state = "listening"
    tray.refresh()
    assert tray._actions["status"].text() == "Status: Listening"
    assert tray._actions["wake"].isEnabled() is False
    assert tray._actions["sleep"].isEnabled() is True
    tray.stop()


def test_unknown_state_leaves_actions_usable(monkeypatch, caplog):
    """Genuinely unknown state says Unavailable but never strands the
    user: both Wake and Sleep stay enabled (fail-safe)."""
    import logging
    _with_host(monkeypatch)
    _, window, ui, _ = _harness()
    tray = ChibiTray(lambda: None, ui, window)
    tray.start()
    with caplog.at_level(logging.DEBUG, logger="cat_talker.tray"):
        tray.refresh()
    assert tray._actions["status"].text() == "Status: Unavailable"
    assert tray._actions["wake"].isEnabled() is True
    assert tray._actions["sleep"].isEnabled() is True
    assert any("state=Unavailable" in r.message for r in caplog.records)
    tray.stop()


def test_probe_matrix():
    from cat_talker.tray import probe_state

    class _Sleep:
        def __init__(self, sleeping):
            self._sleeping = sleeping

        def is_sleeping(self):
            return self._sleeping

    class _Agent:
        def __init__(self, sleeping):
            self.sleep = _Sleep(sleeping)

    class _Window:
        def __init__(self, state):
            self.current_state = state

    assert probe_state(_Agent(True), _Window("listening")) == \
        ("Sleeping", True)
    assert probe_state(_Agent(False), _Window("listening")) == \
        ("Listening", False)
    assert probe_state(_Agent(False), _Window("thinking")) == \
        ("Thinking", False)
    assert probe_state(_Agent(False), _Window("talking")) == \
        ("Speaking", False)
    assert probe_state(_Agent(False), _Window("idle")) == \
        ("Idle", False)
    assert probe_state(_Agent(False), _Window("dictating")) == \
        ("Dictating", False)
    assert probe_state(None, _Window("listening")) == \
        ("Unavailable", None)
    assert probe_state(_Agent(False), None) == ("Awake", False)


def test_status_thinking_and_no_agent(monkeypatch):
    _with_host(monkeypatch)
    agent, window, ui, tray = _harness()
    window.current_state = "thinking"
    tray.start()
    tray.refresh()
    assert tray._actions["status"].text() == "Status: Thinking"
    assert describe_state(None, window) == "Unavailable"
    tray.stop()


def test_wake_word_indicator(monkeypatch):
    assert wake_word_label() in ("Wake word: On", "Wake word: Off")
    monkeypatch.setattr(config_mod, "load_config",
                        lambda: {"wake_word_enabled": True})
    assert wake_word_label() == "Wake word: On"
    monkeypatch.setattr(config_mod, "load_config",
                        lambda: {"wake_word_enabled": False})
    assert wake_word_label() == "Wake word: Off"


# ---------------------------------------------------------------------------
# Actions reuse F1/F2/F3 semantics
# ---------------------------------------------------------------------------

def test_show_uses_visibility_bridge(monkeypatch):
    _with_host(monkeypatch)
    agent, window, ui, tray = _harness()
    tray.start()
    tray.show_window()
    assert ui.calls == ["show"]  # F1-show path, never a new window
    assert count_overlay_windows() == 1
    tray.stop()


def test_wake_sleep_use_agent_transitions(monkeypatch):
    """Same request_wake/request_sleep the F2 socket path calls."""
    _with_host(monkeypatch)
    agent, window, ui, tray = _harness(sleeping=True)
    tray.start()
    tray.wake()
    assert agent.sleep.is_sleeping() is False
    tray.sleep()
    assert agent.sleep.is_sleeping() is True
    tray.stop()


def test_wake_sleep_without_agent_is_safe(monkeypatch, caplog):
    import logging
    _with_host(monkeypatch)
    _, window, ui, _ = _harness()
    tray = ChibiTray(lambda: None, ui, window)
    tray.start()
    with caplog.at_level(logging.WARNING, logger="cat_talker.tray"):
        tray.wake()
        tray.sleep()
    assert any("unavailable" in r.message for r in caplog.records)
    tray.stop()


def test_settings_opens_existing_dialog(monkeypatch):
    _with_host(monkeypatch)
    agent, window, ui, tray = _harness()
    opened = []
    monkeypatch.setattr(window, "open_settings",
                        lambda: opened.append(True))
    tray.start()
    tray.open_settings()
    assert opened == [True]
    tray.stop()


def test_ydotool_action_is_read_only(monkeypatch):
    """Status text lands in a visible label; the daemon is never touched:
    the health function is stubbed and no subprocess call can occur."""
    _with_host(monkeypatch)
    agent, window, ui, tray = _harness()
    import cat_talker.ydotool_health as health_mod
    monkeypatch.setattr(health_mod, "format_ydotool_status",
                        lambda: ("ydotool: healthy\nline2", 0))
    ran = []
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: ran.append(a) or None)
    shown = []
    exec_calls = []
    from cat_talker import tray as tray_mod
    real_build = tray_mod.build_ydotool_status_dialog

    def _capture(win, text):
        dialog = real_build(win, text)
        shown.append(dialog)
        # Never enter a modal loop headless; record the attempt instead.
        dialog.exec = lambda: exec_calls.append(True) or 1
        return dialog

    monkeypatch.setattr(tray_mod, "build_ydotool_status_dialog", _capture)
    tray.start()
    tray.show_ydotool_status()
    assert ran == [], "ydotool action must not run anything"
    assert exec_calls == [True]
    assert len(shown) == 1
    from PyQt6.QtWidgets import QLabel, QPushButton
    label = shown[0].findChild(QLabel, "ydotool_status_text")
    assert label is not None
    assert label.text() == "ydotool: healthy\nline2"
    assert label.wordWrap() is True
    ok_button = shown[0].findChild(QPushButton, "ydotool_status_ok")
    assert ok_button is not None and ok_button.isEnabled()
    tray.stop()


def test_ydotool_dialog_readability():
    """Regression: the complete status text is assigned to a visible,
    wrapping label next to a readable OK button."""
    from PyQt6.QtWidgets import QLabel, QPushButton
    from cat_talker import tray as tray_mod
    from cat_talker.main import get_overlay_window
    window = get_overlay_window()
    long_text = "ydotool: healthy\n" + ("detail line\n" * 30)
    dialog = tray_mod.build_ydotool_status_dialog(window, long_text)
    assert dialog.windowTitle() == "ydotool status"
    label = dialog.findChild(QLabel, "ydotool_status_text")
    assert label is not None
    assert label.text() == long_text  # complete text, not truncated
    assert label.wordWrap() is True
    assert "f0f0f5" in label.styleSheet() or \
        "f0f0f5" in dialog.styleSheet()
    ok_button = dialog.findChild(QPushButton, "ydotool_status_ok")
    assert ok_button is not None
    assert ok_button.isEnabled() is True
    assert ok_button.text() == "OK"
    assert dialog.minimumWidth() >= 400


def test_quit_uses_graceful_shutdown(monkeypatch):
    _with_host(monkeypatch)
    agent, window, ui, tray = _harness()
    called = []
    monkeypatch.setattr("cat_talker.tray.QApplication.quit",
                        lambda: called.append(True))
    tray.start()
    tray.quit()
    assert called == [True]
    tray.stop()


def test_tray_shutdown_leaves_no_workers(monkeypatch):
    _with_host(monkeypatch)
    agent, window, ui, tray = _harness()
    before = threading.enumerate()
    tray.start()
    tray.refresh()
    tray.stop()
    assert threading.enumerate() == before or all(
        t.daemon or not t.is_alive() for t in threading.enumerate()
        if t not in before)


def test_tray_runs_on_main_thread(monkeypatch):
    _with_host(monkeypatch)
    agent, window, ui, tray = _harness()
    assert threading.current_thread() is threading.main_thread()
    tray.start()
    tray.refresh()
    tray.stop()
