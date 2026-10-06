"""Deterministic local reminders (standard library only).

Architecture:
    Gemini -> create/list/cancel_reminder [tools.py] -> this module
    -> validated JSON store (~/.config/cat-talker/reminders.json,
    same directory convention as config.py/memory.py; no database)
    -> single daemon scheduler thread -> existing send_notification.

This is NOT an autonomous agent: reminders fire only for schedules
the user explicitly requested, via the exact message given. Nothing
is inferred, and firing only ever raises a desktop notification -
never clicks, keys, shell, coding, or web actions.

Threading: exactly one scheduler thread per process (guarded start),
stopped from main.request_agent_stop so F3/signal/Qt-quit paths all
unwind it. The worker never touches Qt widgets; notify-send runs in
a subprocess, which is thread-safe here.
"""

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone, tzinfo

from cat_talker.logging_config import get_logger

logger = get_logger("cat_talker.reminders")

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - stdlib on all supported Pythons
    ZoneInfo = None

DEFAULT_REMINDERS_PATH = os.path.expanduser(
    "~/.config/cat-talker/reminders.json")

# Closed recurrence set: validated explicitly, extended by adding one
# entry here (plus its next-occurrence rule below).
KINDS = ("once", "daily")

# Conservative bounds: reminders are short human strings with an
# explicit schedule, not documents or programs.
MAX_MESSAGE_LEN = 500
MAX_REMINDERS = 200

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Scheduler loop never sleeps past this: keeps shutdown prompt and
# bounds lateness if the store changes under a running loop.
MAX_SLEEP_S = 60.0
POLL_INTERVAL_S = 30.0


class ReminderError(ValueError):
    """Invalid reminder input or store failure (tools turn these into
    honest 'Error: ...' strings, never tracebacks to the user)."""


def _local_zone_name() -> str:
    """Best-effort IANA name of the system local zone (for the default
    when the caller gives no timezone). UTC when undetectable."""
    try:
        target = os.readlink("/etc/localtime")
        marker = "/zoneinfo/"
        if marker in target:
            return target.split(marker, 1)[1]
    except OSError:
        pass
    return "UTC"


def resolve_timezone(name) -> tzinfo:
    """Validate an IANA timezone name. Empty means system local zone.
    Raises ReminderError for unknown zones."""
    if ZoneInfo is None:  # pragma: no cover
        raise ReminderError("Reminder error: timezone database unavailable.")
    cleaned = (name or "").strip()
    if not cleaned:
        cleaned = _local_zone_name()
    try:
        return ZoneInfo(cleaned)
    except Exception:
        raise ReminderError(
            f"Reminder error: unknown timezone '{name}'. "
            "Use an IANA name like 'Asia/Kolkata'.")


def zone_name(tz) -> str:
    """Stable serializable name for a resolved zone."""
    key = getattr(tz, "key", None)
    if key:
        return key
    return str(tz)


def normalize_message(message) -> str:
    if not isinstance(message, str):
        raise ReminderError("Reminder error: message must be text.")
    cleaned = message.strip()
    if not cleaned:
        raise ReminderError("Reminder error: message must not be empty.")
    if len(cleaned) > MAX_MESSAGE_LEN:
        raise ReminderError(
            f"Reminder error: message too long "
            f"(max {MAX_MESSAGE_LEN} characters).")
    return cleaned


def normalize_kind(kind) -> str:
    cleaned = (kind or "").strip().lower()
    if cleaned not in KINDS:
        raise ReminderError(
            f"Reminder error: unknown schedule type '{kind}'. "
            f"Allowed: {', '.join(KINDS)}.")
    return cleaned


def _ensure_aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ReminderError(
            f"Reminder error: {field} must be a timezone-aware datetime "
            "(include the UTC offset, e.g. 2026-10-07T09:00:00+05:30).")
    return value


def parse_once_at(at: str, tz, now: datetime) -> datetime:
    """Validate an exact one-time fire time. Must be aware and future."""
    if not isinstance(at, str) or not at.strip():
        raise ReminderError(
            "Reminder error: one-time reminders need 'at' "
            "(timezone-aware datetime).")
    try:
        value = datetime.fromisoformat(at.strip())
    except ValueError:
        raise ReminderError(
            f"Reminder error: could not parse datetime '{at}'. "
            "Use ISO format with offset, e.g. 2026-10-07T09:00:00+05:30.")
    _ensure_aware(value, "'at'")
    if value <= now:
        raise ReminderError(
            "Reminder error: one-time reminder time is in the past.")
    return value


def parse_daily_time(t: str) -> tuple:
    """Validate an HH:MM 24-hour local time. Returns (hour, minute)."""
    cleaned = (t or "").strip()
    match = TIME_RE.match(cleaned)
    if not match:
        raise ReminderError(
            f"Reminder error: could not parse time '{t}'. "
            "Daily reminders need 'time' as HH:MM (24-hour).")
    return int(match.group(1)), int(match.group(2))


def next_daily_occurrence(hour: int, minute: int, tz, now: datetime) -> datetime:
    """Next strictly-future occurrence of HH:MM in tz."""
    local_now = now.astimezone(tz)
    candidate = local_now.replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate = candidate + timedelta(days=1)
    return candidate


def _validate_record(raw) -> dict | None:
    """Validate one persisted record. Returns the cleaned record or
    None when it must be skipped (never raises for bad data)."""
    if not isinstance(raw, dict):
        return None
    try:
        rid = raw.get("id")
        if not isinstance(rid, str) or not rid.strip():
            return None
        message = normalize_message(raw.get("message", ""))
        kind = normalize_kind(raw.get("kind", ""))
        tz = resolve_timezone(raw.get("timezone", ""))
        due = datetime.fromisoformat(raw.get("next_due", ""))
        _ensure_aware(due, "'next_due'")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            return None
        created = raw.get("created_at", "") or ""
        if created:
            datetime.fromisoformat(created)
        return {
            "id": rid.strip(),
            "message": message,
            "kind": kind,
            "timezone": zone_name(tz),
            "next_due": due.isoformat(),
            "enabled": enabled,
            "created_at": created,
        }
    except (ValueError, TypeError):
        return None


class ReminderStore:
    """Validated JSON reminder store with atomic writes.

    Missing file -> empty store. Malformed file or invalid entries ->
    logged and skipped, never a crash, never a partial in-memory state.
    Every mutation persists via tmp-file + os.replace, so a crash can
    never leave a half-written reminders file behind.
    """

    def __init__(self, path=None):
        self.path = path or DEFAULT_REMINDERS_PATH
        self._lock = threading.Lock()
        self._reminders: list = []
        self.reload()

    def reload(self) -> int:
        """Reload from disk, dropping anything invalid. Returns the
        count of valid reminders loaded."""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            raw = []
        except (OSError, ValueError) as e:
            logger.warning(f"reminders: ignoring unreadable store "
                           f"{self.path}: {e}")
            raw = []
        if not isinstance(raw, list):
            logger.warning(f"reminders: store {self.path} is not a list; "
                           "starting empty")
            raw = []
        valid = []
        for entry in raw:
            cleaned = _validate_record(entry)
            if cleaned is not None:
                valid.append(cleaned)
        if len(valid) != len(raw):
            logger.warning(f"reminders: skipped {len(raw) - len(valid)} "
                           "invalid stored reminder(s)")
        with self._lock:
            self._reminders = valid
        return len(valid)

    def _persist_locked(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self.path}.tmp-{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._reminders, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.path)

    def create(self, message: str, kind: str = "once", at: str = "",
               time_str: str = "", timezone_str: str = "",
               now: datetime | None = None) -> dict:
        """Validate and persist a new reminder. Returns its record.
        Raises ReminderError on any invalid input."""
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cleaned_message = normalize_message(message)
        cleaned_kind = normalize_kind(kind)
        tz = resolve_timezone(timezone_str)
        if cleaned_kind == "once":
            due = parse_once_at(at, tz, current)
        else:
            hour, minute = parse_daily_time(time_str)
            due = next_daily_occurrence(hour, minute, tz, current)
        record = {
            "id": uuid.uuid4().hex[:12],
            "message": cleaned_message,
            "kind": cleaned_kind,
            "timezone": zone_name(tz),
            "next_due": due.isoformat(),
            "enabled": True,
            "created_at": current.isoformat(),
        }
        with self._lock:
            if len(self._reminders) >= MAX_REMINDERS:
                raise ReminderError(
                    f"Reminder error: too many reminders "
                    f"(max {MAX_REMINDERS}); cancel one first.")
            self._reminders.append(record)
            self._persist_locked()
            return dict(record)

    def list(self, include_disabled: bool = False) -> list:
        with self._lock:
            records = [dict(r) for r in self._reminders
                       if include_disabled or r["enabled"]]
        records.sort(key=lambda r: r["next_due"])
        return records

    def get(self, reminder_id: str) -> dict | None:
        cleaned = (reminder_id or "").strip()
        with self._lock:
            for record in self._reminders:
                if record["id"] == cleaned:
                    return dict(record)
        return None

    def cancel(self, reminder_id: str) -> bool:
        """Disable a reminder (persisted: it never fires after this,
        even across restarts). True when found, False when unknown."""
        cleaned = (reminder_id or "").strip()
        with self._lock:
            for record in self._reminders:
                if record["id"] == cleaned:
                    record["enabled"] = False
                    self._persist_locked()
                    return True
        return False

    def mark_fired(self, reminder_id: str, now: datetime) -> dict | None:
        """Advance a fired reminder: one-time becomes disabled, daily
        moves to its next occurrence. Returns the updated record."""
        cleaned = (reminder_id or "").strip()
        with self._lock:
            for record in self._reminders:
                if record["id"] == cleaned and record["enabled"]:
                    if record["kind"] == "once":
                        record["enabled"] = False
                    else:
                        tz = resolve_timezone(record["timezone"])
                        due = datetime.fromisoformat(record["next_due"])
                        nxt = due.astimezone(tz) + timedelta(seconds=1)
                        hour, minute = due.astimezone(tz).hour, \
                            due.astimezone(tz).minute
                        advanced = next_daily_occurrence(
                            hour, minute, tz, nxt)
                        record["next_due"] = advanced.isoformat()
                    self._persist_locked()
                    return dict(record)
        return None


def _default_notify(message: str):
    """Fire through the existing send_notification capability (lazy
    import: tools.py imports this module for the tool functions)."""
    from cat_talker import tools as tools_mod
    return tools_mod.send_notification("Reminder", message)


class ReminderScheduler:
    """Single daemon-thread scheduler driving one ReminderStore.

    now_fn/notify_fn are injectable so tests drive a controllable
    clock without sleeping for real time. poll_once() performs one
    deterministic due-check and is also what the worker thread calls.
    """

    def __init__(self, store=None, now_fn=None, notify_fn=None,
                 poll_interval: float = POLL_INTERVAL_S):
        self.store = store or ReminderStore()
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._notify_fn = notify_fn or _default_notify
        self._poll_interval = max(1.0, float(poll_interval))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wakeup_event = threading.Event()

    def _now(self) -> datetime:
        current = self._now_fn()
        if not isinstance(current, datetime):
            raise ReminderError("Reminder error: clock must return datetime.")
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current

    def start(self) -> "ReminderScheduler":
        """Idempotent start: second and later calls return the same
        running scheduler without spawning another worker."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop_event.clear()
            self._wakeup_event.clear()
            self._thread = threading.Thread(
                target=self._run, name="reminder-scheduler", daemon=True)
            self._thread.start()
            return self

    def stop(self, timeout: float = 5.0):
        """Signal the worker to exit and wait for it (clean shutdown)."""
        with self._lock:
            thread = self._thread
        self._stop_event.set()
        self._wakeup_event.set()
        if thread is not None:
            thread.join(timeout=timeout)

    @property
    def alive(self) -> bool:
        with self._lock:
            thread = self._thread
        return thread is not None and thread.is_alive()

    def poll_once(self, now: datetime | None = None) -> int:
        """Fire every enabled reminder due at `now`. Returns the count
        fired. Each due reminder notifies exactly once per call."""
        current = now or self._now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        due_now = [r for r in self.store.list()
                   if datetime.fromisoformat(r["next_due"]) <= current]
        fired = 0
        for record in due_now:
            try:
                self._notify_fn(record["message"])
            except Exception as e:
                logger.warning(f"reminders: notify failed for "
                               f"{record['id']}: {e}")
                continue
            self.store.mark_fired(record["id"], current)
            fired += 1
        return fired

    def _next_wait(self, now: datetime) -> float:
        soonest = None
        for record in self.store.list():
            due = datetime.fromisoformat(record["next_due"])
            delta = (due - now).total_seconds()
            if delta < 0:
                delta = 0.0
            if soonest is None or delta < soonest:
                soonest = delta
        if soonest is None:
            return min(self._poll_interval, MAX_SLEEP_S)
        return min(max(0.0, soonest), MAX_SLEEP_S)

    def _run(self):
        while not self._stop_event.is_set():
            try:
                wait = self._next_wait(self._now())
            except Exception as e:
                logger.warning(f"reminders: scheduler tick failed: {e}")
                wait = self._poll_interval
            self._wakeup_event.wait(timeout=wait)
            self._wakeup_event.clear()
            if self._stop_event.is_set():
                break
            try:
                self.poll_once()
            except Exception as e:
                logger.warning(f"reminders: scheduler poll failed: {e}")

    def wake(self):
        """Recheck the store promptly (called after mutations)."""
        self._wakeup_event.set()


_MANAGER_LOCK = threading.Lock()
_SCHEDULER: ReminderScheduler | None = None


def get_scheduler() -> ReminderScheduler:
    """Process-wide scheduler: created and started exactly once."""
    with _MANAGER_LOCK:
        global _SCHEDULER
        if _SCHEDULER is None or not _SCHEDULER.alive:
            _SCHEDULER = ReminderScheduler()
            _SCHEDULER.store.reload()
            _SCHEDULER.start()
        return _SCHEDULER


def start_reminder_scheduler() -> ReminderScheduler:
    """Start the process-wide scheduler (idempotent). Called once
    from main() at startup; tools use get_scheduler() as fallback."""
    return get_scheduler()


def stop_reminder_scheduler(timeout: float = 5.0):
    """Stop the process-wide scheduler (called from the existing
    request_agent_stop path so every shutdown unwinds it)."""
    with _MANAGER_LOCK:
        scheduler = _SCHEDULER
    if scheduler is not None:
        scheduler.stop(timeout=timeout)


def _reset_scheduler_for_tests():
    """Stop and drop the process-wide scheduler (tests only)."""
    with _MANAGER_LOCK:
        global _SCHEDULER
        scheduler = _SCHEDULER
        _SCHEDULER = None
    if scheduler is not None:
        scheduler.stop(timeout=5.0)
