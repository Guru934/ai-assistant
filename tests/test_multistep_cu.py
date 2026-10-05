"""Focused tests for reliable multi-step computer-use tasks.

The model still decides every step; the agent provides deterministic
execution-state safeguards (ComputerUseContext): inspect -> act ->
inspect sequencing, fresh-frame enforcement between coordinate actions,
and a bounded attempt budget. Real run_loop, fake devices and tools.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cat_talker.agent import (
    CU_MAX_FAILURES,
    ComputerUseContext,
)
from test_computer_use import GEOM_1080P
from test_session_lifecycle import (
    FakeSession,
    make_agent,
    run_with_stop,
    wire_connect,
    _tool_msg,
)


def _ms_tools(calls, click_results=None):
    """Fake tool map: controllable click outcomes, honest successes."""
    state = {"clicks": 0}

    def click_screen(x=0, y=0, target_description="", frame_seq=None):
        calls.append(("click_screen", x, y, target_description))
        if click_results is not None and state["clicks"] < len(click_results):
            out = click_results[state["clicks"]]
        else:
            out = ("OS click dispatched at global test coords "
                   "[image (%s, %s)]." % (x, y))
        state["clicks"] += 1
        return out

    def inspect_screen(query="", monitor=""):
        calls.append(("inspect_screen", query))
        return "FAKE inspect"

    def type_text(text=""):
        calls.append(("type_text", text))
        return "Successfully typed: %s" % text

    def press_key(key=""):
        calls.append(("press_key", key))
        return "Pressed key %s" % key

    fakes = [click_screen, inspect_screen, type_text, press_key]
    patchers = [
        patch("cat_talker.tools.ALL_TOOLS", fakes),
        patch("cat_talker.tools.start_media_ducking"),
        patch("cat_talker.tools.stop_media_ducking"),
        patch("cat_talker.tools.send_notification"),
    ]
    for p in patchers:
        p.start()
    calls.append(("__patchers__", patchers))
    return calls


def _stop_patches(calls):
    for entry in calls:
        if entry[0] == "__patchers__":
            for p in entry[1]:
                p.stop()


def _vision_frames(agent, payloads):
    agent.vision.capture_frame = MagicMock(side_effect=list(payloads))
    agent.vision.last_capture_geometry = dict(GEOM_1080P)


def _click(x, y, call_id, desc="target"):
    return _tool_msg("click_screen",
                     {"x": x, "y": y, "target_description": desc},
                     call_id=call_id)


def _inspect(call_id, query="page"):
    return _tool_msg("inspect_screen", {"query": query}, call_id=call_id)


def _run(agent, interactions):
    session = FakeSession(interactions=interactions + ["hang"])
    wire_connect(agent, session)
    run_with_stop(agent)
    return session


# ---------------------------------------------------------------------------
# 1-2. Single-step and multi-step sequences still work
# ---------------------------------------------------------------------------

def test_single_step_action_still_works():
    calls = []
    _ms_tools(calls)
    try:
        agent = make_agent()
        _vision_frames(agent, [b"A", b"B"])
        session = _run(agent, [
            [_inspect("i-0")],
            [_click(100, 100, "k-1")],
            [_inspect("i-1")],
        ])
        clicks = [c for c in calls if c[0] == "click_screen"]
        assert clicks == [("click_screen", 100, 100, "target")], clicks
        assert len(session.tool_responses) == 3
        assert agent.cu.failures == 0
        assert agent.cu.last_action == "click_screen"
    finally:
        _stop_patches(calls)


def test_successful_multi_step_completion():
    """inspect -> click -> inspect -> type -> press -> inspect: every step
    executes, context ends clean, nothing blocked."""
    calls = []
    _ms_tools(calls)
    try:
        agent = make_agent()
        _vision_frames(agent, [b"A", b"B", b"C"])
        session = _run(agent, [
            [_inspect("i-0")],
            [_click(100, 100, "k-1", desc="search box")],
            [_inspect("i-1")],
            [_tool_msg("type_text", {"text": "cats"}, call_id="t-1")],
            [_tool_msg("press_key", {"key": "enter"}, call_id="p-1")],
            [_inspect("i-2")],
        ])
        kinds = [c[0] for c in calls if not c[0].startswith("__")]
        assert kinds == ["inspect_screen", "click_screen", "inspect_screen",
                         "type_text", "press_key", "inspect_screen"], kinds
        assert len(session.tool_responses) == 6
        assert all("BLOCKED" not in r.response.get("result", "")
                   for r in session.tool_responses)
        assert agent.cu.failures == 0
        assert agent.cu.screenshot_required() is False
    finally:
        _stop_patches(calls)


# ---------------------------------------------------------------------------
# 3. Required inspection between dependent actions
# ---------------------------------------------------------------------------

def test_second_click_blocked_until_reinspect():
    """click -> click with no inspect between: the second never executes,
    and the model is told to inspect first."""
    calls = []
    _ms_tools(calls)
    try:
        agent = make_agent()
        _vision_frames(agent, [b"A", b"B"])
        session = _run(agent, [
            [_inspect("i-0")],
            [_click(100, 100, "k-1")],
            [_click(200, 200, "k-2", desc="other target")],
            [_inspect("i-1")],
            [_click(200, 200, "k-3", desc="other target")],
        ])
        clicks = [c for c in calls if c[0] == "click_screen"]
        assert clicks == [("click_screen", 100, 100, "target"),
                          ("click_screen", 200, 200, "other target")], clicks
        blocked = session.tool_responses[2].response["result"]
        assert "BLOCKED" in blocked and "inspect_screen once" in blocked, blocked
        assert len(session.tool_responses) == 5
    finally:
        _stop_patches(calls)


def test_type_then_enter_needs_no_inspect():
    """press_key carries no coordinates: type -> enter stays legal."""
    calls = []
    _ms_tools(calls)
    try:
        agent = make_agent()
        _vision_frames(agent, [b"A"])
        session = _run(agent, [
            [_inspect("i-0")],
            [_tool_msg("type_text", {"text": "cats"}, call_id="t-1")],
            [_tool_msg("press_key", {"key": "enter"}, call_id="p-1")],
        ])
        kinds = [c[0] for c in calls if not c[0].startswith("__")]
        assert kinds == ["inspect_screen", "type_text", "press_key"], kinds
        assert len(session.tool_responses) == 3
    finally:
        _stop_patches(calls)


def test_type_invalidates_frame_for_next_click():
    """Typing changed the screen: the next click needs a fresh frame."""
    calls = []
    _ms_tools(calls)
    try:
        agent = make_agent()
        _vision_frames(agent, [b"A", b"B"])
        session = _run(agent, [
            [_inspect("i-0")],
            [_tool_msg("type_text", {"text": "cats"}, call_id="t-1")],
            [_click(100, 100, "k-1")],
            [_inspect("i-1")],
            [_click(100, 100, "k-2", desc="again")],
        ])
        blocked = session.tool_responses[2].response["result"]
        assert "BLOCKED" in blocked and "inspect_screen once" in blocked, blocked
        clicks = [c for c in calls if c[0] == "click_screen"]
        assert clicks == [("click_screen", 100, 100, "again")], clicks
    finally:
        _stop_patches(calls)


# ---------------------------------------------------------------------------
# 5-8. Verification failure, retry limit, honest multi-step failure
# ---------------------------------------------------------------------------

def test_failed_multi_step_completion_is_honest():
    """Dispatched click, unchanged screen: verification fails, the point is
    recorded, and the retry is refused - never a fake success."""
    calls = []
    _ms_tools(calls)
    try:
        agent = make_agent()
        _vision_frames(agent, [b"A", b"A"])
        session = _run(agent, [
            [_inspect("i-0")],
            [_click(100, 100, "k-1")],
            [_inspect("i-1")],
            [_click(100, 100, "k-2", desc="same point")],
        ])
        assert (100, 100) in agent._failed_coords
        assert agent.cu.failures >= 1
        retry = session.tool_responses[3].response["result"]
        assert "BLOCKED" in retry, retry
        verify = session.tool_responses[2].response["result"]
        assert "did NOT change the screen" in verify, verify
    finally:
        _stop_patches(calls)


def test_retry_limit_bounds_failed_attempts():
    """Repeated dispatch failures hit CU_MAX_FAILURES: further computer-use
    actions are refused with an exhaustion message, not retried forever."""
    fail = ("Cursor move to global (1, 1) failed: boom. Click NOT sent.",)
    calls = []
    _ms_tools(calls, click_results=list(fail * CU_MAX_FAILURES))
    try:
        agent = make_agent()
        _vision_frames(agent, [b"A"] * (CU_MAX_FAILURES + 2))
        interactions = [[_inspect("i-0")]]
        for n in range(CU_MAX_FAILURES + 1):
            interactions.append(
                [_click(10 + n, 10 + n, f"k-{n}", desc=f"t{n}")])
        session = _run(agent, interactions)
        clicks = [c for c in calls if c[0] == "click_screen"]
        assert len(clicks) == CU_MAX_FAILURES, clicks
        last = session.tool_responses[-1].response["result"]
        assert "maximum" in last and "failed computer-use attempts" in last, last
        assert f"{CU_MAX_FAILURES}" in last, last
    finally:
        _stop_patches(calls)


# ---------------------------------------------------------------------------
# 9-10. Approval intact, ydotool failures propagate
# ---------------------------------------------------------------------------

def test_approval_pause_changes_no_context():
    agent = make_agent()
    before = (agent.cu.last_action_seq, agent.cu.failures,
              agent.cu.verification_pending)
    agent._update_cu_context("click_screen",
                             "PAUSED FOR SAFETY. You MUST ask the user out loud.")
    agent._update_cu_context("type_text", "PAUSED FOR SAFETY. Ask.")
    assert (agent.cu.last_action_seq, agent.cu.failures,
            agent.cu.verification_pending) == before


def test_ydotool_failure_propagates_and_counts():
    daemon_msg = ("Click at global (1, 1): ydotoold is not running "
                  "(ydotool exit 2: failed to connect). Input injection "
                  "could not be performed. Click NOT performed.")
    calls = []
    _ms_tools(calls, click_results=[daemon_msg])
    try:
        agent = make_agent()
        _vision_frames(agent, [b"A"])
        session = _run(agent, [
            [_inspect("i-0")],
            [_click(10, 10, "k-1")],
        ])
        answer = session.tool_responses[1].response["result"]
        assert answer == daemon_msg, answer
        assert agent.cu.failures == 1
    finally:
        _stop_patches(calls)


# ---------------------------------------------------------------------------
# Context unit tests (deterministic transitions)
# ---------------------------------------------------------------------------

def test_context_transitions():
    agent = make_agent()
    cu = agent.cu
    assert cu.click_requires_fresh_frame() is False
    assert cu.exhausted() is False
    assert cu.screenshot_required() is False
    cu.note_executed("type_text")  # no frame yet: action seq stays 0
    assert cu.last_action == "type_text"
    assert cu.click_requires_fresh_frame() is False
    cu.note_frame_sent(7)
    assert cu.screenshot_required() is False
    cu.note_executed("click_screen")
    assert cu.click_requires_fresh_frame() is True
    assert cu.screenshot_required() is True
    cu.note_frame_sent(7)  # same seq: still needs a NEWER frame
    assert cu.click_requires_fresh_frame() is True
    cu.note_frame_sent(8)
    assert cu.click_requires_fresh_frame() is False
    assert cu.screenshot_required() is False
    for _ in range(CU_MAX_FAILURES):
        assert cu.exhausted() is False
        cu.note_failure()
    assert cu.exhausted() is True


def test_context_ignores_non_cu_and_nonstrings():
    agent = make_agent()
    agent._update_cu_context("open_website", "Successfully opened website: x")
    agent._update_cu_context("click_screen", None)
    assert agent.cu.last_action == ""
    assert agent.cu.failures == 0


def test_context_resets_per_interaction():
    agent = make_agent()
    agent.cu.note_frame_sent(9)
    agent.cu.note_executed("click_screen")
    agent.cu.note_failure()
    agent._start_new_interaction()
    assert agent.cu.last_frame_seq == 0
    assert agent.cu.last_action_seq == 0
    assert agent.cu.failures == 0
    assert agent.cu.click_requires_fresh_frame() is False
