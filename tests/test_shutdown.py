"""Focused shutdown tests: stop flag, task cancellation, clean thread exit.

No Qt is imported here. These cover the agent-side plumbing that the
SIGINT/SIGTERM handler and Qt aboutToQuit rely on (verified live separately):
request_stop() from another thread, cooperative exit, and cancellation of a
run_loop that never returns on its own.
"""

import asyncio
import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cat_talker.agent as agent_mod
from cat_talker.agent import GeminiDesktopAgent, start_agent_in_thread


def _make_agent():
    with patch.object(agent_mod.genai, "Client"):
        return GeminiDesktopAgent()


def _wait_for(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.01)
    return pred()


def test_request_stop_sets_flag_and_cancels_task():
    """request_stop() from a foreign thread sets stop_event and cancels a
    run_loop task stuck in a blocking await (e.g. a reconnect backoff)."""
    agent = _make_agent()
    loop = asyncio.new_event_loop()
    started = threading.Event()

    async def stuck():
        started.set()
        await asyncio.Event().wait()  # never returns on its own

    def run():
        asyncio.set_event_loop(loop)
        agent.loop = loop
        agent._run_task = loop.create_task(stuck())
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert started.wait(timeout=10), "loop task never started"
        agent.request_stop()  # foreign thread, like the signal handler
        assert agent.stop_event.is_set()
        task = agent._run_task
        assert task is not None
        assert _wait_for(lambda: task.done()), "task was not cancelled"
        assert task.cancelled()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        loop.close()
    # Second call must be harmless (handler reentrancy / aboutToQuit).
    agent.request_stop()


def test_start_agent_in_thread_cooperative_exit():
    """A run_loop that honors stop_event exits; the thread joins and the
    agent is left stopped. The fake tolerates cancellation landing before
    its first step (request_stop sets the flag AND cancels), so the test
    is deterministic under any interleaving."""
    finished = []
    started = threading.Event()

    async def fake_run_loop(self, *args, **kwargs):
        started.set()
        try:
            await self.stop_event.wait()
        except asyncio.CancelledError:
            pass
        finished.append(True)

    with patch.object(GeminiDesktopAgent, "run_loop", fake_run_loop), \
         patch.object(agent_mod.genai, "Client"):
        ref = []
        thread = threading.Thread(
            target=start_agent_in_thread,
            args=(None, None, None, None, None, None, ref),
            daemon=True,
        )
        thread.start()
        try:
            assert _wait_for(lambda: bool(ref) and started.is_set()), \
                "agent thread never started"
            ref[0].request_stop()
            thread.join(timeout=15)
            assert not thread.is_alive(), "agent thread did not stop"
            assert finished == [True]
            assert ref[0].stop_event.is_set()
        finally:
            if thread.is_alive():
                ref[0].request_stop()
                thread.join(timeout=15)


def test_start_agent_in_thread_cancels_uncooperative_loop():
    """A run_loop that ignores stop_event is cancelled: the thread must
    still exit promptly with the loop closed (no pending tasks)."""
    async def fake_run_loop(self, *args, **kwargs):
        await asyncio.Event().wait()  # never returns on its own

    with patch.object(GeminiDesktopAgent, "run_loop", fake_run_loop), \
         patch.object(agent_mod.genai, "Client"):
        ref = []
        thread = threading.Thread(
            target=start_agent_in_thread,
            args=(None, None, None, None, None, None, ref),
            daemon=True,
        )
        thread.start()
        try:
            assert _wait_for(lambda: bool(ref) and thread.is_alive()), \
                "agent thread never started"
            ref[0].request_stop()
            thread.join(timeout=15)
            assert not thread.is_alive(), \
                "agent thread stuck despite cancellation"
        finally:
            if thread.is_alive():
                ref[0].request_stop()
                thread.join(timeout=15)
