"""Focused tests for the explicit preference store.

All persistence uses tmp_path; the user's real memory.json is never
touched. Network is mocked for the weather-flow test; a socket guard
fails any test that reaches for real sockets.
"""

import json
import os
import socket
import sys
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cat_talker.agent import SIDE_EFFECT_TOOLS, build_system_instructions
from cat_talker.tools import ALL_TOOLS
import cat_talker.memory as mem
from cat_talker.memory import (
    ALLOWED_KEYS,
    MAX_VALUE_LEN,
    PreferenceStore,
)


@pytest.fixture(autouse=True)
def _no_real_sockets(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("real network access blocked in tests")
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)


def _store(tmp_path):
    return PreferenceStore(str(tmp_path / "memory.json"))


# 1/2/4: set, get, update ─────────────────────────────────────────

def test_set_and_get_preference(tmp_path):
    store = _store(tmp_path)
    entry = store.set("preferred_name", "Guru")
    assert entry == {"key": "preferred_name", "value": "Guru",
                     "updated_at": entry["updated_at"]}
    got = store.get("preferred_name")
    assert got["value"] == "Guru"


def test_update_existing_preference(tmp_path):
    store = _store(tmp_path)
    store.set("preferred_name", "Guru")
    store.set("preferred_name", "Guru Ji")
    assert store.get("preferred_name")["value"] == "Guru Ji"


# 3/5/6: missing get, delete, missing delete ───────────────────────

def test_get_missing_preference(tmp_path):
    with pytest.raises(LookupError, match="No preference stored"):
        _store(tmp_path).get("weather_location")


def test_delete_preference(tmp_path):
    store = _store(tmp_path)
    store.set("response_style", "concise")
    assert store.delete("response_style") is True
    with pytest.raises(LookupError):
        store.get("response_style")


def test_delete_missing_is_honest_noop(tmp_path):
    assert _store(tmp_path).delete("weather_location") is False


# 7/8: key/value validation ───────────────────────────────────────

def test_invalid_keys_rejected(tmp_path):
    store = _store(tmp_path)
    for bad in ("favorite_color", "api_key", "password", "", "  ", None, 42):
        with pytest.raises(ValueError, match="Preference error"):
            store.set(bad, "x")
        if isinstance(bad, str) and bad.strip():
            with pytest.raises((ValueError, LookupError)):
                store.get(bad)


def test_temperature_unit_validation(tmp_path):
    store = _store(tmp_path)
    assert store.set("temperature_unit", "c")["value"] == "C"
    assert store.set("temperature_unit", "F")["value"] == "F"
    with pytest.raises(ValueError, match="temperature_unit"):
        store.set("temperature_unit", "Kelvin")


def test_value_length_bound(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="too long"):
        store.set("preferred_name", "x" * (MAX_VALUE_LEN + 1))
    with pytest.raises(ValueError, match="empty"):
        store.set("preferred_name", "   ")


# 9/10/11: persistence, duplicates, init ──────────────────────────

def test_persistence_across_instances(tmp_path):
    path = str(tmp_path / "memory.json")
    PreferenceStore(path).set("weather_location", "Patna")
    second = PreferenceStore(path)
    assert second.get("weather_location")["value"] == "Patna"


def test_duplicate_key_stays_single_entry(tmp_path):
    path = str(tmp_path / "memory.json")
    store = PreferenceStore(path)
    store.set("preferred_name", "A")
    store.set("preferred_name", "B")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert list(data) == ["preferred_name"]
    assert data["preferred_name"]["value"] == "B"


def test_store_init_on_missing_file_and_dirs(tmp_path):
    deep = str(tmp_path / "sub" / "dir" / "memory.json")
    store = PreferenceStore(deep)
    with pytest.raises(LookupError):
        store.get("preferred_name")  # no crash before anything stored
    store.set("preferred_name", "Guru")
    assert os.path.exists(deep)


def test_corrupt_file_is_honest_error(tmp_path):
    path = str(tmp_path / "memory.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    with pytest.raises(RuntimeError, match="unreadable"):
        PreferenceStore(path).get("preferred_name")


def test_unknown_keys_preserved(tmp_path):
    """Coexists with save_user_preference's free-form keys in one file."""
    path = str(tmp_path / "memory.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"music_app": "spotify"}, f)
    store = PreferenceStore(path)
    store.set("preferred_name", "Guru")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["music_app"] == "spotify"
    assert data["preferred_name"]["value"] == "Guru"


# tool surface (isolated store, never the real file) ──────────────

def _patched_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(
        mem, "_DEFAULT_STORE", PreferenceStore(str(tmp_path / "m.json")))


def test_tool_get_set_delete_flow(monkeypatch, tmp_path):
    _patched_tools(monkeypatch, tmp_path)
    from cat_talker.tools import (
        delete_preference, get_preference, set_preference)
    assert "No preference stored" in get_preference("preferred_name")
    assert set_preference("preferred_name", "Guru") == \
        "Saved preferred_name = Guru"
    assert get_preference("preferred_name") == "preferred_name = Guru"
    assert set_preference("preferred_name", "Guru Ji") == \
        "Saved preferred_name = Guru Ji"
    assert "Guru Ji" in get_preference("preferred_name")
    assert delete_preference("preferred_name") == \
        "Deleted preference 'preferred_name'."
    assert "nothing to delete" in delete_preference("preferred_name")
    assert "unknown key" in set_preference("mood", "happy").lower()


# 12/13: registration + classification ────────────────────────────

def test_preference_tools_registered_exactly_once():
    names = [f.__name__ for f in ALL_TOOLS]
    for tool in ("get_preference", "set_preference", "delete_preference"):
        assert names.count(tool) == 1, tool


def test_preference_tool_classification():
    # Pure read stays out; state-changing writes follow the established
    # save_user_preference precedent (dedupe identical writes per turn).
    assert "get_preference" not in SIDE_EFFECT_TOOLS
    assert "save_user_preference" in SIDE_EFFECT_TOOLS
    assert "set_preference" in SIDE_EFFECT_TOOLS
    assert "delete_preference" in SIDE_EFFECT_TOOLS


# 14: weather uses weather_location without changing weather.py ────

def test_weather_uses_stored_location_through_tool_flow(
        monkeypatch, tmp_path):
    _patched_tools(monkeypatch, tmp_path)
    from cat_talker.tools import get_preference, get_weather, set_preference

    assert "Saved weather_location = Patna" in \
        set_preference("weather_location", "Patna")
    stored = get_preference("weather_location")
    assert stored == "weather_location = Patna"
    place = stored.split("=", 1)[1].strip()

    seen = {}

    class _Resp:
        def __init__(self, payload: dict):
            self._body = json.dumps(payload).encode()
            self.status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            out, self._body = self._body[:n], self._body[n:]
            return out

    def _fake(req, timeout=None):
        url = req.full_url
        seen.setdefault("urls", []).append(url)
        if "geocoding-api" in url:
            assert "name=Patna" in url
            return _Resp({"results": [{
                "name": "Patna", "latitude": 25.59, "longitude": 85.13,
                "country": "India", "admin1": "Bihar"}]})
        return _Resp({
            "current": {"temperature_2m": 30.0,
                        "relative_humidity_2m": 60.0,
                        "apparent_temperature": 32.0,
                        "weather_code": 1,
                        "precipitation": 0.0,
                        "wind_speed_10m": 10.0},
            "daily": {"time": ["2026-10-04"],
                      "weather_code": [1],
                      "temperature_2m_max": [33.0],
                      "temperature_2m_min": [25.0],
                      "precipitation_probability_max": [10.0]}})

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    out = get_weather(place)
    assert "Patna, Bihar, India" in out
    assert "30 °C" in out


# no automatic persistence ─────────────────────────────────────────

def test_no_automatic_memory_api():
    for name in ("remember", "extract", "auto_remember", "memorize",
                 "learn_fact", "summarize", "observe"):
        assert not hasattr(mem, name), name
    # Arbitrary conversation text is not a valid key and never persists.
    with pytest.raises(ValueError):
        mem.normalize_key("I love rainy evenings in Patna")


# guidance ─────────────────────────────────────────────────────────

def test_system_guidance_memory_is_explicit():
    text = build_system_instructions()
    assert "get_preference" in text
    assert "set_preference" in text
    assert "delete_preference" in text
    assert "never automatic" in text.lower() or "explicit" in text.lower()
    assert "weather_location" in text
    assert len(ALLOWED_KEYS) == 5
