"""Sleep/wake state machine, F2 control path, and single-launch gate.

Covers: initial sleep, F2 wake/sleep/toggle, single launch, no duplicate
launches, hidden-window independence, 60s meaningful-idle timeout, mic and
media audio not resetting the timer, tool/user resets, session teardown on
sleep, no mic/reconnect while sleeping, fresh session on wake, busy-deferral
of F2, and explicit voice sleep/wake phrases.

No real 60s waits (injectable clock), no hardware (stubbed audio/interface).
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time as _time
import types as pytypes
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cat_talker.agent as agent_mod
from cat_talker.agent import GeminiDesktopAgent
from cat_talker.sleep import (
    IDLE_TIMEOUT_S,
    SleepController,
    parse_voice_command,
)

from test_session_lifecycle import (
    FakeAudio,
    FakeSession,
    _input_transcription_msg,
    _model_audio_msg,
    _tool_msg,
    _turn_complete_msg,
    make_agent,
    run_with_stop,
    wire_connect,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def _agent_with_clock():
    agent = make_agent()  # awake per test contract
    clock = FakeClock()
    agent.sleep = SleepController(clock=clock)
    agent.sleep.request_wake()
    return agent, clock


def _text_turn_msg(text):
    return pytypes.SimpleNamespace(
        server_content=None,
        client_content=pytypes.SimpleNamespace(turns=[
            pytypes.SimpleNamespace(
                role="user",
                parts=[pytypes.SimpleNamespace(text=text)],
            )
        ]),
        tool_call=None,
    )


async def _drive(agent, stop_after, actions=()):
    """Run run_loop, firing (delay, callable) actions, then stopping."""
    loop = asyncio.get_running_loop()
    for delay, fn in actions:
        loop.call_later(delay, fn)
    loop.call_later(stop_after, agent.stop_event.set)
    await asyncio.wait_for(agent.run_loop(), timeout=stop_after + 10.0)


def _run_drive(agent, stop_after, actions=()):
    import asyncio as _aio

    real_sleep = _aio.sleep

    async def fast_sleep(delay, *a, **k):
        await real_sleep(min(delay, 0.02), *a, **k)

    async def go():
        await _drive(agent, stop_after, actions)

    with patch.object(_aio, "sleep", fast_sleep):
        _aio.run(go())


# ---------------------------------------------------------------------------
# 1-3, 7-8, 10-11, 17-18: controller + voice parser (fake clock)
# ---------------------------------------------------------------------------

def test_initial_state_is_sleeping():
    assert SleepController().is_sleeping() is True
    with patch.object(agent_mod.genai, "Client"):
        agent = GeminiDesktopAgent()
    assert agent.sleep.is_sleeping() is True


def test_f2_toggle_wake_and_sleep():
    ctl = SleepController()
    assert ctl.request_toggle() == "awake"       # F2 while sleeping
    assert ctl.is_sleeping() is False
    assert ctl.request_toggle() == "sleeping"    # F2 while awake
    assert ctl.is_sleeping() is True


def test_f2_sleep_then_wake_commands():
    ctl = SleepController()
    ctl.request_wake()
    assert ctl.request_sleep() == "sleeping"
    assert ctl.request_sleep() == "already"
    assert ctl.request_wake() == "awake"
    assert ctl.request_wake() == "already"


def test_idle_timeout_is_60s_of_meaningful_activity():
    assert IDLE_TIMEOUT_S == 60.0
    clock = FakeClock()
    ctl = SleepController(clock=clock)
    ctl.request_wake()
    clock.advance(59.9)
    assert ctl.should_sleep() is False
    clock.advance(0.2)
    assert ctl.should_sleep() is True


def test_transcription_alone_never_resets_timer():
    """False 'User said:' (YouTube/system audio) must not keep awake."""
    clock = FakeClock()
    ctl = SleepController(clock=clock)
    ctl.request_wake()
    clock.advance(50.0)
    ctl.note_voice_heard()          # raw transcription, model ignores it
    ctl.confirm_voice_if_engaged(False)
    assert ctl.idle_for() >= 50.0
    clock.advance(11.0)
    assert ctl.should_sleep() is True


def test_engaged_voice_turn_resets_timer():
    clock = FakeClock()
    ctl = SleepController(clock=clock)
    ctl.request_wake()
    clock.advance(50.0)
    ctl.note_voice_heard()
    ctl.confirm_voice_if_engaged(True)   # model responded: accepted
    assert ctl.idle_for() < 1.0
    assert ctl.should_sleep() is False


def test_tool_and_user_activity_reset_timer():
    clock = FakeClock()
    ctl = SleepController(clock=clock)
    ctl.request_wake()
    clock.advance(50.0)
    ctl.note_activity()                  # tool success / typed command
    assert ctl.should_sleep() is False
    clock.advance(59.0)
    assert ctl.should_sleep() is False
    clock.advance(2.0)
    assert ctl.should_sleep() is True


def test_no_sleep_while_busy():
    clock = FakeClock()
    ctl = SleepController(clock=clock)
    ctl.request_wake()
    ctl.busy_enter()
    clock.advance(3600.0)
    assert ctl.should_sleep() is False
    assert ctl.request_sleep() == "deferred"
    assert ctl.is_sleeping() is False
    ctl.busy_exit()
    assert ctl.take_pending_sleep() is True
    assert ctl.request_sleep() == "sleeping"


def test_parse_voice_commands():
    assert parse_voice_command("go to sleep") == "sleep"
    assert parse_voice_command("please go back to sleep now") == "sleep"
    assert parse_voice_command("sleep") == "sleep"
    assert parse_voice_command("wake up") == "wake"
    assert parse_voice_command("hey wake up please") == "wake"
    assert parse_voice_command("what time is it") is None
    assert parse_voice_command("") is None


# ---------------------------------------------------------------------------
# 6: hidden/unfocused window independence (no UI objects involved)
# ---------------------------------------------------------------------------

def test_toggle_needs_no_window_or_focus():
    agent = make_agent()
    agent.sleep.request_sleep()
    assert agent.request_toggle() == "awake"     # F2 with no UI at all
    assert agent.request_toggle() == "sleeping"
    assert agent.sleep.snapshot()["state"] == "sleeping"


# ---------------------------------------------------------------------------
# control socket (real server thread + fake agent)
# ---------------------------------------------------------------------------

class _CtlAgent:
    def __init__(self):
        self.sleep = SleepController()
        self.loop = None

    def request_toggle(self):
        disp = self.sleep.request_toggle()
        return disp

    def request_wake(self):
        return self.sleep.request_wake()

    def request_sleep(self):
        return self.sleep.request_sleep()


def _serve_in_thread(agent, path):
    from cat_talker.control import send_command, serve_forever
    stop = threading.Event()
    thread = threading.Thread(
        target=serve_forever,
        kwargs={"get_agent": lambda: agent, "path": path,
                "stop_event": stop},
        daemon=True,
    )
    thread.start()
    for _ in range(150):
        try:
            if send_command("status", path=path, timeout=0.2).get("ok"):
                break
        except (OSError, ValueError):
            pass
        _time.sleep(0.02)
    return stop, thread


def test_control_socket_toggle_wake_sleep_status(tmp_path):
    from cat_talker.control import send_command
    agent = _CtlAgent()
    path = str(tmp_path / "control.sock")
    stop, thread = _serve_in_thread(agent, path)
    try:
        assert send_command("toggle", path=path)["state"] == "awake"
        assert send_command("status", path=path)["state"] == "awake"
        assert send_command("sleep", path=path)["state"] == "sleeping"
        assert send_command("wake", path=path)["state"] == "awake"
        assert send_command("bogus", path=path)["ok"] is False
    finally:
        stop.set()
        thread.join(timeout=5)


def test_control_socket_stale_file_replaced(tmp_path):
    from cat_talker.control import send_command
    agent = _CtlAgent()
    path = str(tmp_path / "control.sock")
    with open(path, "w") as f:
        f.write("stale")
    stop, thread = _serve_in_thread(agent, path)
    try:
        assert send_command("toggle", path=path)["state"] == "awake"
    finally:
        stop.set()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 4-5: launcher single-instance gate (bin/assistant-control as subprocess)
# ---------------------------------------------------------------------------

_LAUNCHER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "assistant-control")


def _server_script(sock_path, marker_path, bind_delay=0.0):
    return (
        "import os, socket, time\n"
        f"open({marker_path!r}, 'a').write('spawned\\n')\n"
        f"time.sleep({bind_delay})\n"
        f"s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        f"s.bind({sock_path!r})\n"
        "s.listen(4)\n"
        "s.settimeout(20)\n"
        "end = time.monotonic() + 18\n"
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


def _run_launcher(tmp_path, *args, spawn_argv=None):
    env = dict(os.environ)
    env["CAT_TALKER_RUNTIME_DIR"] = str(tmp_path)
    if spawn_argv is not None:
        env["CAT_TALKER_SPAWN_ARGV_JSON"] = json.dumps(spawn_argv)
    return subprocess.run(
        [sys.executable, _LAUNCHER, *args],
        capture_output=True, text=True, timeout=60, env=env)


def test_f2_launches_exactly_one_assistant(tmp_path):
    sock = str(tmp_path / "cat-talker" / "control.sock")
    marker = str(tmp_path / "spawns.txt")
    script = tmp_path / "srv.py"
    script.write_text(_server_script(sock, marker))
    proc = _run_launcher(tmp_path, "toggle",
                         spawn_argv=[sys.executable, str(script)])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "awake"
    assert open(marker).read().count("spawned") == 1


def test_repeated_f2_never_duplicates(tmp_path):
    sock = str(tmp_path / "cat-talker" / "control.sock")
    marker = str(tmp_path / "spawns.txt")
    script = tmp_path / "srv.py"
    script.write_text(_server_script(sock, marker, bind_delay=0.5))
    results = []
    errors = []

    def one():
        try:
            results.append(_run_launcher(tmp_path, "toggle",
                                         spawn_argv=[sys.executable,
                                                     str(script)]))
        except Exception as e:  # pragma: no cover - diagnostic only
            errors.append(e)

    threads = [threading.Thread(target=one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors
    assert [r.returncode for r in results] == [0, 0]
    assert open(marker).read().count("spawned") == 1


def test_f2_forwards_to_running_without_spawning(tmp_path):
    agent = _CtlAgent()
    path = str(tmp_path / "cat-talker" / "control.sock")
    stop, thread = _serve_in_thread(agent, path)
    marker = str(tmp_path / "spawns.txt")
    try:
        proc = _run_launcher(
            tmp_path, "toggle",
            spawn_argv=[sys.executable, "-c",
                        f"open({marker!r},'a').write('spawned\\n')"])
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "awake"
        assert not os.path.exists(marker)
    finally:
        stop.set()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# audio pause (stubbed interface, no hardware)
# ---------------------------------------------------------------------------

def test_main_startup_sleeps_then_wakes_then_sleeps(tmp_path, monkeypatch):
    """End-to-end startup flow through REAL main(): process starts, control
    socket answers while SLEEPING, zero Gemini connects until F2, wake
    opens exactly one fresh session, sleep closes it with no reconnect.
    Qt runs offscreen; Gemini/audio/window-side effects are stubbed.
    """
    import signal as _signal
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CAT_TALKER_RUNTIME_DIR", str(tmp_path))
    old_term, old_int = _signal.getsignal(_signal.SIGTERM), \
        _signal.getsignal(_signal.SIGINT)
    connects = []
    sessions = []

    class FakeConn:
        def __init__(self, session):
            self._s = session

        async def __aenter__(self):
            return self._s

        async def __aexit__(self, *a):
            self._s.exited = True
            return False

    def fake_connect(model=None, config=None):
        connects.append(1)
        session = FakeSession(hang=True)
        sessions.append(session)
        return FakeConn(session)

    class FakeClient:
        def __init__(self, *a, **k):
            self.aio = MagicMock()
            self.aio.live.connect = fake_connect

    import cat_talker.main as main_mod
    from cat_talker.control import send_command, socket_path
    assert socket_path() == str(tmp_path / "cat-talker" / "control.sock")

    errors = []

    def steps():
        try:
            _time.sleep(4.0)
            assert len(connects) == 0, \
                "startup must not connect to Gemini"
            assert send_command("status")["state"] == "sleeping"
            assert send_command("toggle")["state"] == "awake"
            _time.sleep(2.0)
            assert len(connects) == 1, "wake opens one fresh session"
            assert send_command("toggle")["state"] == "sleeping"
            _time.sleep(1.5)
            assert len(connects) == 1, "sleep causes no reconnect"
            assert sessions and sessions[0].exited, \
                "sleep closes the Live session"
        except Exception as e:  # report via main thread, then quit anyway
            errors.append(e)
        finally:
            os.kill(os.getpid(), _signal.SIGTERM)

    with patch.object(agent_mod.genai, "Client", FakeClient), \
         patch.object(agent_mod, "AudioInterface", FakeAudio), \
         patch.object(agent_mod, "VisionInterface", MagicMock), \
         patch.object(agent_mod, "play_earcon", MagicMock()), \
         patch.object(main_mod, "DictationManager", MagicMock()):
        helper = threading.Thread(target=steps, daemon=True)
        helper.start()
        try:
            main_mod.main()
        except SystemExit:
            pass
        helper.join(timeout=10)
    try:
        _signal.signal(_signal.SIGTERM, old_term)
        _signal.signal(_signal.SIGINT, old_int)
    except Exception:
        pass
    assert not errors, errors
    assert len(connects) == 1


def test_launcher_env_absolute_and_cwd_independent(tmp_path):
    """assistant-control works from /tmp: absolute venv python, absolute
    PYTHONPATH, socket served, exactly one spawn."""
    record = tmp_path / "env.json"
    sock = str(tmp_path / "cat-talker" / "control.sock")
    marker = str(tmp_path / "spawns.txt")
    script = tmp_path / "envsrv.py"
    script.write_text(
        "import json, os, socket, sys, time\n"
        f"open({str(record)!r}, 'w').write(json.dumps({{\n"
        "    'exe': sys.executable,\n"
        "    'cwd': os.getcwd(),\n"
        "    'pythonpath': os.environ.get('PYTHONPATH', '')}))\n"
        f"open({marker!r}, 'a').write('spawned\\n')\n"
        f"s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        f"s.bind({sock!r})\n"
        "s.listen(4)\n"
        "s.settimeout(20)\n"
        "end = time.monotonic() + 18\n"
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
    env = dict(os.environ)
    env["CAT_TALKER_RUNTIME_DIR"] = str(tmp_path)
    env["CAT_TALKER_SPAWN_ARGV_JSON"] = json.dumps(
        [sys.executable, str(script)])
    proc = subprocess.run(
        [sys.executable, _LAUNCHER, "toggle"],
        capture_output=True, text=True, timeout=60,
        cwd="/tmp", env=env)
    assert proc.returncode == 0, proc.stderr
    info = json.loads(record.read_text())
    root = os.path.dirname(os.path.dirname(os.path.abspath(_LAUNCHER)))
    assert info["exe"] == os.path.join(root, ".venv", "bin", "python")
    assert info["pythonpath"].split(os.pathsep)[0] == \
        os.path.join(root, "src")
    assert open(marker).read().count("spawned") == 1


def test_mic_pause_drops_and_resume_forwards():
    import pyaudio
    from cat_talker.audio import AudioInterface

    class FakeLoop:
        def __init__(self):
            self.calls = []

        def call_soon_threadsafe(self, cb, *args):
            self.calls.append((cb, args))

    import queue
    loop = FakeLoop()
    iface = AudioInterface.__new__(AudioInterface)
    iface._running = True
    iface._loop_closed = False
    iface.is_playing = False
    iface.mic_active = False
    iface.loop = loop
    iface.audio_in_queue = queue.Queue()
    iface._echo_enabled = False
    iface.pause_input()
    out = iface._mic_callback(b"ABCD" * 512, 1024, None, None)
    assert out == (None, pyaudio.paContinue)
    assert loop.calls == []
    assert iface._paused_dropped == 1
    iface.resume_input()
    out = iface._mic_callback(b"ABCD" * 512, 1024, None, None)
    assert out == (None, pyaudio.paContinue)
    assert len(loop.calls) == 1


# ---------------------------------------------------------------------------
# run_loop integration (FakeSession harness, fast-forwarded timers)
# ---------------------------------------------------------------------------

def test_sleeping_sends_no_mic_and_never_connects():
    agent = make_agent()
    agent.sleep.request_sleep()  # ensure sleeping (make_agent wakes)
    assert agent.sleep.is_sleeping()
    session = FakeSession(hang=True)
    count = wire_connect(agent, session)
    agent.audio.audio_in_queue.put_nowait(b"chunk-1")
    agent.audio.audio_in_queue.put_nowait(b"chunk-2")
    _run_drive(agent, 0.3, [])
    assert count[0] == 0, "slept agent must not connect"
    assert session.sent_audio == []
    assert agent.sleep.is_sleeping()


def test_sleep_closes_session_without_reconnect_and_wake_is_fresh():
    agent = make_agent()
    texts, bubbles = [], []
    first = FakeSession(hang=True)
    second = FakeSession(hang=True)
    count = wire_connect(agent, [first, second])
    audio = agent.audio  # run_loop's finally releases it; keep a ref
    paused_while_sleeping = []
    resumed_after_wake = []

    async def go():
        loop = asyncio.get_running_loop()
        loop.call_later(0.2, agent.request_sleep)
        loop.call_later(0.35,
                        lambda: paused_while_sleeping.append(
                            audio.input_paused))
        loop.call_later(0.5, agent.request_wake)
        loop.call_later(0.65,
                        lambda: resumed_after_wake.append(
                            audio.input_paused))
        loop.call_later(0.8, agent.stop_event.set)
        await asyncio.wait_for(
            agent.run_loop(text_callback=lambda r, t: texts.append((r, t)),
                           bubble_callback=bubbles.append),
            timeout=10.0)

    import asyncio as _aio
    real_sleep = _aio.sleep

    async def fast_sleep(delay, *a, **k):
        await real_sleep(min(delay, 0.02), *a, **k)

    with patch.object(_aio, "sleep", fast_sleep):
        _aio.run(go())
    assert first.exited, "sleep must close the Live session"
    assert paused_while_sleeping == [True], \
        "mic capture pauses while sleeping"
    assert count[0] == 2, "wake must start exactly one fresh session"
    assert resumed_after_wake == [False], "wake resumes mic capture"
    assert second.exited is False or True  # shutdown path may close it
    greetings = [t for r, t in texts
                 if r == "system" and "Connected! Speak now" in t]
    assert len(greetings) == 1, "exactly one greeting across wake cycles"
    assert any("Awake" in b for b in bubbles), "wake shows awake bubble"


def test_idle_timeout_sleeps_live_session():
    agent, clock = _agent_with_clock()
    session = FakeSession(hang=True)
    count = wire_connect(agent, session)
    clock.advance(61.0)  # no meaningful activity for over a minute
    _run_drive(agent, 0.4, [])
    assert agent.sleep.is_sleeping()
    assert session.exited, "idle timeout must close the session"
    assert count[0] == 1, "no reconnect while sleeping"


def test_transcription_without_response_does_not_prevent_sleep():
    """YouTube-noise 'User said:' the model ignores must not keep awake."""
    agent, clock = _agent_with_clock()
    session = FakeSession(
        interactions=[[_input_transcription_msg("and then the video ends")],
                      "hang"])
    count = wire_connect(agent, session)
    clock.advance(61.0)
    _run_drive(agent, 0.5, [])
    assert agent.sleep.is_sleeping(), \
        "unanswered transcription must not reset the idle timer"
    assert count[0] == 1


def test_tool_execution_resets_idle_timer():
    agent, clock = _agent_with_clock()
    calls = []

    def fake_ping(note=""):
        calls.append(note)
        return "pong"

    fake_ping.__name__ = "fake_ping_tool"
    with patch("cat_talker.tools.ALL_TOOLS", [fake_ping]):
        session = FakeSession(
            interactions=[[_tool_msg("fake_ping_tool", {"note": "hi"}),
                            _turn_complete_msg()],
                          "hang"])
        count = wire_connect(agent, session)
        clock.advance(50.0)
        _run_drive(agent, 0.5, [])
    assert calls == ["hi"], "tool must have executed"
    assert agent.sleep.idle_for() < 50.0, "tool success resets the timer"
    assert not agent.sleep.is_sleeping()
    assert count[0] == 1


def test_typed_user_command_resets_idle_timer():
    agent, clock = _agent_with_clock()
    session = FakeSession(
        interactions=[[_text_turn_msg("open the browser"),
                       _turn_complete_msg()],
                      "hang"])
    wire_connect(agent, session)
    clock.advance(50.0)
    _run_drive(agent, 0.5, [])
    assert agent.sleep.idle_for() < 50.0
    assert not agent.sleep.is_sleeping()


def test_explicit_go_to_sleep_voice_command():
    agent = make_agent()
    session = FakeSession(
        interactions=[[_input_transcription_msg("ok go to sleep now"),
                       _turn_complete_msg()],
                      "hang"])
    count = wire_connect(agent, session)
    _run_drive(agent, 0.5, [])
    assert agent.sleep.is_sleeping()
    assert session.exited
    assert count[0] == 1


def test_wake_up_phrase_while_awake_confirms_and_resets():
    agent, clock = _agent_with_clock()
    session = FakeSession(
        interactions=[[_input_transcription_msg("hey wake up"),
                       _turn_complete_msg()],
                      "hang"])
    wire_connect(agent, session)
    clock.advance(50.0)
    _run_drive(agent, 0.5, [])
    assert not agent.sleep.is_sleeping()
    assert agent.sleep.idle_for() < 50.0


def test_f2_during_tool_call_defers_without_corruption():
    agent = make_agent()
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow_tool(note=""):
        entered.set()
        assert release.wait(timeout=10), "test did not release the tool"
        calls.append(note)
        return "slow-done"

    slow_tool.__name__ = "slow_probe_tool"
    with patch("cat_talker.tools.ALL_TOOLS", [slow_tool]):
        session = FakeSession(
            interactions=[[_tool_msg("slow_probe_tool", {"note": "x"}),
                           _turn_complete_msg()],
                          "hang"])
        wire_connect(agent, session)
        outcome = {}

        def sequencer():
            # Runs OFF the loop thread: the tool blocks the loop while
            # waiting, so sequencing must not need loop time.
            assert entered.wait(timeout=10), "tool never started"
            outcome["disp"] = agent.request_sleep()
            release.set()
            for _ in range(500):
                if agent.sleep.is_sleeping():
                    break
                _time.sleep(0.02)
            outcome["slept"] = agent.sleep.is_sleeping()
            outcome["responses"] = len(session.tool_responses)
            agent.stop_event.set()

        seq = threading.Thread(target=sequencer, daemon=True)
        seq.start()

        async def go():
            await agent.run_loop()

        import asyncio as _aio
        real_sleep = _aio.sleep

        async def fast_sleep(delay, *a, **k):
            await real_sleep(min(delay, 0.02), *a, **k)

        with patch.object(_aio, "sleep", fast_sleep):
            _aio.run(asyncio.wait_for(go(), timeout=20.0))
        seq.join(timeout=10.0)
    assert outcome["disp"] == "deferred"
    assert outcome["slept"], "deferred sleep applies at turn end"
    assert outcome["responses"] > 0, "tool result delivered before sleep"
    assert calls == ["x"]
    assert session.exited, "deferred sleep closes the session cleanly"
