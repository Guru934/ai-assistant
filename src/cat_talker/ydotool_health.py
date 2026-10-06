"""Deterministic ydotool runtime health for Chibi's computer-use backend.

Distinguishes: ydotool executable missing, ydotoold executable missing,
daemon stopped, daemon unreachable (stale socket), permission/uinput
problems, and a healthy input path.

The health check NEVER performs input: no clicks, types, or key presses.
Availability is proven by (1) both binaries on PATH, (2) read/write access
to /dev/uinput for this user, and (3) a bare AF_UNIX connect to the
ydotool socket (connect+close, zero bytes sent). systemd user-service
state is consulted only to sharpen the hint, never to start anything:
Chibi never runs sudo/pkexec/root commands and never manages daemons.
"""

import os
import shutil
import socket
import subprocess

# Concise machine-readable statuses. "daemon_unusable" covers a reachable
# socket whose service is in a failed state; "permission" covers uinput
# access problems that prevent the daemon from running for this user.
HEALTHY = "healthy"
YDTOOL_MISSING = "ydotool_missing"
DAEMON_MISSING = "daemon_missing"
DAEMON_STOPPED = "daemon_stopped"
DAEMON_UNREACHABLE = "daemon_unreachable"
DAEMON_UNUSABLE = "daemon_unusable"
PERMISSION = "permission"

SERVICE_NAME = "ydotool.service"
SETUP_HINT = ("Enable the packaged user service once: "
              "`systemctl --user enable --now ydotool.service` "
              "(or run `bin/assistant-ydotool-setup`).")
PERMISSION_HINT = ("This user cannot open /dev/uinput, so ydotoold cannot "
                   "run here. Typical fix: `sudo usermod -aG input $USER` "
                   "then log out and back in (or ask an admin); also see "
                   "`journalctl --user -u ydotool.service`.")


class YdotoolHealth:
    """Small value object: status, human detail, actionable hint."""

    def __init__(self, status, detail="", hint=""):
        self.status = status
        self.detail = detail
        self.hint = hint

    def __repr__(self):
        return (f"YdotoolHealth(status={self.status!r}, "
                f"detail={self.detail!r}, hint={self.hint!r})")


def socket_path():
    """ydotool daemon socket (explicit override honored, else runtime dir)."""
    override = os.environ.get("YDOTOOL_SOCKET")
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.geteuid()}"
    return os.path.join(runtime, ".ydotool_socket")


def uinput_accessible():
    """True when this process can open /dev/uinput for read+write."""
    try:
        return os.access("/dev/uinput", os.R_OK | os.W_OK)
    except Exception:
        return False


def user_service_state():
    """Query the packaged user unit without changing anything.

    Returns (active, failed) booleans, or (None, False) when systemctl is
    unavailable or the unit is unknown (non-systemd hosts).
    """
    if not shutil.which("systemctl"):
        return (None, False)
    try:
        active = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE_NAME],
            capture_output=True, text=True, timeout=10)
        failed = subprocess.run(
            ["systemctl", "--user", "is-failed", SERVICE_NAME],
            capture_output=True, text=True, timeout=10)
        return (active.stdout.strip() == "active",
                failed.stdout.strip() == "failed")
    except Exception:
        return (None, False)


def socket_reachable(path, timeout=2.0):
    """Bare AF_UNIX connect+close. Sends zero bytes: no input performed.

    ydotoold serves a SOCK_DGRAM socket (verified live: STREAM connect
    fails with EPROTOTYPE while the daemon is healthy), so the probe uses
    the daemon's own socket type.
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.settimeout(timeout)
            sock.connect(path)
            return True
        finally:
            try:
                sock.close()
            except Exception:
                pass
    except Exception:
        return False


def check_ydotool_health():
    """Full deterministic health determination (read-only, no input)."""
    if not shutil.which("ydotool"):
        return YdotoolHealth(
            YDTOOL_MISSING,
            "ydotool executable not found on PATH.",
            "Install the ydotool package and ensure `ydotool` is on PATH.")
    if not shutil.which("ydotoold"):
        return YdotoolHealth(
            DAEMON_MISSING,
            "ydotoold executable not found on PATH (ydotool is present).",
            "Install the ydotool package (it ships both binaries).")
    if not uinput_accessible():
        return YdotoolHealth(
            PERMISSION,
            "This user cannot open /dev/uinput.",
            PERMISSION_HINT)
    path = socket_path()
    if not os.path.exists(path):
        active, failed = user_service_state()
        if failed:
            return YdotoolHealth(
                DAEMON_UNUSABLE,
                f"User service {SERVICE_NAME} is in a failed state "
                f"(see `journalctl --user -u {SERVICE_NAME}` - often a "
                f"uinput permission problem at login).",
                f"Try `systemctl --user reset-failed {SERVICE_NAME}` then "
                f"{SETUP_HINT} {PERMISSION_HINT}")
        if active is False:
            return YdotoolHealth(
                DAEMON_STOPPED,
                f"Daemon socket {path} absent; user service "
                f"{SERVICE_NAME} is not active.",
                SETUP_HINT)
        return YdotoolHealth(
            DAEMON_STOPPED,
            f"Daemon socket {path} is absent: ydotoold is not running.",
            SETUP_HINT)
    if socket_reachable(path):
        return YdotoolHealth(HEALTHY, f"Daemon socket {path} answers.", "")
    active, failed = user_service_state()
    if failed:
        return YdotoolHealth(
            DAEMON_UNUSABLE,
            f"Socket {path} exists but the daemon does not answer; user "
            f"service {SERVICE_NAME} is in a failed state.",
            f"Try `systemctl --user reset-failed {SERVICE_NAME}` then "
            f"{SETUP_HINT}")
    _ = active
    return YdotoolHealth(
        DAEMON_UNREACHABLE,
        f"Socket {path} exists but nothing answers (stale socket).",
        f"Try `systemctl --user restart {SERVICE_NAME}`, or {SETUP_HINT}")


def format_ydotool_status():
    """Operator-facing one-glance report. Returns (text, exit_code)."""
    health = check_ydotool_health()
    lines = [f"ydotool: {health.status}"]
    if health.detail:
        lines.append(f"detail: {health.detail}")
    if health.hint:
        lines.append(f"hint: {health.hint}")
    return ("\n".join(lines), 0 if health.status == HEALTHY else 1)


def user_in_input_group():
    """True/False whether this user holds the input group; None when the
    group database cannot answer. Pure read (grp/getgroups), no I/O side
    effects, no privilege needed."""
    try:
        import grp
        want = grp.getgrnam("input").gr_gid
        return want in os.getgroups()
    except Exception:
        return None


def persistent_access_hint() -> str:
    """One-time host step for reboot-proof ydotool, or "".

    Background: the packaged user unit starts at boot, but logind grants
    /dev/uinput access (uaccess ACL) only once a graphical session is
    active - the daemon fails 5x in the first second and systemd gives up
    for the whole boot (start-limit-hit). Membership in the input group
    (the upstream ydotool recommendation, see /usr/lib/udev/rules.d/
    80-uinput.rules: GROUP="input" MODE="0660") makes access static:
    no race, no session dependence, no re-login sensitivity afterwards.
    """
    if user_in_input_group() is False:
        return ("For reliable startup across reboots (no manual start): "
                "run once `sudo usermod -aG input $USER`, then log out "
                "and back in. Until then, `bin/assistant-ydotool-setup` "
                "starts the daemon for the current session only.")
    return ""
