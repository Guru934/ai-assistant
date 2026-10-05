"""Focused tests for grounded, diagnosable computer-use actions.

Covers stale-frame rejection, the ydotool missing/daemon/dispatch
distinction (daemon errors arrive on ydotool's STDOUT with exit 2 -
verified live), the deterministic post-click verification contract, and
preservation of voice-approval. No display, no daemon, no mic: subprocess
and shutil are stubbed.
"""

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cat_talker.tools as tools_mod
from cat_talker.agent import evaluate_click_verification
from test_session_lifecycle import (
    FakeSession,
    make_agent,
    run_with_stop,
    wire_connect,
    _tool_msg,
)

GEOM = {"img_w": 1024, "img_h": 576, "src_w": 1920, "src_h": 1080,
        "x": 0, "y": 0, "scale": 1.0}

# ydotool's real daemon-missing report (exit 2, on STDOUT, verified live).
DAEMON_MSG = ("failed to connect socket `/run/user/1000/.ydotool_socket': "
              "No such file or directory\n"
              "Please check if ydotoold is running.")


def _proc(stdout="", stderr="", returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


def _which(*names):
    def fake_which(name):
        return "/usr/bin/" + name if name in names else None
    return fake_which


@pytest.fixture
def grounding():
    """Isolated approval gate + fresh coordinate store."""
    tools_mod.PENDING_RISKY_ACTION = None
    tools_mod.set_coordinate_geometry(None)
    yield tools_mod
    tools_mod.PENDING_RISKY_ACTION = None
    tools_mod.set_coordinate_geometry(None)


def _hypr_flow(recorded, cursorpos="960, 540", ydotool_mode="ok"):
    """Stub for the Hyprland click path.

    ydotool_mode: "ok" | "daemon" (exit 2 + daemon msg on stdout) |
    "fail" (exit 1, no daemon markers).
    """
    def fake_run(cmd, **kwargs):
        recorded.append(cmd)
        if cmd[:2] == ["hyprctl", "dispatch"]:
            return _proc("")
        if cmd == ["hyprctl", "cursorpos"]:
            return _proc(cursorpos)
        if cmd[0] == "ydotool":
            if ydotool_mode == "daemon":
                raise subprocess.CalledProcessError(2, cmd, output=DAEMON_MSG)
            if ydotool_mode == "fail":
                raise subprocess.CalledProcessError(1, cmd, output="boom")
            return _proc("")
        raise AssertionError("unexpected command: %r" % (cmd,))
    return fake_run


def _click_exec(x, y, desc="target", seq=None):
    """Run a click through approval to execution."""
    if seq is None:
        seq = tools_mod.get_frame_seq()
    tools_mod.click_screen(x, y, desc, frame_seq=seq)
    return tools_mod.click_screen(x, y, desc, frame_seq=seq)


# ---------------------------------------------------------------------------
# 1. Stale screenshot/frame rejection
# ---------------------------------------------------------------------------

def test_click_without_frame_seq_refused(grounding):
    tools_mod.set_coordinate_geometry(dict(GEOM))
    out = tools_mod.click_screen(512, 288, "target")
    assert "Stale frame" in out and "Click NOT sent" in out, out
    assert tools_mod.PENDING_RISKY_ACTION is None, \
        "refused click must not consume an approval turn"


def test_click_with_superseded_frame_refused(grounding):
    seen = []
    tools_mod.set_coordinate_geometry(dict(GEOM))
    old_seq = tools_mod.get_frame_seq()
    tools_mod.set_coordinate_geometry(dict(GEOM))  # newer frame sent
    with patch.object(tools_mod.shutil, "which",
                      side_effect=_which("hyprctl", "ydotool")), \
         patch("subprocess.run", side_effect=_hypr_flow(seen)):
        out = _click_exec(512, 288, seq=old_seq)
    assert "Stale frame" in out, out
    assert f"frame #{old_seq}" in out and "Click NOT sent" in out, out
    assert not seen, "no subprocess may run for a stale click"


def test_confirm_revalidates_frame_after_new_inspect(grounding):
    """Approved with frame #N, then a fresh inspect supersedes it: the
    confirmation honestly fails instead of clicking stale coordinates."""
    seen = []
    tools_mod.set_coordinate_geometry(dict(GEOM))
    seq = tools_mod.get_frame_seq()
    with patch.object(tools_mod.shutil, "which",
                      side_effect=_which("hyprctl", "ydotool")), \
         patch("subprocess.run", side_effect=_hypr_flow(seen)):
        paused = tools_mod.click_screen(512, 288, "target", frame_seq=seq)
        assert "PAUSED FOR SAFETY" in paused, paused
        tools_mod.set_coordinate_geometry(dict(GEOM))  # fresh inspect
        out = tools_mod.confirm_action()
    assert "Stale frame" in out and "Click NOT sent" in out, out
    assert not any(c[0] == "ydotool" for c in seen)


# ---------------------------------------------------------------------------
# 2-3. Successful mapping + mapping failure (no silent passthrough)
# ---------------------------------------------------------------------------

def test_successful_mapping_with_current_frame(grounding):
    seen = []
    tools_mod.set_coordinate_geometry(dict(GEOM))
    with patch.object(tools_mod.shutil, "which",
                      side_effect=_which("hyprctl", "ydotool")), \
         patch("subprocess.run", side_effect=_hypr_flow(seen)):
        out = _click_exec(512, 288)
    assert "dispatched at global (960, 540)" in out, out
    assert "cursor verified at (960, 540)" in out, out


def test_no_geometry_is_mapping_failure_not_passthrough(grounding):
    """No recorded frame: honest mapping failure, never silent coords."""
    seen = []
    assert tools_mod.get_coordinate_geometry() is None
    seq = tools_mod.get_frame_seq()  # current seq passes the frame gate
    with patch.object(tools_mod.shutil, "which",
                      side_effect=_which("hyprctl", "ydotool")), \
         patch("subprocess.run", side_effect=_hypr_flow(seen)):
        tools_mod.PENDING_RISKY_ACTION = None
        tools_mod.click_screen(512, 288, "t", frame_seq=seq)
        out = tools_mod.click_screen(512, 288, "t", frame_seq=seq)
    assert "Coordinate mapping failed" in out and "Click NOT sent" in out, out
    assert not any(c[:2] == ["ydotool", "click"] for c in seen)


# ---------------------------------------------------------------------------
# 4-5. ydotool missing vs ydotoold unavailable
# ---------------------------------------------------------------------------

def test_ydotool_missing_is_explicit(grounding):
    seen = []
    tools_mod.set_coordinate_geometry(dict(GEOM))
    with patch.object(tools_mod.shutil, "which",
                      side_effect=_which("hyprctl")), \
         patch("subprocess.run", side_effect=_hypr_flow(seen)):
        out = _click_exec(512, 288)
    assert "ydotool executable not found" in out, out
    assert "Click NOT sent" in out or "NOT performed" in out, out
    assert not any(c[0] == "ydotool" for c in seen)


def test_ydotoold_unavailable_names_the_daemon(grounding):
    """Binary present, daemon down (exit 2, message on stdout): actionable
    error naming ydotoold - never a fake success."""
    seen = []
    tools_mod.set_coordinate_geometry(dict(GEOM))
    with patch.object(tools_mod.shutil, "which",
                      side_effect=_which("hyprctl", "ydotool")), \
         patch("subprocess.run", side_effect=_hypr_flow(seen, ydotool_mode="daemon")):
        out = _click_exec(512, 288)
    assert "ydotoold is not running" in out, out
    assert "could not be performed" in out and "NOT performed" in out, out
    assert "dispatched at" not in out, out


@pytest.mark.parametrize("tool,call", [
    ("type_text", lambda: (tools_mod.type_text("hi"), tools_mod.type_text("hi"))),
    ("press_key", lambda: (tools_mod.press_key("enter"), tools_mod.press_key("enter"))),
])
def test_ydotoold_unavailable_for_type_and_key(grounding, tool, call):
    with patch.object(tools_mod.shutil, "which", return_value="/usr/bin/ydotool"):
        def daemon(cmd, **kwargs):
            raise subprocess.CalledProcessError(2, cmd, output=DAEMON_MSG)
        with patch("subprocess.run", side_effect=daemon):
            tools_mod.PENDING_RISKY_ACTION = None
            call()[0]  # approval pause
            out = call()[1]
    assert "ydotoold is not running" in out, (tool, out)
    assert "NOT performed" in out, (tool, out)


def test_ydotool_missing_for_type_and_key(grounding):
    with patch.object(tools_mod.shutil, "which", return_value=None):
        for fn, arg in ((tools_mod.type_text, "hi"),
                        (tools_mod.press_key, "enter")):
            tools_mod.PENDING_RISKY_ACTION = None
            fn(arg)
            out = fn(arg)
            assert "executable not found" in out and "NOT performed" in out, out


# ---------------------------------------------------------------------------
# 6-7. Successful dispatch + non-daemon dispatch failure
# ---------------------------------------------------------------------------

def test_successful_type_and_key_dispatch(grounding):
    with patch.object(tools_mod.shutil, "which", return_value="/usr/bin/ydotool"):
        with patch("subprocess.run", return_value=_proc("")) as run:
            for fn, arg, want in ((tools_mod.type_text, "hi", "Successfully typed"),
                                  (tools_mod.press_key, "enter", "Pressed key")):
                tools_mod.PENDING_RISKY_ACTION = None
                fn(arg)
                out = fn(arg)
                assert want in out, out
    cmds = [c.args[0] for c in run.call_args_list]
    assert ["ydotool", "type", "hi"] in cmds
    assert ["ydotool", "key", "28:1", "28:0"] in cmds


def test_nondaemon_dispatch_failure_is_distinct(grounding):
    """Exit 1 without daemon markers: dispatch failure, not daemon blame."""
    with patch.object(tools_mod.shutil, "which", return_value="/usr/bin/ydotool"):
        def boom(cmd, **kwargs):
            raise subprocess.CalledProcessError(1, cmd, output="boom")
        with patch("subprocess.run", side_effect=boom):
            for fn, arg in ((tools_mod.type_text, "hi"),
                            (tools_mod.press_key, "enter")):
                tools_mod.PENDING_RISKY_ACTION = None
                fn(arg)
                out = fn(arg)
                assert "dispatch failed" in out and "exit 1" in out, out
                assert "ydotoold is not running" not in out, out
                assert "NOT performed" in out, out


def test_cursor_move_success_does_not_claim_click(grounding):
    """Daemon down after a verified cursor move: the result credits the
    verified cursor but reports the click as NOT performed."""
    seen = []
    tools_mod.set_coordinate_geometry(dict(GEOM))
    with patch.object(tools_mod.shutil, "which",
                      side_effect=_which("hyprctl", "ydotool")), \
         patch("subprocess.run", side_effect=_hypr_flow(seen, ydotool_mode="daemon")):
        out = _click_exec(512, 288)
    assert "Cursor verified at (960, 540)" in out, out
    assert "ydotoold is not running" in out, out
    assert "Target UI success NOT verified" not in out, out


# ---------------------------------------------------------------------------
# 8. Deterministic post-click verification contract
# ---------------------------------------------------------------------------

def test_verify_failed_on_unchanged_screen():
    pending = {"x": 100, "y": 100, "desc": "thumbnail", "base": "abc"}
    outcome, log_line, note = evaluate_click_verification(pending, "abc")
    assert outcome == "failed"
    assert log_line is not None and "Click verification FAILED for 'thumbnail'" in log_line
    assert "do NOT reuse" in note


def test_verify_changed_is_not_success():
    pending = {"x": 100, "y": 100, "desc": "History button", "base": "abc"}
    outcome, log_line, note = evaluate_click_verification(pending, "def")
    assert outcome == "changed"
    assert log_line is not None and "NOT confirmed" in log_line
    assert "Verification required" in note and "History button" in note
    assert "visible evidence" in note


def test_verify_no_pending_is_none():
    assert evaluate_click_verification(None, "abc")[0] == "none"
    assert evaluate_click_verification(None, None) == ("none", None, "")


def test_verify_missing_baseline_never_fails():
    """No baseline hash (or unknown new frame): treated as changed, never
    as a proven failure."""
    pending = {"x": 1, "y": 2, "desc": "t", "base": None}
    assert evaluate_click_verification(pending, "abc")[0] == "changed"
    pending2 = {"x": 1, "y": 2, "desc": "t", "base": "abc"}
    assert evaluate_click_verification(pending2, None)[0] == "changed"


# ---------------------------------------------------------------------------
# 9-11. No reuse, attempt budget, approval (agent-level, real run_loop)
# ---------------------------------------------------------------------------

def test_unchanged_guard_keeps_frame_seq_valid():
    """The unchanged-screen guard sends no new frame: the previous frame's
    seq stays current, so acting on it is not a stale refusal."""
    from test_computer_use import _vision_with_frame, _inspect_msg
    agent = make_agent()
    _vision_with_frame(agent, b"frame-A")
    session = FakeSession(
        interactions=[
            [_tool_msg("inspect_screen", {"query": "youtube"}, call_id="i-1")],
            [_tool_msg("inspect_screen", {"query": "youtube"}, call_id="i-2")],
            "hang",
        ]
    )
    wire_connect(agent, session)
    run_with_stop(agent)
    first = session.tool_responses[0].response["result"]
    assert "frame_seq=" in first, first
    second = session.tool_responses[1].response["result"]
    assert "unchanged" in second.lower(), second
    import re
    m = re.search(r"frame_seq=(\d+)", first)
    assert m is not None, first
    seq = int(m.group(1))
    assert tools_mod.get_frame_seq() == seq, \
        "guard path must not supersede the live frame"


def test_inspect_states_frame_seq_for_clicks():
    from test_computer_use import _vision_with_frame
    agent = make_agent()
    _vision_with_frame(agent, b"frame-A")
    session = FakeSession(
        interactions=[
            [_tool_msg("inspect_screen", {"query": "youtube"}, call_id="i-1")],
            "hang",
        ]
    )
    wire_connect(agent, session)
    run_with_stop(agent)
    answer = session.tool_responses[0].response["result"]
    assert "frame #" in answer and "frame_seq=" in answer, answer
    assert "stale" in answer.lower(), answer


def test_risky_approval_survives_grounding(grounding):
    """Valid grounded clicks still pause for voice approval first."""
    tools_mod.set_coordinate_geometry(dict(GEOM))
    seq = tools_mod.get_frame_seq()
    out = tools_mod.click_screen(512, 288, "History link", frame_seq=seq)
    assert "PAUSED FOR SAFETY" in out, out
    assert "Do you confirm I should click the History link?" in out, out
