"""Focused tests for deterministic local reminders.

Architecture under test:
    tools.create/list/cancel_reminder -> reminders.ReminderStore
    (validated JSON, atomic writes) -> reminders.ReminderScheduler
    (one daemon thread) -> send_notification path.

A controllable clock (mutable now + poll_once) drives all timing:
no test sleeps for real time. The real notify-send path is replaced
by a recording fake; one test verifies the wiring into the existing
tools.send_notification capability via monkeypatch.
"""

import json
import os
import sys
import threading
import time as _time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import datetime, timedelta, timezone  # noqa: E402

from zoneinfo import ZoneInfo  # noqa: E402

from cat_talker import reminders as reminders_mod  # noqa: E402
from cat_talker.reminders import (  # noqa: E402
    ReminderError,
    ReminderScheduler,
    ReminderStore,
)

KOLKATA = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 1, 1, 8, 0, tzinfo=KOLKATA)


@pytest.fixture
def clock():
    """Controllable 'now': mutate now[0] to travel in time."""
    return [T0]


@pytest.fixture
def store(tmp_path):
    # Store creation does not need the clock; each create() takes now.
    return ReminderStore(path=str(tmp_path / "reminders.json"))


@pytest.fixture
def notified():
    return []


@pytest.fixture
def scheduler(store, clock, notified):
    sched = ReminderScheduler(
        store=store,
        now_fn=lambda: clock[0],
        notify_fn=lambda msg: notified.append(msg),
        poll_interval=60.0,
    )
    yield sched
    sched.stop(timeout=5.0)


def _fire_at(store, clock, message, kind, **kwargs):
    return store.create(message, kind, now=clock[0], **kwargs)


# 1: create one-time reminder -------------------------------------------
def test_create_once_reminder(store, clock):
    record = _fire_at(store, clock, "Take medicine", "once",
                      at="2026-01-01T09:00:00+05:30",
                      timezone_str="Asia/Kolkata")
    assert record["id"]
    assert record["message"] == "Take medicine"
    assert record["kind"] == "once"
    assert record["timezone"] == "Asia/Kolkata"
    assert record["enabled"] is True
    assert record["next_due"] == "2026-01-01T09:00:00+05:30"
    on_disk = json.load(open(store.path, encoding="utf-8"))
    assert any(r["id"] == record["id"] for r in on_disk)


# 2: create daily reminder ----------------------------------------------
def test_create_daily_reminder(store, clock):
    # Later today -> today.
    morning = _fire_at(store, clock, "Stand up", "daily",
                       time_str="09:00", timezone_str="Asia/Kolkata")
    assert morning["next_due"] == "2026-01-01T09:00:00+05:30"
    # Already passed today -> tomorrow.
    evening = _fire_at(store, clock, "Early run", "daily",
                       time_str="07:00", timezone_str="Asia/Kolkata")
    assert evening["next_due"] == "2026-01-02T07:00:00+05:30"


# 3: list reminders ------------------------------------------------------
def test_list_reminders(store, clock):
    _fire_at(store, clock, "Later", "once",
             at="2026-01-01T12:00:00+05:30",
             timezone_str="Asia/Kolkata")
    _fire_at(store, clock, "Sooner", "once",
             at="2026-01-01T09:00:00+05:30",
             timezone_str="Asia/Kolkata")
    listed = store.list()
    assert [r["message"] for r in listed] == ["Sooner", "Later"]
    store.cancel(listed[0]["id"])
    assert [r["message"] for r in store.list()] == ["Later"]
    assert len(store.list(include_disabled=True)) == 2


# 4: cancel reminder -----------------------------------------------------
def test_cancel_reminder(store, clock):
    record = _fire_at(store, clock, "Call mom", "once",
                      at="2026-01-01T09:00:00+05:30",
                      timezone_str="Asia/Kolkata")
    assert store.cancel(record["id"]) is True
    assert store.get(record["id"])["enabled"] is False
    assert store.cancel(record["id"]) is True  # idempotent
    assert store.cancel("no-such-id") is False


# 5: persistence across reload/restart -----------------------------------
def test_persistence_across_reload(tmp_path, clock):
    path = str(tmp_path / "reminders.json")
    first = ReminderStore(path=path)
    record = first.create("Pay bills", "daily", time_str="18:00",
                          timezone_str="Asia/Kolkata", now=clock[0])
    second = ReminderStore(path=path)  # simulated restart
    loaded = second.get(record["id"])
    assert loaded is not None
    assert loaded["message"] == "Pay bills"
    assert loaded["kind"] == "daily"
    assert loaded["enabled"] is True


# 6: one-time reminder disables after firing -----------------------------
def test_once_disables_after_firing(scheduler, clock, notified):
    _fire_at(scheduler.store, clock, "One shot", "once",
            at="2026-01-01T09:00:00+05:30",
            timezone_str="Asia/Kolkata")
    assert scheduler.poll_once() == 0
    clock[0] = datetime(2026, 1, 1, 9, 0, tzinfo=KOLKATA)
    assert scheduler.poll_once() == 1
    assert notified == ["One shot"]
    remaining = scheduler.store.list()
    assert remaining == []
    assert scheduler.store.list(include_disabled=True)[0]["enabled"] is False


# 7: daily reminder advances to its next occurrence ----------------------
def test_daily_advances_after_firing(scheduler, clock, notified):
    record = _fire_at(scheduler.store, clock, "Water plants", "daily",
                      time_str="09:00", timezone_str="Asia/Kolkata")
    clock[0] = datetime(2026, 1, 1, 9, 0, tzinfo=KOLKATA)
    assert scheduler.poll_once() == 1
    assert notified == ["Water plants"]
    updated = scheduler.store.get(record["id"])
    assert updated["enabled"] is True
    assert updated["next_due"] == "2026-01-02T09:00:00+05:30"


# 8: invalid schedule/message is rejected ---------------------------------
def test_invalid_input_rejected(store, clock):
    with pytest.raises(ReminderError):
        _fire_at(store, clock, "   ", "once",
                 at="2026-01-01T09:00:00+05:30",
                 timezone_str="Asia/Kolkata")
    with pytest.raises(ReminderError):
        _fire_at(store, clock, "Hi", "weekly",
                 at="2026-01-01T09:00:00+05:30",
                 timezone_str="Asia/Kolkata")
    with pytest.raises(ReminderError):  # naive datetime
        _fire_at(store, clock, "Hi", "once", at="2026-01-01T09:00:00",
                 timezone_str="Asia/Kolkata")
    with pytest.raises(ReminderError):  # past one-time
        _fire_at(store, clock, "Hi", "once",
                 at="2026-01-01T07:00:00+05:30",
                 timezone_str="Asia/Kolkata")
    with pytest.raises(ReminderError):  # bad daily time
        _fire_at(store, clock, "Hi", "daily", time_str="25:00",
                 timezone_str="Asia/Kolkata")
    with pytest.raises(ReminderError):  # bad timezone
        _fire_at(store, clock, "Hi", "daily", time_str="09:00",
                 timezone_str="Mars/Olympus")
    with pytest.raises(ReminderError):  # missing schedule detail
        _fire_at(store, clock, "Hi", "daily",
                 timezone_str="Asia/Kolkata")
    assert store.list(include_disabled=True) == []


# 9: scheduler starts only once ------------------------------------------
def test_scheduler_starts_only_once(tmp_path, clock, notified):
    store = ReminderStore(path=str(tmp_path / "reminders.json"))
    sched = ReminderScheduler(store=store, now_fn=lambda: clock[0],
                              notify_fn=lambda msg: notified.append(msg))
    try:
        first = sched.start()
        second = sched.start()
        assert first is second
        workers = [t for t in threading.enumerate()
                   if t.name == "reminder-scheduler" and t.is_alive()]
        assert len(workers) == 1
    finally:
        sched.stop(timeout=5.0)


# 10: scheduler shuts down cleanly -----------------------------------------
def test_scheduler_shuts_down_cleanly(tmp_path, clock, notified):
    store = ReminderStore(path=str(tmp_path / "reminders.json"))
    sched = ReminderScheduler(store=store, now_fn=lambda: clock[0],
                              notify_fn=lambda msg: notified.append(msg))
    sched.start()
    assert sched.alive is True
    sched.stop(timeout=5.0)
    assert sched.alive is False
    # Second stop is a safe no-op.
    sched.stop(timeout=5.0)
    assert sched.alive is False


# 11: cancelled reminder does not fire after reload -------------------------
def test_cancelled_does_not_fire_after_reload(tmp_path, clock, notified):
    path = str(tmp_path / "reminders.json")
    first = ReminderStore(path=path)
    record = first.create("Do not fire", "once",
                          at="2026-01-01T09:00:00+05:30",
                          timezone_str="Asia/Kolkata", now=clock[0])
    assert first.cancel(record["id"]) is True
    restarted = ReminderStore(path=path)  # simulated restart
    sched = ReminderScheduler(store=restarted, now_fn=lambda: clock[0],
                              notify_fn=lambda msg: notified.append(msg))
    try:
        clock[0] = datetime(2026, 1, 1, 10, 0, tzinfo=KOLKATA)
        assert sched.poll_once() == 0
        assert notified == []
    finally:
        sched.stop(timeout=5.0)


# 12: due reminders invoke notification exactly once -------------------------
def test_due_reminders_notify_exactly_once(scheduler, clock, notified):
    _fire_at(scheduler.store, clock, "First", "once",
            at="2026-01-01T09:00:00+05:30",
            timezone_str="Asia/Kolkata")
    _fire_at(scheduler.store, clock, "Second", "once",
            at="2026-01-01T09:00:00+05:30",
            timezone_str="Asia/Kolkata")
    clock[0] = datetime(2026, 1, 1, 9, 0, tzinfo=KOLKATA)
    assert scheduler.poll_once() == 2
    assert sorted(notified) == ["First", "Second"]
    # A second poll fires nothing new (once-reminders disabled).
    assert scheduler.poll_once() == 0
    assert sorted(notified) == ["First", "Second"]


# Tool surface: create/list/cancel through the real tool functions ---------
def test_tool_surface_end_to_end(tmp_path, clock, monkeypatch):
    from cat_talker import tools as tools_mod
    path = str(tmp_path / "reminders.json")
    store = ReminderStore(path=path)
    sched = ReminderScheduler(store=store, now_fn=lambda: clock[0],
                              notify_fn=lambda msg: None)
    monkeypatch.setattr(reminders_mod, "get_scheduler", lambda: sched)
    try:
        created = tools_mod.create_reminder(
            "Drink water", "daily", time="09:00",
            timezone_str="Asia/Kolkata")
        assert created.startswith("Reminder created (id ")
        listed = tools_mod.list_reminders()
        assert "Drink water" in listed
        rid = store.list()[0]["id"]
        assert tools_mod.cancel_reminder(rid) == f"Reminder {rid} cancelled."
        assert tools_mod.cancel_reminder("bogus") .startswith("Error:")
        assert tools_mod.create_reminder("", "once") .startswith("Error:")
    finally:
        sched.stop(timeout=5.0)


def test_malformed_store_is_tolerated(tmp_path):
    path = str(tmp_path / "reminders.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not valid json[")
    store = ReminderStore(path=path)
    assert store.list() == []
    with open(path, "w", encoding="utf-8") as f:
        json.dump([{"id": "x", "message": "", "kind": "nope"}], f)
    store.reload()
    assert store.list() == []


def test_firing_uses_existing_notification_path(tmp_path, clock, monkeypatch):
    """The scheduler's default notify_fn is tools.send_notification."""
    from cat_talker import tools as tools_mod
    calls = []
    monkeypatch.setattr(tools_mod, "send_notification",
                        lambda title, body: calls.append((title, body))
                        or "Notification sent.")
    store = ReminderStore(path=str(tmp_path / "reminders.json"))
    sched = ReminderScheduler(store=store, now_fn=lambda: clock[0])
    try:
        store.create("Wired", "once", at="2026-01-01T09:00:00+05:30",
                     timezone_str="Asia/Kolkata", now=clock[0])
        clock[0] = datetime(2026, 1, 1, 9, 0, tzinfo=KOLKATA)
        assert sched.poll_once() == 1
        assert calls == [("Reminder", "Wired")]
    finally:
        sched.stop(timeout=5.0)
        reminders_mod._reset_scheduler_for_tests()
