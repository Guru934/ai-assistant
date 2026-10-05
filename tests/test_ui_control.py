"""F1/F2/F4 control separation: visibility vs listening vs lifetime.

F1 (ui-*) touches ONLY visibility. F2 (toggle/wake/sleep) touches ONLY
sleep state. F4 (stop) ends the process via existing shutdown machinery.
No real global hotkeys: socket + bridge calls stand in for Hyprland.

Qt runs offscreen; UiBridge calls from worker threads prove main-thread
marshalling.
"""

import os
import sys
import threading
import time as _time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PyQt6.QtWidgets import QApplication

_qt_app = QApplication.instance() or QApplication([])

import cat_talker.main as main_mod
from cat_talker.main import (
    RadialVisualizerWindow,
    UiBridge,
    count_overlay_windows,
    get_overlay_window,
)

from test_session_lifecycle import make_agent


def _drain():
    QApplication.processEvents()
    QApplication.processEvents()


def _bridge(window=None):
    window = window if window is not None else get_overlay_window()
    bridge = UiBridge(lambda: window)
    _drain()
    return bridge, window


# ---------------------------------------------------------------------------
# 1-3: show/hide/toggle semantics on the existing window
# ---------------------------------------------------------------------------

def test_ui_show_from_hidden():
    bridge, window = _bridge()
    window.hide()
    _drain()
    assert window.isHidden()
    bridge.show()
    _drain()
    assert not window.isHidden()
    assert bridge.visible is True
    assert count_overlay_windows() == 1


def test_ui_hide_from_visible():
    bridge, window = _bridge()
    window.show()
    _drain()
    bridge.hide()
    _drain()
    assert window.isHidden()
    assert bridge.visible is False
    assert count_overlay_windows() == 1
    bridge.show()
    _drain()


def test_ui_toggle_twice_returns_to_original():
    bridge, window = _bridge()
    window.show()
    _drain()
    bridge.toggle()
    _drain()
    assert window.isHidden()
    bridge.toggle()
    _drain()
    assert not window.isHidden()
    assert count_overlay_windows() == 1


def test_repeated_ui_toggle_never_duplicates():
    bridge, window = _bridge()
    window.show()
    _drain()
    for _ in range(20):
        bridge.toggle()
        _drain()
    assert not window.isHidden(), "even toggles return to visible"
    assert count_overlay_windows() == 1


# ---------------------------------------------------------------------------
# 9: socket/control-thread calls land on the Qt/main thread
# ---------------------------------------------------------------------------

def test_ui_calls_marshal_onto_main_thread():
    bridge, window = _bridge()
    window.show()
    _drain()
    main_thread = threading.current_thread()
    seen = []

    def spy():
        seen.append(threading.current_thread())

    bridge.hide_requested.connect(spy)

    def worker():
        bridge.hide()  # called OFF the main thread (like the socket)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    for _ in range(100):
        _drain()
        if seen:
            break
        _time.sleep(0.01)
    assert seen, "hide was never delivered"
    assert all(t is main_thread for t in seen), \
        "QWidget touched off the main thread"
    assert window.isHidden()
    bridge.show()
    _drain()


# ---------------------------------------------------------------------------
# 4-7: visibility independent of sleep/awake; F2 unchanged
# ---------------------------------------------------------------------------

def test_visibility_does_not_change_sleep_state():
    bridge, window = _bridge()
    agent = make_agent()  # awake per test contract
    window.show()
    _drain()
    bridge.hide()  # F1 while awake
    _drain()
    assert agent.sleep.is_sleeping() is False
    agent.sleep.request_sleep()
    bridge.show()  # F1 while sleeping
    _drain()
    assert agent.sleep.is_sleeping() is True
    assert count_overlay_windows() == 1


def test_f2_still_changes_sleep_state_only():
    agent = make_agent()
    assert agent.request_toggle() == "sleeping"
    assert agent.request_toggle() == "awake"


def test_socket_ui_and_sleep_are_independent(tmp_path):
    from cat_talker.control import send_command, serve_forever
    bridge, window = _bridge()
    window.show()
    _drain()
    agent = make_agent()  # awake
    path = str(tmp_path / "cat-talker" / "control.sock")
    stop = threading.Event()
    thread = threading.Thread(
        target=serve_forever,
        kwargs={"get_agent": lambda: agent, "path": path,
                "stop_event": stop, "ui": bridge},
        daemon=True)
    thread.start()
    try:
        for _ in range(150):
            try:
                if send_command("status", path=path,
                                timeout=0.2).get("ok"):
                    break
            except (OSError, ValueError):
                pass
            _time.sleep(0.02)
        assert send_command("ui-hide", path=path)["ok"] is True
        _drain()
        assert window.isHidden()
        assert agent.sleep.is_sleeping() is False, "F1 must not sleep"
        assert send_command("ui-show", path=path)["ok"] is True
        _drain()
        assert not window.isHidden()
        assert send_command("toggle", path=path)["state"] == "sleeping"
        assert send_command("ui-show", path=path)["ok"] is True
        _drain()
        assert not window.isHidden()
        assert agent.sleep.is_sleeping() is True, "F1 must not wake"
        assert count_overlay_windows() == 1
    finally:
        stop.set()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 8: stop path (signal emission; real quit is a no-op without an event loop)
# ---------------------------------------------------------------------------

def test_stop_emits_quit_request():
    bridge, _ = _bridge()
    seen = []
    bridge.quit_requested.connect(lambda: seen.append(True))
    bridge.quit()  # as the socket thread would (cross-thread emit)
    _drain()
    assert seen == [True]
    bridge._do_quit()  # direct slot: QApplication.quit(), safe headless


def test_socket_stop_reaches_quit(tmp_path):
    from cat_talker.control import send_command, serve_forever
    bridge, _ = _bridge()
    agent = make_agent()
    path = str(tmp_path / "cat-talker" / "control.sock")
    stop = threading.Event()
    thread = threading.Thread(
        target=serve_forever,
        kwargs={"get_agent": lambda: agent, "path": path,
                "stop_event": stop, "ui": bridge},
        daemon=True)
    thread.start()
    try:
        for _ in range(150):
            try:
                if send_command("status", path=path,
                                timeout=0.2).get("ok"):
                    break
            except (OSError, ValueError):
                pass
            _time.sleep(0.02)
        reply = send_command("stop", path=path)
        assert reply["ok"] is True
        _drain()
    finally:
        stop.set()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 12-15: right-click menu honesty (builder wiring, offscreen triggers)
# ---------------------------------------------------------------------------

def _menu(window):
    menu, actions = window._build_context_menu()
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    return menu, actions, texts


def test_menu_has_no_unimplemented_mute():
    window = get_overlay_window()
    _, _, texts = _menu(window)
    assert not any("Mute" in t for t in texts), texts


def test_menu_keeps_working_items():
    window = get_overlay_window()
    _, actions, _ = _menu(window)
    assert set(actions) == {"pin", "bottom_center", "bottom_right",
                            "center", "hide", "settings", "capture", "quit"}


def test_menu_hide_works():
    window = get_overlay_window()
    window.show()
    _drain()
    menu, actions = window._build_context_menu()[:2]
    window._handle_menu_action(actions["hide"], actions)
    assert window.isHidden()
    window.show()
    _drain()


def test_menu_quit_path_calls_quit(monkeypatch):
    window = get_overlay_window()
    _, actions = window._build_context_menu()[:2]
    calls = []
    monkeypatch.setattr(main_mod.QApplication, "quit",
                        staticmethod(lambda: calls.append(True)))
    window._handle_menu_action(actions["quit"], actions)
    assert calls == [True]


def test_menu_snap_actions_move_same_window():
    from PyQt6.QtWidgets import QApplication as _QApp
    window = get_overlay_window()
    window.show()
    _drain()
    screen = _QApp.primaryScreen()
    if screen is None:  # pragma: no cover - no screen at all
        return
    geo = screen.geometry()
    _, actions = window._build_context_menu()[:2]
    window._handle_menu_action(actions["bottom_center"], actions)
    assert (window.x(), window.y()) == (
        (geo.width() - window.width()) // 2,
        geo.height() - window.height() - 40)
    window._handle_menu_action(actions["bottom_right"], actions)
    assert (window.x(), window.y()) == (
        geo.width() - window.width() - 40,
        geo.height() - window.height() - 40)
    window._handle_menu_action(actions["center"], actions)
    assert (window.x(), window.y()) == (
        (geo.width() - window.width()) // 2,
        (geo.height() - window.height()) // 2)
    assert count_overlay_windows() == 1


def test_menu_pin_toggles_flag_on_same_window():
    window = get_overlay_window()
    window.show()
    _drain()
    _, actions = window._build_context_menu()[:2]
    before = window.always_on_top
    window._handle_menu_action(actions["pin"], actions)
    assert window.always_on_top is not before
    assert count_overlay_windows() == 1


# ---------------------------------------------------------------------------
# 10, 16: singleton + canonical socket path
# ---------------------------------------------------------------------------

def test_no_second_window_anywhere():
    get_overlay_window()
    assert count_overlay_windows() == 1


def test_socket_commands_use_canonical_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CAT_TALKER_RUNTIME_DIR", str(tmp_path))
    from cat_talker.control import lock_path, socket_path
    assert socket_path() == str(tmp_path / "cat-talker" / "control.sock")
    assert lock_path() == str(tmp_path / "cat-talker" / "launch.lock")
