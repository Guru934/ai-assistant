"""Focused tests for Hyprland workspace switching (Lua dispatcher).

All hyprctl calls are mocked: dispatch argv is asserted exactly, and
`activeworkspace -j` replies are scripted. No desktop session needed.
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cat_talker.agent import SIDE_EFFECT_TOOLS, build_system_instructions
from cat_talker.tools import ALL_TOOLS, switch_workspace


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _mock_hyprctl(monkeypatch, active_id=2, dispatch_fail=False,
                  active_fail=False):
    seen = {}
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/hyprctl")

    def _fake_run(argv, **kwargs):
        assert isinstance(argv, list)
        assert "shell" not in kwargs
        seen.setdefault("calls", []).append((argv, kwargs))
        if argv[:2] == ["hyprctl", "eval"]:
            if dispatch_fail:
                raise subprocess.CalledProcessError(1, argv, "err")
            return _Proc(0, "ok")
        if argv == ["hyprctl", "activeworkspace", "-j"]:
            if active_fail:
                return _Proc(1, "")
            return _Proc(0, json.dumps({"id": active_id, "name": str(active_id)}))
        raise AssertionError(f"unexpected hyprctl call: {argv}")

    monkeypatch.setattr("subprocess.run", _fake_run)
    return seen


def _dispatch_argv(seen):
    for argv, _ in seen["calls"]:
        if argv[:2] == ["hyprctl", "eval"]:
            return argv
    raise AssertionError("no dispatch call made")


# 1-3: workspaces 1, 2, 4 ──────────────────────────────────────────

@pytest.mark.parametrize("ws", [1, 2, 3, 4, 5, 6])
def test_switch_workspace_success(monkeypatch, ws):
    seen = _mock_hyprctl(monkeypatch, active_id=ws)
    assert switch_workspace(ws) == f"Switched to workspace {ws}."
    argv = _dispatch_argv(seen)
    assert argv == ["hyprctl", "eval",
                    f"hl.dispatch(hl.dsp.focus({{ workspace = \"{ws}\" }}))"]


# 4-5: invalid values + bounds ─────────────────────────────────────

@pytest.mark.parametrize("bad", ["abc", None, "2; evil", "", [2]])
def test_invalid_values_rejected(monkeypatch, bad):
    _mock_hyprctl(monkeypatch)
    out = switch_workspace(bad)
    assert "integer" in out.lower() or "Error" in out


def test_bounds_clamped(monkeypatch):
    seen = _mock_hyprctl(monkeypatch, active_id=1)
    assert switch_workspace(0) == "Switched to workspace 1."
    assert 'workspace = "1"' in _dispatch_argv(seen)[2]


def test_upper_bound_clamped(monkeypatch):
    seen = _mock_hyprctl(monkeypatch, active_id=10)
    assert switch_workspace(99) == "Switched to workspace 10."
    assert 'workspace = "10"' in _dispatch_argv(seen)[2]


# 6-7: missing binary + dispatch failure ───────────────────────────

def test_missing_hyprctl(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert switch_workspace(2) == "Error: hyprctl not found."


def test_dispatch_failure_honest(monkeypatch):
    _mock_hyprctl(monkeypatch, dispatch_fail=True)
    out = switch_workspace(2)
    assert "failed" in out.lower()
    assert out != "Switched to workspace 2."


# 8: safe argv ─────────────────────────────────────────────────────

def test_old_plain_syntax_gone():
    import inspect
    from cat_talker import tools as tools_mod
    src = inspect.getsource(tools_mod.switch_workspace)
    assert "dispatch workspace" not in src
    assert "exec_raw" not in src
    assert "hl.dsp.focus" in src


def test_no_shell_or_string_command(monkeypatch):
    seen = _mock_hyprctl(monkeypatch, active_id=3)
    switch_workspace(3)
    for argv, kwargs in seen["calls"]:
        assert isinstance(argv, list)
        assert kwargs.get("shell") in (None, False)
        assert kwargs.get("timeout") is not None


# 9-10: verification ───────────────────────────────────────────────

def test_verification_mismatch_honest(monkeypatch):
    _mock_hyprctl(monkeypatch, active_id=1)
    out = switch_workspace(2)
    assert "did not take effect" in out
    assert "active workspace is 1" in out
    assert out != "Switched to workspace 2."


def test_unverifiable_state_honest(monkeypatch):
    _mock_hyprctl(monkeypatch, active_fail=True)
    out = switch_workspace(2)
    assert "could not be verified" in out
    assert out != "Switched to workspace 2."


# 11-13: registration, policy, guidance ───────────────────────────

def test_registered_exactly_once():
    names = [f.__name__ for f in ALL_TOOLS]
    assert names.count("switch_workspace") == 1


def test_side_effect_membership():
    assert "switch_workspace" in SIDE_EFFECT_TOOLS


def test_guidance_covers_workspace_switching():
    text = build_system_instructions()
    assert "switch workspaces (1-6" in text
    assert "workspace 5" in text
    assert "workspace 6" in text
