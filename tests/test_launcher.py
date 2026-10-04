"""Launcher/spawn-path regression tests (bin/assistant-control).

Runs the real launcher script as a subprocess against a temporary
runtime dir. Spawn targets are fakes (socket servers, exiting
processes, sleepers) driven by CAT_TALKER_SPAWN_ARGV_JSON - no desktop,
Gemini, or hardware needed.
"""

import json
import os
import subprocess
import sys
import threading
import time as _time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

_LAUNCHER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "assistant-control")


def _run_launcher(tmp_path, *args, spawn_argv=None, cwd=None,
                  extra_env=None):
    env = dict(os.environ)
    env["CAT_TALKER_RUNTIME_DIR"] = str(tmp_path)
    env.setdefault("CAT_TALKER_SPAWN_WAIT_S", "8")
    if spawn_argv is not None:
        env["CAT_TALKER_SPAWN_ARGV_JSON"] = json.dumps(spawn_argv)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, _LAUNCHER, *args],
        capture_output=True, text=True, timeout=120,
        cwd=cwd or "/tmp", env=env)


def _server_script(sock_path, marker_path, bind_delay=0.0):
    return (
        "import os, socket, time\n"
        f"open({marker_path!r}, 'a').write('spawned\\n')\n"
        f"time.sleep({bind_delay})\n"
        f"s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        f"s.bind({sock_path!r})\n"
        "s.listen(4)\n"
        "s.settimeout(20)\n"
        "end = time.monotonic() + 16\n"
        "while time.monotonic() < end:\n"
        "    try:\n"
        "        c, _ = s.accept()\n"
        "    except socket.timeout:\n"
        "        continue\n"
        "    with c:\n"
        "        data = b''\n"
        "        while not data.endswith(b'\\n'):\n"
        "            b = c.recv(1024)\n"
        "            if not b: break\n"
        "            data += b\n"
        "        c.sendall(b'{\"ok\": true, \"state\": \"awake\","
        " \"detail\": {}}\\n')\n"
    )


def _serve_in_thread(path):
    """Real control socket backed by a fake agent (healthy instance)."""
    from cat_talker.control import serve_forever
    from cat_talker.sleep import SleepController

    class _Agent:
        def __init__(self):
            self.sleep = SleepController()
            self.sleep.request_wake()
            self.loop = None

        def request_toggle(self):
            return self.sleep.request_toggle()

        def request_wake(self):
            return self.sleep.request_wake()

        def request_sleep(self):
            return self.sleep.request_sleep()

    stop = threading.Event()
    thread = threading.Thread(
        target=serve_forever,
        kwargs={"get_agent": lambda: _Agent(), "path": path,
                "stop_event": stop},
        daemon=True)
    thread.start()
    return stop, thread


# 1. healthy instance forwards, never spawns ────────────────────────

def test_healthy_instance_forwards_without_spawning(tmp_path):
    path = str(tmp_path / "cat-talker" / "control.sock")
    stop, thread = _serve_in_thread(path)
    marker = str(tmp_path / "spawns.txt")
    try:
        proc = _run_launcher(
            tmp_path, "toggle",
            spawn_argv=[sys.executable, "-c",
                        f"open({marker!r},'a').write('spawned\\n')"])
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "sleeping"  # was awake: toggle sleeps
        assert not os.path.exists(marker)
    finally:
        stop.set()
        thread.join(timeout=5)


# 2/10. no instance starts exactly one; second call reuses it ──────

def test_no_instance_starts_one_and_reuses(tmp_path):
    sock = str(tmp_path / "cat-talker" / "control.sock")
    marker = str(tmp_path / "spawns.txt")
    script = tmp_path / "srv.py"
    script.write_text(_server_script(sock, marker))
    argv = [sys.executable, str(script)]
    first = _run_launcher(tmp_path, "toggle", spawn_argv=argv)
    assert first.returncode == 0, first.stderr
    second = _run_launcher(tmp_path, "toggle", spawn_argv=argv)
    assert second.returncode == 0, second.stderr
    assert open(marker).read().count("spawned") == 1


# 3. child crashes before socket: actual failure reported ──────────

def test_crash_before_socket_reports_real_failure(tmp_path):
    proc = _run_launcher(
        tmp_path, "toggle",
        spawn_argv=[sys.executable, "-c", "import sys; sys.exit(3)"])
    assert proc.returncode == 1
    assert "code 3" in proc.stderr
    assert "exited" in proc.stderr
    assert "did not come up" not in proc.stderr


# 4. socket created late is still recognized ───────────────────────

def test_slow_socket_still_recognized(tmp_path):
    sock = str(tmp_path / "cat-talker" / "control.sock")
    marker = str(tmp_path / "spawns.txt")
    script = tmp_path / "slow.py"
    script.write_text(_server_script(sock, marker, bind_delay=1.0))
    proc = _run_launcher(tmp_path, "status",
                         spawn_argv=[sys.executable, str(script)])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "awake"


# 5. arbitrary CWD ─────────────────────────────────────────────────

def test_works_from_arbitrary_cwd(tmp_path):
    sock = str(tmp_path / "cat-talker" / "control.sock")
    marker = str(tmp_path / "spawns.txt")
    script = tmp_path / "srv.py"
    script.write_text(_server_script(sock, marker))
    proc = _run_launcher(tmp_path, "status",
                         spawn_argv=[sys.executable, str(script)],
                         cwd="/")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "awake"


# 6. missing virtualenv executable ─────────────────────────────────

def test_missing_executable_reported(tmp_path):
    proc = _run_launcher(
        tmp_path, "toggle",
        spawn_argv=["/nonexistent-venv-dir/python", "-m", "cat_talker.main"])
    assert proc.returncode == 1
    assert "cannot spawn" in proc.stderr


# 7. startup timeout names the stuck child ─────────────────────────

def test_startup_timeout_reports_running_child(tmp_path):
    proc = _run_launcher(
        tmp_path, "toggle",
        spawn_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        extra_env={"CAT_TALKER_SPAWN_WAIT_S": "2"})
    assert proc.returncode == 1
    assert "still running" in proc.stderr
    assert "pid" in proc.stderr


# 8. stale socket file is replaced ─────────────────────────────────

def test_stale_socket_replaced(tmp_path):
    sock = str(tmp_path / "cat-talker" / "control.sock")
    os.makedirs(os.path.dirname(sock), exist_ok=True)
    with open(sock, "w") as f:
        f.write("stale")
    marker = str(tmp_path / "spawns.txt")
    script = tmp_path / "srv.py"
    script.write_text(_server_script(sock, marker))
    proc = _run_launcher(tmp_path, "status",
                         spawn_argv=[sys.executable, str(script)])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "awake"


# 9/11. concurrent launches + lock ─────────────────────────────────

def test_concurrent_launches_spawn_once(tmp_path):
    sock = str(tmp_path / "cat-talker" / "control.sock")
    marker = str(tmp_path / "spawns.txt")
    script = tmp_path / "srv.py"
    script.write_text(_server_script(sock, marker, bind_delay=0.5))
    results = []

    def one():
        results.append(_run_launcher(
            tmp_path, "toggle", spawn_argv=[sys.executable, str(script)]))

    threads = [threading.Thread(target=one) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert [r.returncode for r in results] == [0, 0, 0]
    assert open(marker).read().count("spawned") == 1


def test_busy_lock_attaches_instead_of_duplicating(tmp_path):
    """A launcher blocked on the spawn lock attaches to the instance
    that comes up meanwhile instead of spawning a duplicate."""
    import fcntl
    sock = str(tmp_path / "cat-talker" / "control.sock")
    marker = str(tmp_path / "spawns.txt")
    script = tmp_path / "srv.py"
    script.write_text(_server_script(sock, marker))
    lockf = str(tmp_path / "cat-talker" / "launch.lock")
    os.makedirs(os.path.dirname(lockf), exist_ok=True)

    ready = threading.Event()

    def hold_then_serve():
        with open(lockf, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            ready.set()
            # Fake "slow starter": hold the lock, then serve.
            server = subprocess.Popen(
                [sys.executable, str(script)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _time.sleep(1.0)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            server.wait(timeout=60)

    holder = threading.Thread(target=hold_then_serve)
    holder.start()
    assert ready.wait(timeout=10)
    try:
        proc = _run_launcher(tmp_path, "status",
                             spawn_argv=[sys.executable, str(script)],
                             extra_env={"CAT_TALKER_LOCK_TIMEOUT_S": "15"})
        assert proc.returncode == 0, proc.stderr
        assert open(marker).read().count("spawned") == 1
    finally:
        holder.join(timeout=90)
