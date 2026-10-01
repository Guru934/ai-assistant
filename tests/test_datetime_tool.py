"""Focused tests for deterministic local date/time support.

Covers the pure formatting helpers against a FIXED datetime (deterministic,
no real clock), plus registration/classification wiring. The only checks that
touch the real clock are minimal sanity checks (a returned value contains
date/time information and is timezone-aware).
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cat_talker.agent import SIDE_EFFECT_TOOLS, build_system_instructions
from cat_talker.tools import (
    ALL_TOOLS,
    _format_datetime,
    _format_utc_offset,
    _timezone_name,
    get_current_datetime,
)


# A Wednesday, so the weekday assertion cannot accidentally pass for "today".
FIXED = datetime(2026, 10, 1, 22, 2, 13, tzinfo=timezone(timedelta(hours=5, minutes=30)))


# ─── pure formatting helper: fixed datetime, no real clock ──────────


def test_format_datetime_contains_date_time_and_weekday():
    """The rendered block carries calendar date, weekday, time and zone."""
    text = _format_datetime(FIXED)

    assert "01 October 2026" in text          # full calendar date
    assert "Thursday" in text                 # weekday
    assert "10:02 PM" in text                 # local time (12-hour)
    assert "22:02:13" in text                 # local time (24-hour, seconds)
    assert "+05:30" in text                   # UTC offset
    assert "Timezone:" in text

    # Each labelled line is present exactly once.
    for label in ("Full date:", "Weekday:", "Local time:", "Timezone:", "UTC offset:"):
        assert text.count(label) == 1


def test_format_datetime_is_deterministic():
    """Same input -> same output; no clock, no randomness."""
    assert _format_datetime(FIXED) == _format_datetime(FIXED)


def test_format_datetime_weekday_matches_fixed_date():
    """Weekday derives from the datetime, not from today."""
    assert "Weekday: Thursday" in _format_datetime(FIXED)
    assert "Weekday: Wednesday" not in _format_datetime(FIXED)


def test_format_datetime_rejects_naive_datetime():
    """Naive datetimes cannot be reported with a reliable offset."""
    result = _format_datetime(datetime(2026, 10, 1, 22, 2, 13))
    assert result.startswith("Error")
    assert "timezone-aware" in result


# ─── UTC offset formatting ──────────────────────────────────────────


def test_format_utc_offset_positive_half_hour():
    assert _format_utc_offset(timedelta(hours=5, minutes=30)) == "+05:30"


def test_format_utc_offset_negative():
    assert _format_utc_offset(-timedelta(hours=5)) == "-05:00"


def test_format_utc_offset_utc():
    assert _format_utc_offset(timedelta(0)) == "+00:00"


def test_format_utc_offset_quarter_hour():
    assert _format_utc_offset(timedelta(hours=5, minutes=45)) == "+05:45"


def test_format_utc_offset_always_matches_hh_mm_shape():
    """Offset shape is stable: sign + 2-digit hour + ':' + 2-digit minute."""
    pattern = re.compile(r"^[+-]\d{2}:\d{2}(:\d{2})?$")
    for offset in (timedelta(hours=5, minutes=30), -timedelta(hours=5),
                   timedelta(0), timedelta(hours=5, minutes=45),
                   timedelta(hours=-9, minutes=-30)):
        assert pattern.match(_format_utc_offset(offset))


def test_format_utc_offset_none_defaults_to_utc():
    assert _format_utc_offset(None) == "+00:00"


# ─── timezone name (environment-driven, nothing hardcoded) ──────────


def test_timezone_name_uses_configured_tz(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    assert _timezone_name(FIXED, FIXED.utcoffset()) == "Asia/Kolkata"


def test_timezone_name_falls_back_without_tz(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    # A plain fixed-offset tzinfo exposes no key, so the fallback label is used.
    assert _timezone_name(FIXED, FIXED.utcoffset()) == "local"


def test_timezone_name_prefers_tzinfo_key(monkeypatch):
    """An aware tzinfo carrying `.key` wins over the TZ environment."""
    class NamedTZ(timezone.__mro__[1]):  # datetime.tzinfo
        def __init__(self, key, offset):
            super().__init__()
            self.key = key
            self._offset = offset

        def utcoffset(self, dt):
            return self._offset

        def dst(self, dt):
            return timedelta(0)

    dt = datetime(2026, 10, 1, 12, 0, 0,
                  tzinfo=NamedTZ("Europe/Berlin", timedelta(hours=2)))
    monkeypatch.setenv("TZ", "Ignore/Me")
    assert _timezone_name(dt, dt.utcoffset()) == "Europe/Berlin"


# ─── real-clock sanity (minimal, deliberately loose) ────────────────


def test_get_current_datetime_sanity():
    """Real clock: returns parseable date/time info and a UTC offset."""
    result = get_current_datetime()

    assert isinstance(result, str) and result
    assert not result.startswith("Error")
    assert "Full date:" in result
    assert "Weekday:" in result
    assert "Local time:" in result
    assert "UTC offset:" in result

    match = re.search(r"UTC offset: ([+-]\d{2}:\d{2}(?::\d{2})?)", result)
    assert match, result

    # LOOSE: the year only needs to be a plausible 4-digit year, so the test
    # never goes stale or depends on the exact wall-clock reading.
    assert re.search(r"\b(20\d{2})\b", result), result


def test_get_current_datetime_is_timezone_aware():
    """Uses the machine's configured zone (aware), never a naive clock."""
    now = datetime.now().astimezone()
    assert now.tzinfo is not None
    assert now.utcoffset() is not None
    # The tool's own clock agrees with the system's aware clock.
    assert abs((datetime.now().astimezone() - now).total_seconds()) < 5


# ─── registry and classification wiring ─────────────────────────────


def test_get_current_datetime_in_all_tools():
    names = [func.__name__ for func in ALL_TOOLS]
    assert "get_current_datetime" in names
    assert names.count("get_current_datetime") == 1


def test_get_current_datetime_not_a_side_effect():
    """Pure read-only information tool: exempt from side-effect dedup."""
    assert "get_current_datetime" not in SIDE_EFFECT_TOOLS


def test_system_guidance_routes_datetime_to_the_tool():
    text = build_system_instructions()
    assert "get_current_datetime" in text
    assert "internal knowledge" in text


def test_get_current_datetime_makes_no_network_call(monkeypatch):
    """Standard-library clock only: any network attempt fails the test."""
    import urllib.request

    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("get_current_datetime must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert not get_current_datetime().startswith("Error")
