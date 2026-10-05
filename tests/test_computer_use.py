"""Focused hardware-free tests for computer-use reliability.

Covers the image-space coordinate contract (vision -> tools), the
inspect_screen repeat guard, and type_text failure reporting. No display,
no ydotool, no PyAudio: subprocess/shutil are stubbed and run_loop tests
reuse fakes (real agent + tool dispatch code paths, fake devices).
"""

import logging
import os
import subprocess
import sys
import types as pytypes
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cat_talker.tools as tools_mod
from cat_talker.vision import image_to_screen_coords
from test_session_lifecycle import (
    FakeSession,
    make_agent,
    run_with_stop,
    wire_connect,
    _tool_msg,
    _turn_complete_msg,
)


GEOM_1080P = {"img_w": 1024, "img_h": 576, "src_w": 1920, "src_h": 1080,
              "x": 0, "y": 0, "scale": 1.0}


def _fake_proc(stdout=""):
    m = MagicMock()
    m.stdout = stdout
    m.returncode = 0
    return m


def _hyprland_run(recorded, cursorpos="960, 540", move_fail=False,
                  query_fail=False, move_stderr=""):
    """Stub subprocess.run for the Hyprland click flow."""
    def fake_run(cmd, **kwargs):
        recorded.append(cmd)
        if (len(cmd) == 3 and cmd[:2] == ["hyprctl", "dispatch"]
                and cmd[2].startswith("hl.dsp.cursor.move(")):
            if move_fail:
                raise subprocess.CalledProcessError(
                    7, cmd, output="", stderr=move_stderr)
            return _fake_proc("")
        if cmd == ["hyprctl", "cursorpos"]:
            if query_fail:
                raise subprocess.CalledProcessError(1, cmd)
            return _fake_proc(cursorpos)
        if cmd[:2] == ["ydotool", "click"]:
            return _fake_proc("")
        raise AssertionError("unexpected command: %r" % (cmd,))
    return fake_run


def _which(*names):
    def fake_which(name):
        return "/usr/bin/" + name if name in names else None
    return fake_which


def _click_twice(x, y, desc=""):
    """Drive through the voice-approval gate: 1st call pauses, identical
    2nd call executes. Coordinates are grounded on the current frame
    (geometry must already be recorded by the caller)."""
    seq = tools_mod.get_frame_seq()
    tools_mod.click_screen(x, y, desc, frame_seq=seq)
    return tools_mod.click_screen(x, y, desc, frame_seq=seq)


@pytest.fixture
def risky_reset():
    """Isolate the voice-approval gate and the coordinate store."""
    tools_mod.PENDING_RISKY_ACTION = None
    tools_mod.set_coordinate_geometry(None)
    yield tools_mod
    tools_mod.PENDING_RISKY_ACTION = None
    tools_mod.set_coordinate_geometry(None)


@pytest.fixture
def cu_tools():
    """Fake tool map: real agent dispatch, no desktop side effects."""
    calls = []

    def click_screen(x=0, y=0, target_description=""):
        calls.append(("click_screen", x, y, target_description))
        return ("OS click dispatched at global test coords "
                "[image (%s, %s)]." % (x, y))

    def inspect_screen(query="", monitor=""):
        calls.append(("inspect_screen", query))
        return "FAKE inspect"

    fakes = [click_screen, inspect_screen]
    with patch("cat_talker.tools.ALL_TOOLS", fakes), \
         patch("cat_talker.tools.start_media_ducking"), \
         patch("cat_talker.tools.stop_media_ducking"), \
         patch("cat_talker.tools.send_notification"):
        yield calls


# ---------------------------------------------------------------------------
# Coordinate contract
# ---------------------------------------------------------------------------

def test_image_center_maps_to_native_center():
    """1024x576 image pixels map onto 1920x1080 native pixels."""
    assert image_to_screen_coords(512, 288, GEOM_1080P) == (960, 540)
    assert image_to_screen_coords(0, 0, GEOM_1080P) == (0, 0)
    assert image_to_screen_coords(1024, 576, GEOM_1080P) == (1920, 1080)


def test_monitor_offset_is_applied():
    """Monitors not at (0,0) shift the result; negatives work too."""
    right = dict(GEOM_1080P, x=1920, y=0)
    assert image_to_screen_coords(0, 0, right) == (1920, 0)
    assert image_to_screen_coords(1024, 576, right) == (3840, 1080)
    left = dict(GEOM_1080P, x=-1920, y=0)
    assert image_to_screen_coords(1024, 0, left) == (0, 0)


def test_image_to_global_layout_respects_scale():
    """Hyprland layout coordinates are logical pixels: physical / scale."""
    from cat_talker.vision import image_to_global_layout
    assert image_to_global_layout(512, 288, GEOM_1080P) == (960, 540)
    hidpi = {"img_w": 1024, "img_h": 640, "src_w": 3840, "src_h": 2400,
             "x": 0, "y": 0, "scale": 2.0}
    assert image_to_global_layout(512, 320, hidpi) == (960, 600)
    off = dict(GEOM_1080P, x=1920, y=0)
    assert image_to_global_layout(0, 0, off) == (1920, 0)


def test_hyprland_click_moves_verifies_clicks(risky_reset):
    """Real click_screen on Hyprland: image (512,288) -> movecursor global
    (960,540) -> cursorpos verified -> ydotool button press only. No
    ydotool absolute movement. Result reports the full verification."""
    seen = []
    with patch.object(tools_mod.shutil, "which",
                      side_effect=_which("hyprctl", "ydotool")), \
         patch("subprocess.run",
               side_effect=_hyprland_run(seen, cursorpos="960, 540")):
        tools_mod.set_coordinate_geometry(GEOM_1080P)
        result = _click_twice(512, 288)
    moves = [c for c in seen
             if len(c) == 3 and c[:2] == ["hyprctl", "dispatch"]]
    assert moves, "no hyprctl dispatch ran: %r" % (seen,)
    assert moves[0][2] == "hl.dsp.cursor.move({x = 960, y = 540})", moves
    assert not any(c[:2] == ["ydotool", "mousemove"] for c in seen), \
        "ydotool absolute movement must not be used on Hyprland"
    assert any(c[:2] == ["ydotool", "click"] for c in seen), \
        "verified click never sent the button press"
    assert "cursor verified at (960, 540) within 3px" in result, result
    assert "Target UI success NOT verified" in result


def test_move_command_construction():
    """The exact argv for the supported Hyprland cursor API (legacy
    `dispatch movecursor X Y` fails with exit 7 on 0.56+)."""
    assert tools_mod._hyprctl_move_command(137, 1187) == [
        "hyprctl", "dispatch", "hl.dsp.cursor.move({x = 137, y = 1187})",
    ]


def test_move_failure_reports_hyprland_stderr(risky_reset):
    """Exit 7 surfaces the real Hyprland stderr; the click is withheld."""
    seen = []
    stderr = "error: dispatch: ')' expected near '137'"
    with patch.object(tools_mod.shutil, "which",
                      side_effect=_which("hyprctl", "ydotool")), \
         patch("subprocess.run",
               side_effect=_hyprland_run(seen, move_fail=True,
                                         move_stderr=stderr)):
        tools_mod.set_coordinate_geometry(GEOM_1080P)
        result = _click_twice(512, 288)
    assert not any(c[:2] == ["ydotool", "click"] for c in seen)
    assert "exit 7" in result and "Click NOT sent" in result, result
    assert stderr in result, result


def test_cursorpos_parsing():
    assert tools_mod.parse_cursorpos("422, 720") == (422, 720)
    assert tools_mod.parse_cursorpos("  960,540\n") == (960, 540)
    with pytest.raises(ValueError):
        tools_mod.parse_cursorpos("nope")


def test_tolerance_boundary(risky_reset):
    """<= 3px passes (click sent); 4px fails (click withheld)."""
    for actual, want_click in (("963, 540", True), ("964, 540", False)):
        seen = []
        with patch.object(tools_mod.shutil, "which",
                          side_effect=_which("hyprctl", "ydotool")), \
             patch("subprocess.run",
                   side_effect=_hyprland_run(seen, cursorpos=actual)):
            tools_mod.set_coordinate_geometry(GEOM_1080P)
            tools_mod.PENDING_RISKY_ACTION = None
            result = _click_twice(512, 288)
        clicks = [c for c in seen if c[:2] == ["ydotool", "click"]]
        assert bool(clicks) is want_click, (actual, result)


def test_click_not_sent_when_verification_fails(risky_reset):
    """Cursor lands far away: explicit failure, button press withheld."""
    seen = []
    with patch.object(tools_mod.shutil, "which",
                      side_effect=_which("hyprctl", "ydotool")), \
         patch("subprocess.run",
               side_effect=_hyprland_run(seen, cursorpos="100, 100")):
        tools_mod.set_coordinate_geometry(GEOM_1080P)
        result = _click_twice(512, 288)
    assert not any(c[:2] == ["ydotool", "click"] for c in seen)
    assert "failed verification" in result and "Click NOT sent" in result


def test_click_not_sent_when_cursorpos_unavailable(risky_reset):
    """cursorpos itself failing is an explicit error, never a fake success."""
    seen = []
    with patch.object(tools_mod.shutil, "which",
                      side_effect=_which("hyprctl", "ydotool")), \
         patch("subprocess.run",
               side_effect=_hyprland_run(seen, query_fail=True)):
        tools_mod.set_coordinate_geometry(GEOM_1080P)
        result = _click_twice(512, 288)
    assert not any(c[:2] == ["ydotool", "click"] for c in seen)
    assert "position verification" in result and "Click NOT sent" in result


def test_non_hyprland_fallback_uses_ydotool_move(risky_reset):
    """Without hyprctl the pre-existing ydotool absolute path is preserved
    (native physical coordinates)."""
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return _fake_proc("")

    with patch.object(tools_mod.shutil, "which",
                      side_effect=_which("ydotool")), \
         patch("subprocess.run", side_effect=fake_run):
        tools_mod.set_coordinate_geometry(GEOM_1080P)
        result = _click_twice(512, 288)
    moves = [c for c in seen if c[:2] == ["ydotool", "mousemove"]]
    assert moves and moves[0][-2:] == ["960", "540"], moves
    assert not any(c[0] == "hyprctl" for c in seen)
    assert "NOT verified" in result


def test_click_approval_uses_semantic_description(risky_reset):
    """Spoken approval names the target, never raw coordinates."""
    tools_mod.set_coordinate_geometry(GEOM_1080P)
    seq = tools_mod.get_frame_seq()
    res = tools_mod.click_screen(73, 633, "History link", frame_seq=seq)
    assert "History link" in res, res
    assert "Do you confirm I should click the History link?" in res, res
    assert "73" not in res and "633" not in res, res


def test_click_approval_fallback_hides_coordinates(risky_reset):
    """Empty/blank description falls back without exposing coordinates."""
    tools_mod.set_coordinate_geometry(GEOM_1080P)
    seq = tools_mod.get_frame_seq()
    for desc in ("", "   "):
        tools_mod.PENDING_RISKY_ACTION = None
        res = tools_mod.click_screen(10, 20, desc, frame_seq=seq)
        assert "click the selected screen location" in res, res
        assert "10" not in res and "20" not in res, res


def test_confirmation_executes_original_coordinates(risky_reset):
    """confirm_action runs exactly the x/y bound at approval time (image
    (512,288) on the 1080p frame maps to global (960,540))."""
    seen = []
    with patch.object(tools_mod.shutil, "which",
                      side_effect=_which("hyprctl", "ydotool")), \
         patch("subprocess.run",
               side_effect=_hyprland_run(seen, cursorpos="960, 540")):
        tools_mod.set_coordinate_geometry(GEOM_1080P)
        seq = tools_mod.get_frame_seq()
        approval = tools_mod.click_screen(512, 288, "History link",
                                          frame_seq=seq)
        assert "History link" in approval
        result = tools_mod.confirm_action()
    moves = [c for c in seen
             if len(c) == 3 and c[:2] == ["hyprctl", "dispatch"]
             and c[2].startswith("hl.dsp.cursor.move(")]
    assert moves and moves[0][2] == "hl.dsp.cursor.move({x = 960, y = 540})", moves
    assert "cursor verified at (960, 540)" in result, result


def test_type_text_reports_ydotool_failure(risky_reset):
    """Non-zero ydotool exit is an explicit failure, not a silent lie."""
    with patch.object(tools_mod.shutil, "which", return_value="/usr/bin/ydotool"):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            tools_mod.type_text("hi")  # pauses for approval
            ok = tools_mod.type_text("hi")
        assert "Successfully typed" in ok, ok

        def boom(cmd, **kwargs):
            raise subprocess.CalledProcessError(1, cmd)

        tools_mod.PENDING_RISKY_ACTION = None
        with patch("subprocess.run", side_effect=boom):
            tools_mod.type_text("hi")
            bad = tools_mod.type_text("hi")
        assert "dispatch failed" in bad and "1" in bad, bad
        assert "NOT performed" in bad, bad


# ---------------------------------------------------------------------------
# inspect_screen repeat guard (real run_loop, fake devices)
# ---------------------------------------------------------------------------

def _vision_with_frame(agent, payload: bytes):
    agent.vision.capture_frame = MagicMock(return_value=payload)
    agent.vision.last_capture_geometry = dict(GEOM_1080P)


def _vision_frames(agent, payloads):
    agent.vision.capture_frame = MagicMock(side_effect=list(payloads))
    agent.vision.last_capture_geometry = dict(GEOM_1080P)


def _click_msg(x, y, call_id, desc="video thumbnail"):
    return _tool_msg("click_screen",
                     {"x": x, "y": y, "target_description": desc},
                     call_id=call_id)


def _inspect_msg(call_id, query="youtube"):
    return _tool_msg("inspect_screen", {"query": query}, call_id=call_id)


def test_repeat_inspect_blocked_same_interaction(cu_tools):
    """Two inspects, unchanged screen, no action between: one frame sent,
    second call answered from the guard (protocol still gets 2 responses)."""
    agent = make_agent()
    _vision_with_frame(agent, b"frame-A")
    session = FakeSession(
        interactions=[
            [_tool_msg("inspect_screen", {"query": "youtube"}, call_id="i-1")],
            [_tool_msg("inspect_screen", {"query": "youtube"}, call_id="i-2")],
            "hang",
        ]
    )
    count = wire_connect(agent, session)
    run_with_stop(agent)
    assert count[0] == 1
    assert len(session.sent_video) == 1, \
        "unchanged screen was re-sent (%d frames)" % len(session.sent_video)
    assert len(session.tool_responses) == 2
    second = session.tool_responses[1].response["result"]
    assert "unchanged" in second.lower(), second


def test_inspect_allowed_after_desktop_action(cu_tools, caplog):
    """click (side-effecting) dirties the screen: the next inspect sends a
    fresh frame. One INFO line per inspection, no spam."""
    agent = make_agent()
    _vision_with_frame(agent, b"frame-A")
    session = FakeSession(
        interactions=[
            [_tool_msg("inspect_screen", {"query": "youtube"}, call_id="i-1")],
            [_tool_msg("click_screen", {"x": 100, "y": 100}, call_id="c-1")],
            [_tool_msg("inspect_screen", {"query": "youtube"}, call_id="i-2")],
            "hang",
        ]
    )
    count = wire_connect(agent, session)
    with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
        run_with_stop(agent)
    assert count[0] == 1
    assert len(session.sent_video) == 2, \
        "post-action verification frame was not sent"
    infos = [r for r in caplog.records if "Inspection sent" in r.message]
    assert len(infos) == 2, "expected one log line per inspection, got %d" % len(infos)


# ---------------------------------------------------------------------------
# Click verification + retry policy (real run_loop, fake devices)
# ---------------------------------------------------------------------------

def test_failed_click_never_reuses_coordinates(caplog, cu_tools):
    """Unchanged screen after a click marks the point failed; identical
    retries are blocked indefinitely (protocol still answered)."""
    agent = make_agent()
    _vision_frames(agent, [b"A", b"A"])
    session = FakeSession(
        interactions=[
            [_inspect_msg("i-0")],
            [_click_msg(100, 100, "k-1")],
            [_inspect_msg("i-1")],
            [_click_msg(100, 100, "k-2", desc="thumbnail center")],
            [_click_msg(100, 100, "k-3", desc="thumbnail middle")],
            "hang",
        ]
    )
    count = wire_connect(agent, session)
    with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
        run_with_stop(agent)
    assert count[0] == 1
    clicks = [c for c in cu_tools if c[0] == "click_screen"]
    assert clicks == [("click_screen", 100, 100, "video thumbnail")], clicks
    assert len(session.tool_responses) == 5
    assert any("Click verification FAILED for 'video thumbnail'" in r.message
               for r in caplog.records), "failed verification was not logged"
    blocked = [session.tool_responses[3].response["result"],
               session.tool_responses[4].response["result"]]
    assert all("BLOCKED" in r for r in blocked), blocked
    assert all("Do not reuse failed coordinates" in r or "Do NOT reuse" in r
               for r in blocked)


def test_successful_verification_ends_the_action(cu_tools):
    """A changed screen clears the failure state: the same point may be
    clicked again afterwards."""
    agent = make_agent()
    _vision_frames(agent, [b"A", b"B"])
    session = FakeSession(
        interactions=[
            [_inspect_msg("i-0")],
            [_click_msg(100, 100, "k-1")],
            [_inspect_msg("i-1")],
            [_click_msg(100, 100, "k-2", desc="thumbnail again")],
            "hang",
        ]
    )
    count = wire_connect(agent, session)
    run_with_stop(agent)
    assert count[0] == 1
    clicks = [c for c in cu_tools if c[0] == "click_screen"]
    assert clicks == [("click_screen", 100, 100, "video thumbnail"),
                      ("click_screen", 100, 100, "thumbnail again")], clicks
    assert len(session.tool_responses) == 4


def test_retry_budget_enforced_then_tell_user(cu_tools):
    """Two failed alternate points exhaust the budget: the next click is
    blocked with a tell-the-user message naming the described target."""
    agent = make_agent()
    _vision_frames(agent, [b"A", b"A", b"A"])
    session = FakeSession(
        interactions=[
            [_inspect_msg("i-0")],
            [_click_msg(100, 100, "k-1")],
            [_inspect_msg("i-1")],
            [_click_msg(200, 200, "k-2", desc="thumbnail lower part")],
            [_inspect_msg("i-2")],
            [_click_msg(300, 300, "k-3", desc="History link")],
            "hang",
        ]
    )
    count = wire_connect(agent, session)
    run_with_stop(agent)
    assert count[0] == 1
    clicks = [c for c in cu_tools if c[0] == "click_screen"]
    assert clicks == [
        ("click_screen", 100, 100, "video thumbnail"),
        ("click_screen", 200, 200, "thumbnail lower part"),
    ], clicks
    assert len(session.tool_responses) == 6
    last = session.tool_responses[5].response["result"]
    assert "could not be reliably located" in last, last
    assert "History link" in last, last
    assert agent._click_failures == 2, \
        "retry budget is not exactly 2: %r" % (agent._click_failures,)


# ---------------------------------------------------------------------------
# Higher-resolution inspection + honest target verification
# ---------------------------------------------------------------------------

GEOM_1536 = {"img_w": 1536, "img_h": 960, "src_w": 1920, "src_h": 1200,
             "x": 0, "y": 0, "scale": 1.0}


def test_hires_geometry_maps_to_global():
    """1536x960 inspection frames map exactly through the same pipeline."""
    from cat_talker.vision import image_to_global_layout
    assert image_to_global_layout(768, 480, GEOM_1536) == (960, 600)
    assert image_to_global_layout(0, 0, GEOM_1536) == (0, 0)
    assert image_to_global_layout(1536, 960, GEOM_1536) == (1920, 1200)


def test_instructions_have_no_fixed_coordinate_assumptions():
    """The prompt must not name a native grid or fixed target locations."""
    from cat_talker.agent import build_system_instructions
    text = build_system_instructions()
    assert "1920x1080" not in text
    assert "0-1023" not in text
    for required in ("EXACT pixel dimensions",
                     "NEVER infer",
                     "NEVER assume",
                     "NEVER reuse a previous",
                     "CURRENT",
                     "do NOT guess coordinates"):
        assert required in text, "missing grounding rule: %r" % required


def _vision_hires(agent, payloads):
    agent.vision.capture_frame = MagicMock(side_effect=list(payloads))
    agent.vision.last_capture_geometry = dict(GEOM_1536)


def test_changed_screen_is_not_target_success(caplog, cu_tools):
    """Changed screen after a click: reported honestly (changed, but target
    NOT confirmed), with the verification question naming the described
    target. Pending clears, nothing is marked failed, budget untouched."""
    agent = make_agent()
    _vision_hires(agent, [b"A", b"B"])
    session = FakeSession(
        interactions=[
            [_inspect_msg("i-0")],
            [_click_msg(100, 100, "k-1", desc="History button in the sidebar")],
            [_inspect_msg("i-1")],
            "hang",
        ]
    )
    count = wire_connect(agent, session)
    with caplog.at_level(logging.INFO, logger="cat_talker.agent"):
        run_with_stop(agent)
    assert count[0] == 1
    assert agent._pending_click is None, "verification did not end the action"
    assert (100, 100) not in agent._failed_coords
    assert agent._click_failures == 0
    assert any("NOT confirmed" in r.message for r in caplog.records), \
        "changed screen was reported as target success"
    answer = session.tool_responses[2].response["result"]
    assert "Verification required" in answer, answer
    assert "History button in the sidebar" in answer, answer
    assert "(1536x960 image pixels)" in answer, answer
    assert "0-1535 horizontally" in answer, answer
