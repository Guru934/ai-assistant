"""Focused tests for the ydotool runtime health layer.

Health states are driven by monkeypatched module functions (no daemon,
no input, no systemd needed), except the operator CLI wiring tests which
run the real bin scripts with a stripped PATH for determinism.
"""

import os
import socket
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cat_talker.ydotool_health as health_mod
from cat_talker.ydotool_health import (
    DAEMON_MISSING,
    DAEMON_STOPPED,
    DAEMON_UNREACHABLE,
    DAEMON_UNUSABLE,
    HEALTHY,
    PERMISSION,
    YDTOOL_MISSING,
    YdotoolHealth,
    check_ydotool_health,
    format_ydotool_status,
    socket_reachable,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROL = os.path.join(REPO, "bin", "assistant-control")
SETUP = os.path.join(REPO, "bin", "assistant-ydotool-setup")


def _which(names):
    def fake_which(name):
        return "/usr/bin/" + name if name in names else None
    return fake_which


@pytest.fixture
def healthy_env(monkeypatch):
    """All platform probes report a working backend."""
    monkeypatch.setattr(health_mod.shutil, "which",
                        _which(("ydotool", "ydotoold")))
    monkeypatch.setattr(health_mod, "uinput_accessible", lambda: True)
    monkeypatch.setattr(health_mod.os.path, "exists", lambda p: True)
    monkeypatch.setattr(health_mod, "socket_reachable", lambda *a, **k: True)
    monkeypatch.setattr(health_mod, "user_service_state", lambda: (True, False))


def test_healthy(healthy_env):
    h = check_ydotool_health()
    assert h.status == HEALTHY
    text, code = format_ydotool_status()
    assert code == 0 and text.startswith("ydotool: healthy")


def test_ydotool_missing(monkeypatch):
    monkeypatch.setattr(health_mod.shutil, "which", _which(()))
    h = check_ydotool_health()
    assert h.status == YDTOOL_MISSING and h.hint
    assert format_ydotool_status()[1] == 1


def test_ydotoold_missing(monkeypatch):
    monkeypatch.setattr(health_mod.shutil, "which", _which(("ydotool",)))
    monkeypatch.setattr(health_mod, "uinput_accessible", lambda: True)
    h = check_ydotool_health()
    assert h.status == DAEMON_MISSING and "ydotoold" in h.detail


def test_permission_blocks_before_socket(monkeypatch):
    monkeypatch.setattr(health_mod.shutil, "which",
                        _which(("ydotool", "ydotoold")))
    monkeypatch.setattr(health_mod, "uinput_accessible", lambda: False)
    h = check_ydotool_health()
    assert h.status == PERMISSION
    assert "uinput" in h.detail and "input" in h.hint


def test_daemon_stopped(monkeypatch, healthy_env):
    monkeypatch.setattr(health_mod.os.path, "exists", lambda p: False)
    monkeypatch.setattr(health_mod, "user_service_state", lambda: (False, False))
    h = check_ydotool_health()
    assert h.status == DAEMON_STOPPED
    assert "not active" in h.detail and "enable --now" in h.hint


def test_daemon_unreachable_stale_socket(monkeypatch, healthy_env):
    monkeypatch.setattr(health_mod, "socket_reachable", lambda *a, **k: False)
    monkeypatch.setattr(health_mod, "user_service_state", lambda: (True, False))
    h = check_ydotool_health()
    assert h.status == DAEMON_UNREACHABLE
    assert "stale socket" in h.detail


def test_daemon_unusable_when_service_failed(monkeypatch, healthy_env):
    monkeypatch.setattr(health_mod.os.path, "exists", lambda p: False)
    monkeypatch.setattr(health_mod, "user_service_state", lambda: (False, True))
    h = check_ydotool_health()
    assert h.status == DAEMON_UNUSABLE
    assert "failed state" in h.detail


def test_socket_probe_sends_zero_bytes(tmp_path):
    """A bound datagram socket is reachable, and nothing is ever sent."""
    path = str(tmp_path / "probe.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(path)
    server.settimeout(0.2)
    try:
        assert socket_reachable(path) is True
        try:
            data = server.recvfrom(64)
            raise AssertionError(f"probe sent data: {data!r}")
        except socket.timeout:
            pass  # expected: zero bytes sent
    finally:
        server.close()
    assert socket_reachable(str(tmp_path / "absent.sock")) is False


def test_user_service_state_unknown_without_systemctl(monkeypatch):
    monkeypatch.setattr(health_mod.shutil, "which", _which(()))
    assert health_mod.user_service_state() == (None, False)


def test_dispatch_refines_permission_message(monkeypatch):
    """A daemon failure plus a permission health state names the uinput
    cause; the taxonomy and NOT-performed honesty are preserved."""
    import cat_talker.tools as tools_mod

    def daemon(cmd, **kwargs):
        raise subprocess.CalledProcessError(
            2, cmd, output="failed to connect socket: nope\n"
                           "Please check if ydotoold is running.")

    monkeypatch.setattr(tools_mod.shutil, "which",
                        lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(tools_mod.subprocess, "run", daemon)
    monkeypatch.setattr(
        health_mod, "check_ydotool_health",
        lambda: YdotoolHealth(PERMISSION, "denied", "fix-it"))
    out = tools_mod._ydotool_dispatch(["ydotool", "click", "0xC0"], "Click test")
    assert out is not None
    assert "ydotoold is not running" in out, out
    assert "Likely permission cause" in out, out
    assert "NOT performed" in out, out


def test_dispatch_hint_never_breaks_taxonomy(monkeypatch):
    """If the health layer itself errors, the daemon message is unchanged."""
    import cat_talker.tools as tools_mod

    def daemon(cmd, **kwargs):
        raise subprocess.CalledProcessError(2, cmd, output="failed to connect")

    def boom():
        raise RuntimeError("health exploded")

    monkeypatch.setattr(tools_mod.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(tools_mod.subprocess, "run", daemon)
    monkeypatch.setattr(health_mod, "check_ydotool_health", boom)
    out = tools_mod._ydotool_dispatch(["ydotool", "click", "0xC0"], "Click test")
    assert out is not None
    assert "ydotoold is not running" in out and "NOT performed" in out, out


def _stripped_env(tmp_path):
    env = dict(os.environ)
    empty = str(tmp_path / "emptybin")
    os.makedirs(empty, exist_ok=True)
    env["PATH"] = empty
    env.pop("YDOTOOL_SOCKET", None)
    return env


def test_status_command_reports_missing_backend(tmp_path):
    """Real bin script, PATH without ydotool: concise missing report."""
    proc = subprocess.run(
        [sys.executable, CONTROL, "ydotool-status"],
        capture_output=True, text=True, timeout=60, cwd="/tmp",
        env=_stripped_env(tmp_path))
    assert proc.returncode == 1, proc.stdout
    assert "ydotool: ydotool_missing" in proc.stdout, proc.stdout
    assert "hint:" in proc.stdout, proc.stdout


def test_setup_script_exists_and_refuses_without_backend(tmp_path):
    assert os.path.isfile(SETUP) and os.access(SETUP, os.X_OK)
    proc = subprocess.run(
        [sys.executable, SETUP],
        capture_output=True, text=True, timeout=60, cwd="/tmp",
        env=_stripped_env(tmp_path))
    assert proc.returncode == 1, proc.stdout
    assert "cannot proceed" in proc.stdout, proc.stdout


def test_status_command_never_launches_assistant(tmp_path):
    """ydotool-status is local-only: no socket dir is created, nothing
    spawns, even with an isolated runtime dir."""
    runtime = tmp_path / "rt"
    env = _stripped_env(tmp_path)
    env["CAT_TALKER_RUNTIME_DIR"] = str(runtime)
    proc = subprocess.run(
        [sys.executable, CONTROL, "ydotool-status"],
        capture_output=True, text=True, timeout=60, cwd="/tmp", env=env)
    assert proc.returncode == 1  # backend missing under stripped PATH
    assert not os.path.exists(os.path.join(str(runtime), "control.sock"))


def test_risky_approval_unchanged_for_input_tools():
    """Voice confirmation still gates click/type/key (pause first)."""
    import cat_talker.tools as tools_mod
    tools_mod.PENDING_RISKY_ACTION = None
    try:
        out = tools_mod.click_screen(1, 2, "t")
        assert "PAUSED FOR SAFETY" in out or "Stale frame" in out, out
        tools_mod.PENDING_RISKY_ACTION = None
        out = tools_mod.type_text("hi")
        assert "PAUSED FOR SAFETY" in out, out
        tools_mod.PENDING_RISKY_ACTION = None
        out = tools_mod.press_key("enter")
        assert "PAUSED FOR SAFETY" in out, out
    finally:
        tools_mod.PENDING_RISKY_ACTION = None


# ---------------------------------------------------------------------------
# Persistent-access advisory (reboot-proof input-group step)
# ---------------------------------------------------------------------------

def test_input_group_membership_states(monkeypatch):
    import grp
    import cat_talker.ydotool_health as health_mod

    class _Group:
        gr_gid = 992

    monkeypatch.setattr(grp, "getgrnam", lambda name: _Group())
    monkeypatch.setattr(health_mod.os, "getgroups", lambda: [992, 1000])
    assert health_mod.user_in_input_group() is True
    monkeypatch.setattr(health_mod.os, "getgroups", lambda: [1000])
    assert health_mod.user_in_input_group() is False
    monkeypatch.setattr(grp, "getgrnam", lambda name: (_ for _ in ()).throw(
        KeyError("no such group")))
    assert health_mod.user_in_input_group() is None


def test_persistent_hint_only_when_missing(monkeypatch):
    import cat_talker.ydotool_health as health_mod
    monkeypatch.setattr(health_mod, "user_in_input_group", lambda: True)
    assert health_mod.persistent_access_hint() == ""
    monkeypatch.setattr(health_mod, "user_in_input_group", lambda: None)
    assert health_mod.persistent_access_hint() == ""
    monkeypatch.setattr(health_mod, "user_in_input_group", lambda: False)
    hint = health_mod.persistent_access_hint()
    assert "usermod -aG input" in hint, hint
    assert "sudo" in hint and "log out" in hint
