"""Assistant sleep/wake state machine and idle policy.

Pure logic, no I/O: the clock is injectable so tests never wait real
time. The agent owns one SleepController and calls into it from session
events; the control socket only flips it via thread-safe methods.

Rules enforced here:
  - Startup state is SLEEPING.
  - Only *accepted* interactions reset the idle timer: completed model
    output, executed tools, deliberate text turns, explicit wake/sleep.
    Raw voice transcriptions (which include false "User said:" from
    YouTube/system audio) merely mark a *pending* voice turn and never
    reset the timer until the model actually engages with it.
  - F2 toggle defers while busy (tool call or response in flight).
"""

import re
import time as _time

IDLE_TIMEOUT_S = 60.0

_SLEEP_RE = re.compile(r"\b(go to sleep|go back to sleep|sleep now)\b|\bsleep\b")
_WAKE_RE = re.compile(r"\bwake up\b")


def parse_voice_command(text: str):
    """Map a user utterance to 'sleep', 'wake', or None.

    Pure function so voice-phrase behavior is unit-testable. 'sleep' is
    matched conservatively (standalone word or explicit phrases) so
    ordinary sentences containing 'sleep' as a substring still match only
    when the word is present - callers decide context.
    """
    if not text:
        return None
    lowered = text.lower()
    if _SLEEP_RE.search(lowered):
        return "sleep"
    if _WAKE_RE.search(lowered):
        return "wake"
    return None


class SleepController:
    """Sleep/wake state with meaningful-activity idle tracking."""

    def __init__(self, clock=None, idle_timeout: float = IDLE_TIMEOUT_S):
        self._clock = clock or _time.monotonic
        self._idle_timeout = idle_timeout
        self._sleeping = True  # startup contract: SLEEPING
        self._busy = 0  # tool calls / responses in flight
        self._pending_sleep = False  # F2 arrived while busy
        self._last_activity = self._clock()
        self._pending_voice = False  # unconfirmed transcription heard

    # -- state ---------------------------------------------------------
    def is_sleeping(self) -> bool:
        return self._sleeping

    def is_busy(self) -> bool:
        return self._busy > 0

    def take_pending_sleep(self) -> bool:
        pending, self._pending_sleep = self._pending_sleep, False
        return pending

    # -- transitions (thread-safe: plain flag writes) -------------------
    def request_sleep(self) -> str:
        """Ask to sleep now; defers while busy. Returns disposition."""
        if self._sleeping:
            return "already"
        if self.is_busy():
            self._pending_sleep = True
            return "deferred"
        self._sleeping = True
        self._pending_sleep = False
        self._pending_voice = False
        self._last_activity = self._clock()
        return "sleeping"

    def request_wake(self) -> str:
        """Wake (or confirm awake). Explicit wake is meaningful activity."""
        self._last_activity = self._clock()
        self._pending_sleep = False
        if not self._sleeping:
            return "already"
        self._sleeping = False
        self._pending_voice = False
        return "awake"

    def request_toggle(self) -> str:
        if self._sleeping:
            return self.request_wake()
        return self.request_sleep()

    # -- busy tracking ---------------------------------------------------
    def busy_enter(self):
        self._busy += 1

    def busy_exit(self):
        if self._busy > 0:
            self._busy -= 1

    # -- meaningful activity ---------------------------------------------
    def note_activity(self):
        """An accepted interaction completed: reset the idle timer."""
        self._last_activity = self._clock()
        self._pending_voice = False

    def note_voice_heard(self):
        """A raw transcription arrived. NOT activity by itself: YouTube /
        system-audio false positives must never keep the assistant awake.
        Only a subsequent model engagement confirms it (see
        confirm_voice_if_engaged)."""
        self._pending_voice = True

    def confirm_voice_if_engaged(self, model_engaged: bool):
        """Turn end: a pending voice turn counts iff the model engaged."""
        if self._pending_voice and model_engaged:
            self.note_activity()
        self._pending_voice = False

    # -- idle policy -------------------------------------------------------
    def idle_for(self, now=None) -> float:
        now = self._clock() if now is None else now
        return now - self._last_activity

    def should_sleep(self, now=None) -> bool:
        """True only when awake, idle past timeout, and not busy."""
        if self._sleeping or self.is_busy():
            return False
        return self.idle_for(now) >= self._idle_timeout

    def snapshot(self) -> dict:
        return {
            "state": "sleeping" if self._sleeping else "awake",
            "busy": self._busy,
            "pending_sleep": self._pending_sleep,
            "idle_for": round(self.idle_for(), 1),
        }
