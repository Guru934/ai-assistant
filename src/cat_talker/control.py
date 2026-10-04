"""Local control socket for the assistant (F2 sleep/wake path).

Hyprland cannot call into the running process, and Python must not own
the global hotkey, so F2 runs `bin/assistant-control`, which dials this
socket. Protocol: one JSON line per connection, `{"cmd": "toggle" |
"wake" | "sleep" | "status"}`; reply is one JSON line
`{"ok": true, "state": ...}` (or `{"ok": false, "error": ...}`).

The socket file itself identifies THIS instance - no process-name
matching, no killall/pkill. Only the UID's runtime dir is used.
"""

import json
import os
import socket
import threading

from cat_talker.logging_config import get_logger

logger = get_logger("cat_talker.control")


def _base_dir(runtime_dir: str | None = None) -> str:
    """One deterministic home for socket + lock.

    Always <base>/cat-talker where base is the explicit override (tests),
    $CAT_TALKER_RUNTIME_DIR, $XDG_RUNTIME_DIR (normally /run/user/<uid>),
    or ~/.cache as fallback. Server, launcher, and client resolve through
    this single function - never two paths.
    """
    base = (runtime_dir
            or os.environ.get("CAT_TALKER_RUNTIME_DIR")
            or os.environ.get("XDG_RUNTIME_DIR")
            or os.path.join(os.path.expanduser("~"), ".cache"))
    path = os.path.join(base, "cat-talker")
    os.makedirs(path, exist_ok=True)
    return path


def socket_path(runtime_dir: str | None = None) -> str:
    return os.path.join(_base_dir(runtime_dir), "control.sock")


def lock_path(runtime_dir: str | None = None) -> str:
    return os.path.join(_base_dir(runtime_dir), "launch.lock")


def _handle_command(get_agent, cmd: str, ui=None) -> dict:
    agent = get_agent()
    if agent is None:
        return {"ok": False, "error": "agent not ready"}
    try:
        # UI visibility commands never touch sleep/wake state, and
        # sleep/wake commands never touch visibility: three concepts,
        # three independent flags.
        if cmd in ("ui-toggle", "ui-show", "ui-hide"):
            if ui is None:
                return {"ok": False, "error": "ui control unavailable"}
            {"ui-toggle": ui.toggle,
             "ui-show": ui.show,
             "ui-hide": ui.hide}[cmd]()
            disp = cmd
        elif cmd == "stop":
            if ui is None:
                return {"ok": False, "error": "ui control unavailable"}
            ui.quit()
            disp = "stop"
        elif cmd == "toggle":
            disp = agent.request_toggle()
        elif cmd == "wake":
            disp = agent.request_wake()
        elif cmd == "sleep":
            disp = agent.request_sleep()
        elif cmd == "status":
            disp = "status"
        else:
            return {"ok": False, "error": f"unknown cmd {cmd!r}"}
        snap = agent.sleep.snapshot()
        snap["disposition"] = disp
        if ui is not None:
            try:
                snap["visible"] = bool(ui.visible)
            except Exception:
                pass
        return {"ok": True, "state": snap["state"], "detail": snap}
    except Exception as e:
        logger.error(f"control command {cmd!r} failed: {e}")
        return {"ok": False, "error": str(e)}


def _adopt_inherited_lock():
    """Adopt the spawn lock inherited from bin/assistant-control.

    The launcher holds the runtime-dir lock while spawning and passes
    its fd down (same open file description). Re-locking it here is a
    no-op success for us but would block rivals, which is exactly the
    ownership we need - and the fd stays open for the server's lifetime
    because this function runs until shutdown. Returns the fd, or None
    when there is nothing usable to adopt (manual launches fall back
    to opening the lock file themselves).
    """
    import fcntl
    raw = os.environ.get("CAT_TALKER_LOCK_FD", "").strip()
    if not raw:
        return None
    try:
        fd = int(raw)
    except ValueError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError, ValueError):
        # Held by someone else, or not a real inherited fd: do not use.
        return None
    return fd


def serve_forever(get_agent, path: str | None = None,
                  stop_event: threading.Event | None = None,
                  ui=None, on_ready=None):
    """Bind the control socket and serve until stop_event is set.

    A stale socket file from a dead instance is removed first; an
    EADDRINUSE from a LIVE instance propagates so a second server can
    never steal this instance's identity.

    on_ready, if given, is called with True once listening, or False
    if another live instance owns this runtime dir. Ownership is an
    exclusive lock held for the server's whole lifetime: without it a
    second server merely unlinks the live socket file and binds its own,
    leaving two live microphone pipelines (the duplicate-listener
    failure) with F2/F3 reaching only one of them. A child spawned by
    bin/assistant-control inherits the already-held lock fd via
    CAT_TALKER_LOCK_FD and adopts it (same open file description, so no
    self-conflict); otherwise the lock file is opened and locked here.
    """
    path = path or socket_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except OSError:
        pass
    import fcntl
    lock_fd = _adopt_inherited_lock()
    if lock_fd is None:
        try:
            lock_fd = os.open(os.path.join(os.path.dirname(path) or ".",
                                           "launch.lock"),
                              os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            lock_fd = None
    if lock_fd is not None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            # Another live instance holds this runtime dir - never unlink
            # its socket, never steal it, never run headless beside it.
            try:
                os.close(lock_fd)
            except OSError:
                pass
            if on_ready is not None:
                try:
                    on_ready(False)
                except Exception:
                    pass
            raise OSError("assistant already running in this runtime dir")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
    except OSError:
        # A live instance owns this path - never steal it.
        srv.close()
        if on_ready is not None:
            try:
                on_ready(False)
            except Exception:
                pass
        raise
    srv.listen(8)
    srv.settimeout(0.5)
    logger.info(f"control socket listening on {path}")
    if on_ready is not None:
        try:
            on_ready(True)
        except Exception:
            pass
    try:
        while stop_event is None or not stop_event.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    conn.settimeout(5.0)
                    data = b""
                    while not data.endswith(b"\n") and len(data) < 4096:
                        chunk = conn.recv(1024)
                        if not chunk:
                            break
                        data += chunk
                    try:
                        msg = json.loads(data.decode("utf-8").strip() or "{}")
                    except (ValueError, UnicodeDecodeError):
                        msg = {}
                    reply = _handle_command(get_agent, msg.get("cmd", ""),
                                              ui=ui)
                    conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
                except (OSError, ValueError):
                    pass
    finally:
        srv.close()
        try:
            os.unlink(path)
        except OSError:
            pass
        if lock_fd is not None:
            try:
                os.close(lock_fd)  # releases the runtime-dir ownership lock
            except OSError:
                pass


def send_command(cmd: str, path: str | None = None, timeout: float = 3.0) -> dict:
    """Client side (used by bin/assistant-control and tests)."""
    path = path or socket_path()
    cli = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    cli.settimeout(timeout)
    try:
        cli.connect(path)
        cli.sendall((json.dumps({"cmd": cmd}) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = cli.recv(1024)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode("utf-8").strip() or "{}")
    finally:
        cli.close()
