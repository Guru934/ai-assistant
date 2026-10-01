"""Top-level overlay window lifecycle: exactly one per process.

No second RadialVisualizerWindow may ever exist: sleep/wake, F2, the
control socket, hide/show, and flag changes must all reuse the singleton.
Qt runs offscreen (no display needed); topLevelWidgets() is the counter.
"""

import asyncio
import os
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

_qt_app = QApplication.instance() or QApplication([])

import cat_talker.main as main_mod
from cat_talker.main import (
    RadialVisualizerWindow,
    count_overlay_windows,
    get_overlay_window,
)

from test_session_lifecycle import FakeSession, make_agent, wire_connect


def _only_window():
    wins = [w for w in QApplication.topLevelWidgets()
            if isinstance(w, RadialVisualizerWindow)]
    return wins


def test_factory_returns_singleton():
    first = get_overlay_window()
    second = get_overlay_window()
    assert first is second
    assert count_overlay_windows() == 1
    assert _only_window() == [first]


def test_show_hide_reshow_keeps_one_window():
    window = get_overlay_window()
    window.show()
    assert count_overlay_windows() == 1
    window.hide()
    assert count_overlay_windows() == 1
    window.show()
    assert count_overlay_windows() == 1


def test_window_flag_change_keeps_one_window():
    """Pin-style setWindowFlags + show (native recreate) stays singular."""
    window = get_overlay_window()
    window.show()
    flags = window.windowFlags() | Qt.WindowType.WindowStaysOnTopHint
    window.setWindowFlags(flags)
    window.show()
    assert count_overlay_windows() == 1
    flags &= ~Qt.WindowType.WindowStaysOnTopHint
    window.setWindowFlags(flags)
    window.show()
    assert count_overlay_windows() == 1


def test_close_destroys_cleanly_and_factory_recovers():
    from PyQt6 import sip
    window = get_overlay_window()
    window.show()
    window.close()
    sip.delete(window)
    QApplication.processEvents()
    assert count_overlay_windows() == 0
    fresh = get_overlay_window()
    assert count_overlay_windows() == 1
    assert fresh is not window
    fresh.show()


def test_sleep_wake_cycles_keep_one_window():
    """sleep -> wake -> sleep -> wake with a live session and a live
    window wired exactly like main(): window count stays 1 throughout."""
    window = get_overlay_window()
    window.show()
    agent = make_agent()
    first = FakeSession(hang=True)
    second = FakeSession(hang=True)
    wire_connect(agent, [first, second])
    seen = []

    def sample():
        seen.append(count_overlay_windows())

    async def go():
        loop = asyncio.get_running_loop()
        loop.call_later(0.15, sample)
        loop.call_later(0.2, agent.request_sleep)
        loop.call_later(0.35, sample)
        loop.call_later(0.45, agent.request_wake)
        loop.call_later(0.6, sample)
        loop.call_later(0.65, agent.request_sleep)
        loop.call_later(0.8, sample)
        loop.call_later(0.85, agent.request_wake)
        loop.call_later(1.0, sample)
        loop.call_later(1.1, agent.stop_event.set)
        await asyncio.wait_for(
            agent.run_loop(
                state_callback=window.state_signal.emit,
                bubble_callback=window.bubble_signal.emit,
                glow_callback=window.glow_signal.emit,
            ),
            timeout=12.0)

    import asyncio as _aio
    real_sleep = _aio.sleep

    async def fast_sleep(delay, *a, **k):
        await real_sleep(min(delay, 0.02), *a, **k)

    from unittest.mock import patch
    with patch.object(_aio, "sleep", fast_sleep):
        _aio.run(go())
    assert seen, "no samples taken"
    assert all(n == 1 for n in seen), seen
    assert count_overlay_windows() == 1


def test_control_commands_keep_one_window(tmp_path):
    """F2 path (socket toggle/wake/sleep) never touches window creation."""
    from cat_talker.control import send_command, serve_forever
    window = get_overlay_window()
    window.show()
    agent = make_agent()
    agent.sleep.request_sleep()
    path = str(tmp_path / "cat-talker" / "control.sock")
    stop = threading.Event()
    thread = threading.Thread(
        target=serve_forever,
        kwargs={"get_agent": lambda: agent, "path": path,
                "stop_event": stop},
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
            import time as _time
            _time.sleep(0.02)
        assert send_command("toggle", path=path)["state"] == "awake"
        assert count_overlay_windows() == 1
        assert send_command("toggle", path=path)["state"] == "sleeping"
        assert count_overlay_windows() == 1
        assert send_command("wake", path=path)["state"] == "awake"
        assert send_command("sleep", path=path)["state"] == "sleeping"
        assert count_overlay_windows() == 1
    finally:
        stop.set()
        thread.join(timeout=5)
    assert count_overlay_windows() == 1


def test_toplevel_contains_exactly_the_app_window():
    window = get_overlay_window()
    window.show()
    assert _only_window() == [window]
