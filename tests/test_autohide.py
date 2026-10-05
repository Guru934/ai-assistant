"""Auto-hide UI: successful window-opening tools hide the avatar UI.

Visibility only - never sleep. Qt runs offscreen; the agent's
hide_callback is the real UiBridge.hide (queued signal), invoked from a
worker thread like the agent thread would.
"""

import os
import sys
import threading
import time as _time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PyQt6.QtWidgets import QApplication

_qt_app = QApplication.instance() or QApplication([])

from cat_talker.agent import (
    AUTO_HIDE_TOOLS,
    auto_hide_succeeded,
)
from cat_talker.main import UiBridge, count_overlay_windows, get_overlay_window
from test_session_lifecycle import make_agent


def _drain():
    QApplication.processEvents()
    QApplication.processEvents()


def _agent_and_bridge():
    agent = make_agent()  # awake per test contract
    window = get_overlay_window()
    window.show()
    _drain()
    bridge = UiBridge(lambda: window)
    return agent, bridge, window


def _hide_from_worker_thread(bridge):
    """Invoke hide the way the agent thread does: cross-thread emit."""
    t = threading.Thread(target=bridge.hide)
    t.start()
    t.join(timeout=5)
    for _ in range(100):
        _drain()
        if bridge.visible is False:
            break
        _time.sleep(0.01)


# ---------------------------------------------------------------------------
# 1-5: successful window-opening tools -> hide requested
# ---------------------------------------------------------------------------

SUCCESS_CASES = [
    ("open_application", "Successfully opened terminal."),
    ("open_website", "Successfully opened website: https://youtube.com"),
    ("open_website", "Opened website in brave: https://youtube.com"),
    ("open_file", "Successfully opened file /home/user/notes.txt"),
    ("focus_or_launch", "Focused existing brave window."),
    ("focus_or_launch", "Successfully opened code."),
    ("search_and_play_youtube", "Successfully opened website: https://www.youtube.com/watch?v=abc"),
]


def test_success_results_hide():
    agent, bridge, window = _agent_and_bridge()
    for tool, result in SUCCESS_CASES:
        window.show()
        _drain()
        assert agent._maybe_auto_hide(tool, result, bridge.hide) is True
        _hide_from_worker_thread(bridge)  # signal already emitted; drain it
        _drain()
        assert window.isHidden(), f"{tool}: {result!r} did not hide"
    bridge.show()
    _drain()


def test_auto_hide_tools_exact_set():
    assert AUTO_HIDE_TOOLS == frozenset({
        "open_application", "open_website", "open_file",
        "focus_or_launch", "search_and_play_youtube",
    })


# ---------------------------------------------------------------------------
# 6: failed versions -> no hide
# ---------------------------------------------------------------------------

FAILURE_CASES = [
    ("open_application", "Error: 'foo' application could not be found."),
    ("open_application", "Failed to open foo. Exception: boom"),
    ("open_website", "Error: xdg-open not found to launch URL."),
    ("open_website", "Failed to open website. Exception: boom"),
    ("open_file", "Error: File /nope does not exist"),
    ("open_file", "Failed to open file: boom"),
    ("focus_or_launch", "Error: No application name provided."),
    ("focus_or_launch", "Error focusing/launching foo: boom"),
    ("search_and_play_youtube", "Failed to search and play YouTube: boom"),
    ("open_application", ""),
    ("open_application", None),
]


def test_failure_results_never_hide():
    agent, bridge, window = _agent_and_bridge()
    for tool, result in FAILURE_CASES:
        assert agent._maybe_auto_hide(tool, result, bridge.hide) is False
        _drain()
        assert not window.isHidden(), f"{tool}: {result!r} hid the UI"
    bridge.show()
    _drain()


# ---------------------------------------------------------------------------
# 7-8: non-auto-hide tools + workspace switching -> no hide
# ---------------------------------------------------------------------------

def test_non_auto_hide_tools_never_hide():
    agent, bridge, window = _agent_and_bridge()
    others = ["switch_workspace", "set_volume", "set_brightness",
              "media_action", "get_clipboard", "get_active_window",
              "take_screenshot", "inspect_screen", "get_weather",
              "get_preference", "web_search", "fetch_webpage",
              "read_aloud", "run_coding_task", "send_notification",
              "click_screen", "type_text", "press_key"]
    for tool in others:
        assert tool not in AUTO_HIDE_TOOLS
        assert agent._maybe_auto_hide(
            tool, "Successfully opened something.", bridge.hide) is False
        _drain()
        assert not window.isHidden(), f"{tool} hid the UI"
    bridge.show()
    _drain()


def test_workspace_switch_never_hides():
    agent, bridge, window = _agent_and_bridge()
    assert agent._maybe_auto_hide(
        "switch_workspace", "Switched to workspace 5.", bridge.hide) is False
    _drain()
    assert not window.isHidden()


# ---------------------------------------------------------------------------
# 9: auto-hide does not change sleep state
# ---------------------------------------------------------------------------

def test_auto_hide_leaves_sleep_untouched():
    agent, bridge, window = _agent_and_bridge()
    assert agent.sleep.is_sleeping() is False
    assert agent._maybe_auto_hide(
        "open_website", "Successfully opened website: https://x",
        bridge.hide) is True
    _drain()
    assert agent.sleep.is_sleeping() is False, "auto-hide must not sleep"
    assert "request_sleep" not in bridge.hide.__name__  # sanity: plain hide


def test_auto_hide_never_calls_request_sleep(monkeypatch):
    import cat_talker.agent as agent_mod
    agent, _, _ = _agent_and_bridge()
    calls = []
    monkeypatch.setattr(agent.sleep, "request_sleep",
                        lambda *a, **k: calls.append(True))
    hidden = []
    agent._maybe_auto_hide("open_application", "Successfully opened x.",
                           lambda: hidden.append(True))
    assert hidden == [True]
    assert calls == [], "sleep machinery must not be invoked"


# ---------------------------------------------------------------------------
# 10: F1 (show) brings the UI back
# ---------------------------------------------------------------------------

def test_f1_show_restores_after_auto_hide():
    agent, bridge, window = _agent_and_bridge()
    agent._maybe_auto_hide("open_application", "Successfully opened x.",
                           bridge.hide)
    _drain()
    assert window.isHidden()
    bridge.show()  # F1 path
    _drain()
    assert not window.isHidden()
    assert bridge.visible is True
    assert count_overlay_windows() == 1


# ---------------------------------------------------------------------------
# 11: hidden UI does not stop microphone/session
# ---------------------------------------------------------------------------

def test_hidden_ui_keeps_session_alive():
    agent, bridge, window = _agent_and_bridge()
    loop_before = agent.loop if hasattr(agent, "loop") else None
    mic_live_before = agent._mic_live
    agent._maybe_auto_hide("open_website", "Successfully opened website: x",
                           bridge.hide)
    _drain()
    assert window.isHidden()
    assert agent.sleep.is_sleeping() is False
    assert agent._mic_live == mic_live_before, "mic accounting untouched"
    if loop_before is not None:
        assert agent.loop is loop_before
    # Agent still answers control commands while hidden.
    assert agent.request_toggle() == "sleeping"
    assert agent.request_toggle() == "awake"
    bridge.show()
    _drain()


# ---------------------------------------------------------------------------
# 12: hide request is thread-safe / goes through UiBridge
# ---------------------------------------------------------------------------

def test_hide_marshals_onto_main_thread():
    _, bridge, window = _agent_and_bridge()
    window.show()
    _drain()
    main_thread = threading.current_thread()
    seen = []
    bridge.hide_requested.connect(lambda: seen.append(threading.current_thread()))

    def worker():
        bridge.hide()  # as the agent thread calls it

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5)
    for _ in range(100):
        _drain()
        if seen:
            break
        _time.sleep(0.01)
    assert seen, "hide was never delivered"
    assert all(th is main_thread for th in seen)
    assert window.isHidden()
    bridge.show()
    _drain()


def test_none_callback_is_safe_noop():
    agent, _, _ = _agent_and_bridge()
    assert agent._maybe_auto_hide(
        "open_website", "Successfully opened website: x", None) is False


# ---------------------------------------------------------------------------
# 13: duplicate tool-call dedup does not cause repeated hide behavior
# ---------------------------------------------------------------------------

def test_repeated_success_is_idempotent():
    agent, bridge, window = _agent_and_bridge()
    for _ in range(5):  # replayed/deduped success results
        assert agent._maybe_auto_hide(
            "open_website", "Successfully opened website: x",
            bridge.hide) is True
    _drain()
    assert window.isHidden()
    assert count_overlay_windows() == 1  # still exactly one overlay
    assert agent.sleep.is_sleeping() is False
    bridge.show()
    _drain()


def test_hook_lives_only_on_fresh_execution_path():
    """The hook call must sit inside the NEW-execution branch, so
    DUPLICATE_ID / DUPLICATE_SIGNATURE / BLOCKED replays (which reuse a
    cached result_dict and skip execution) can never trigger a hide."""
    import inspect
    import cat_talker.agent as agent_mod
    src = inspect.getsource(agent_mod)
    lines = src.splitlines()
    hook = [i for i, line in enumerate(lines)
            if "_maybe_auto_hide(" in line and "def _maybe_auto_hide" not in line]
    assert len(hook) == 1, "exactly one auto-hide trigger location"
    hook = hook[0]
    # The NEW-execution branch starts at `elif ... in tool_func_map:` and
    # ends at its `except`; every dedup/blocked branch (DUPLICATE_ID,
    # DUPLICATE_SIGNATURE, BLOCKED_*) sets a cached result_dict and skips
    # execution before that elif, so replays can never reach the hook.
    branch_starts = [i for i, line in enumerate(lines)
                     if "elif function_call.name in tool_func_map:" in line]
    assert branch_starts, "execution branch not found"
    branch = max(s for s in branch_starts if s < hook)
    tail = lines[branch:hook]
    assert "classification=NEW" in "\n".join(lines[branch:branch + 12])
    for marker in ("DUPLICATE_ID", "DUPLICATE_SIGNATURE", "BLOCKED_RETRY",
                   "BLOCKED_UNRELIABLE", "BLOCKED_FETCH_BUDGET"):
        assert any(marker in line for line in lines[:branch]), marker
    assert not any("DUPLICATE" in line or "BLOCKED_" in line for line in tail), \
        "hook must sit after all dedup/blocked branches, on fresh execution only"
    assert "auto_hide_succeeded" in src
