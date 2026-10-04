"""Deterministic mocked tests for the Open-Meteo weather tool.

No test touches the real internet. The mocked seam is the exact one the
implementation uses: urllib.request.urlopen. A socket-level guard fails
any test that slips through to real sockets.
"""

import io
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cat_talker.agent import SIDE_EFFECT_TOOLS, build_system_instructions
from cat_talker.tools import ALL_TOOLS, get_weather
import cat_talker.weather as wmod
from cat_talker.weather import (
    describe_weather_code,
    fetch_forecast,
    geocode_location,
)


@pytest.fixture(autouse=True)
def _no_real_sockets(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("real network access blocked in tests")
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)


GEOCODE_PATNA = {
    "results": [{
        "name": "Patna",
        "latitude": 25.59408,
        "longitude": 85.13756,
        "country": "India",
        "admin1": "Bihar",
    }]
}


def _forecast_doc(days=1):
    dates = [f"2026-10-0{i + 3}" for i in range(days)]
    return {
        "current": {
            "temperature_2m": 31.5,
            "relative_humidity_2m": 70.0,
            "apparent_temperature": 35.0,
            "weather_code": 2,
            "precipitation": 0.0,
            "wind_speed_10m": 12.5,
        },
        "daily": {
            "time": dates,
            "weather_code": [2] * days,
            "temperature_2m_max": [34.0] * days,
            "temperature_2m_min": [26.0] * days,
            "precipitation_probability_max": [20.0] * days,
        },
    }


class _FakeResp:
    def __init__(self, payload, status=200):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        self._body = payload.encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        if n is not None and n >= 0:
            out, self._body = self._body[:n], self._body[n:]
            return out
        out, self._body = self._body, b""
        return out


def _mock_open_meteo(monkeypatch, geocode=None, forecast_days=1, capture=None):
    def _fake(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if capture is not None:
            capture.setdefault("urls", []).append(url)
            capture["timeout"] = timeout
        if "geocoding-api" in url:
            return _FakeResp(geocode if geocode is not None else GEOCODE_PATNA)
        return _FakeResp(_forecast_doc(forecast_days))
    monkeypatch.setattr(urllib.request, "urlopen", _fake)


# ─── registration / classification ────────────────────────────────

def test_get_weather_registered_exactly_once():
    names = [f.__name__ for f in ALL_TOOLS]
    assert names.count("get_weather") == 1


def test_get_weather_not_a_side_effect():
    assert "get_weather" not in SIDE_EFFECT_TOOLS


# ─── happy path ───────────────────────────────────────────────────

def test_successful_lookup(monkeypatch):
    _mock_open_meteo(monkeypatch)
    out = get_weather("Patna")
    assert "Patna, Bihar, India" in out
    assert "31.5 °C" in out
    assert "35 °C" in out
    assert "Partly cloudy" in out
    assert "12.5 km/h" in out
    assert "34 °C" in out and "26 °C" in out


def test_resolved_lat_lon_forwarded(monkeypatch):
    capture = {}
    _mock_open_meteo(monkeypatch, capture=capture)
    get_weather("Patna Bihar")
    forecast_urls = [u for u in capture["urls"] if "/v1/forecast" in u]
    assert len(forecast_urls) == 1
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(forecast_urls[0]).query)
    assert abs(float(qs["latitude"][0]) - 25.59408) < 1e-6
    assert abs(float(qs["longitude"][0]) - 85.13756) < 1e-6
    assert qs["temperature_unit"] == ["celsius"]
    assert qs["wind_speed_unit"] == ["kmh"]
    assert capture["timeout"] == wmod.HTTP_TIMEOUT


def test_multi_day_forecast(monkeypatch):
    _mock_open_meteo(monkeypatch, forecast_days=3)
    out = get_weather("Patna", days=3)
    assert "2026-10-03" in out
    assert "2026-10-04" in out
    assert "2026-10-05" in out


def test_celsius_and_kmh_units(monkeypatch):
    _mock_open_meteo(monkeypatch)
    out = get_weather("London")
    assert "°C" in out
    assert "km/h" in out
    assert "°F" not in out
    assert "mph" not in out


def test_days_clamped_to_max(monkeypatch):
    capture = {}
    _mock_open_meteo(monkeypatch, capture=capture)
    get_weather("Patna", days=99)
    qs = urllib.parse.parse_qs(
        urllib.parse.urlparse(capture["urls"][-1]).query)
    assert qs["forecast_days"] == [str(wmod.MAX_DAYS)]


# ─── honest failures ──────────────────────────────────────────────

def test_location_not_found(monkeypatch):
    _mock_open_meteo(monkeypatch, geocode={"results": []})
    out = get_weather("Nowhere XYZ")
    assert "not found" in out.lower()


def test_empty_location_asks_for_place():
    out = get_weather("   ")
    assert "name a place" in out.lower()


def test_network_failure_is_honest(monkeypatch):
    def _boom(req, timeout=None):
        raise urllib.error.URLError("dns down")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    out = get_weather("Patna")
    assert isinstance(out, str)
    assert "error" in out.lower() or "failed" in out.lower()


def test_http_error_is_honest(monkeypatch):
    def _boom(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 500, "err", {}, io.BytesIO(b""))
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    out = get_weather("Patna")
    assert "HTTP error" in out or "failed" in out.lower()


def test_malformed_geocode_response(monkeypatch):
    _mock_open_meteo(monkeypatch, geocode={"unexpected": 1})
    out = get_weather("Patna")
    assert "not found" in out.lower() or "malformed" in out.lower()


def test_malformed_forecast_response(monkeypatch):
    class _BadForecastResp(_FakeResp):
        pass

    def _fake(req, timeout=None):
        url = req.full_url
        if "geocoding-api" in url:
            return _FakeResp(GEOCODE_PATNA)
        return _FakeResp({"current": {}, "daily": {}})
    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    out = get_weather("Patna")
    assert "malformed" in out.lower()


def test_geocode_helpers_reject_bad_input():
    with pytest.raises(ValueError):
        geocode_location("  ")
    with pytest.raises(ValueError):
        fetch_forecast(999.0, 0.0)


def test_describe_weather_code_unknown_stays_honest():
    assert describe_weather_code(0) == "Clear sky"
    assert "9999" in describe_weather_code(9999)


# ─── safety: no subprocess/shell ──────────────────────────────────

def test_no_subprocess_or_shell(monkeypatch):
    import subprocess

    def _boom(*a, **k):
        raise AssertionError("subprocess must not be used")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    _mock_open_meteo(monkeypatch)
    assert "Patna" in get_weather("Patna")
    import inspect
    src = inspect.getsource(wmod)
    assert "subprocess" not in src
    assert "os.system" not in src
    assert "shell=True" not in src


# ─── assistant guidance ───────────────────────────────────────────

def test_system_guidance_routes_weather_to_tool():
    text = build_system_instructions()
    assert "get_weather" in text
    assert "get_current_datetime" in text
    assert "web_search" in text
    assert "Never guess or pretend to know the user's location" in text
