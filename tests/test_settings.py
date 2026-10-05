"""Focused tests for the Chibi settings UI.

Pure settings logic (snapshot/validate/apply) is tested without Qt;
widget binding is tested offscreen against the real SettingsDialog;
no real config/memory files are touched (paths redirected to tmp).
"""

import json
import os
import sys
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cat_talker.config as config_mod
import cat_talker.memory as memory_mod
from cat_talker import settings as settings_mod

from PyQt6.QtWidgets import QApplication

# Module-level reference: if the last QApplication Python wrapper is
# garbage-collected (e.g. a helper-local), dependent widgets die with it.
_qt_app = QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_stores(tmp_path, monkeypatch):
    """Redirect config file + memory store into tmp.

    NOTE: memory._DEFAULT_STORE binds its path at import, so patching
    DEFAULT_MEMORY_PATH alone is not enough - replace the store too,
    otherwise tests write to the real ~/.config/cat-talker/memory.json.
    """
    cfg = tmp_path / "config.json"
    mem = tmp_path / "memory.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg))
    # Isolate the API-key status too: an exported GEMINI_API_KEY counts
    # as configured by design, so drop it for deterministic tests.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # Both redirections are needed: settings builds fresh PreferenceStore()
    # instances from DEFAULT_MEMORY_PATH, while the set_/get_preference
    # helpers use the import-bound _DEFAULT_STORE.
    monkeypatch.setattr(memory_mod, "DEFAULT_MEMORY_PATH", str(mem))
    monkeypatch.setattr(memory_mod, "_DEFAULT_STORE",
                        memory_mod.PreferenceStore(str(mem)))
    return cfg, mem


def _valid_values(**over):
    values = {
        "wake_word_enabled": True,
        "wake_word_phrase": "Hey Jarvis",
        "wake_word_model": "hey_jarvis",
        "wake_word_threshold": 0.5,
        "preferred_monitor": "focused",
        "voice_approval_enabled": True,
        "echo_suppress_enabled": True,
        "auto_reconnect": True,
        "preferred_language": "Hindi",
        "response_style": "concise",
        "weather_location": "Mumbai",
        "api_key_new": "",
        "api_key_clear": False,
    }
    values.update(over)
    return values


# ---------------------------------------------------------------------------
# Logic: snapshot / validate / apply
# ---------------------------------------------------------------------------

def test_snapshot_loads_current_values(isolated_stores):
    config_mod.set_wake_word_enabled(True)
    config_mod.set_wake_word("Hey Jarvis", model="hey_jarvis",
                             threshold=0.7)
    config_mod.set_preferred_monitor("HDMI-1")
    memory_mod.set_preference("weather_location", "Pune")
    snap = settings_mod.load_snapshot()
    assert snap["wake_word_enabled"] is True
    assert snap["wake_word_phrase"] == "Hey Jarvis"
    assert snap["wake_word_model"] == "hey_jarvis"
    assert snap["wake_word_threshold"] == 0.7
    assert snap["preferred_monitor"] == "HDMI-1"
    assert snap["weather_location"] == "Pune"
    assert snap["preferred_language"] == ""
    assert snap["api_key_configured"] is False
    assert snap["api_key_new"] == ""


def test_valid_values_pass_validation():
    assert settings_mod.validate_settings(_valid_values()) == []


def test_invalid_threshold_rejected():
    for bad in (0, -1, 1.5, "high", None):
        errors = settings_mod.validate_settings(
            _valid_values(wake_word_threshold=bad))
        assert any("threshold" in e for e in errors), (bad, errors)


def test_empty_phrase_and_model_rejected():
    assert any("phrase" in e for e in settings_mod.validate_settings(
        _valid_values(wake_word_phrase="   ")))
    assert any("model" in e for e in settings_mod.validate_settings(
        _valid_values(wake_word_model="")))


def test_unknown_builtin_model_rejected():
    errors = settings_mod.validate_settings(
        _valid_values(wake_word_model="hey_jarvi"))
    assert any("model" in e for e in errors), errors


def test_missing_model_file_rejected(tmp_path):
    errors = settings_mod.validate_settings(_valid_values(
        wake_word_model=str(tmp_path / "ghost.onnx")))
    assert any("not found" in e for e in errors), errors


def test_existing_model_file_accepted(tmp_path):
    model = tmp_path / "chibi.onnx"
    model.write_bytes(b"fake")
    assert settings_mod.validate_settings(
        _valid_values(wake_word_model=str(model))) == []


def test_overlong_preference_rejected():
    errors = settings_mod.validate_settings(
        _valid_values(response_style="x" * 201))
    assert any("response_style" in e for e in errors), errors


def test_valid_changes_persist(isolated_stores):
    ok, message = settings_mod.apply_settings(_valid_values())
    assert ok is True, message
    assert config_mod.get_wake_word_enabled() is True
    assert config_mod.get_wake_word_phrase() == "Hey Jarvis"
    assert config_mod.get_wake_word_threshold() == 0.5
    assert config_mod.get_preferred_monitor() == "focused"
    assert memory_mod.get_preference("weather_location") == \
        "weather_location = Mumbai"


def test_invalid_changes_persist_nothing(isolated_stores):
    before = dict(settings_mod.load_snapshot())
    ok, message = settings_mod.apply_settings(
        _valid_values(wake_word_threshold=5, wake_word_phrase="  "))
    assert ok is False
    assert "threshold" in message and "phrase" in message
    after = settings_mod.load_snapshot()
    assert after["wake_word_enabled"] == before["wake_word_enabled"]
    assert after["wake_word_phrase"] == before["wake_word_phrase"]
    assert after["preferred_monitor"] == before["preferred_monitor"]
    assert after["weather_location"] == before["weather_location"]


def test_wake_enable_disable_persists(isolated_stores):
    assert settings_mod.apply_settings(
        _valid_values(wake_word_enabled=True))[0] is True
    assert config_mod.get_wake_word_enabled() is True
    assert settings_mod.apply_settings(
        _valid_values(wake_word_enabled=False))[0] is True
    assert config_mod.get_wake_word_enabled() is False


def test_wake_phrase_persists(isolated_stores):
    assert settings_mod.apply_settings(
        _valid_values(wake_word_phrase="Hey Chibi"))[0] is True
    assert config_mod.get_wake_word_phrase() == "Hey Chibi"


def test_empty_preference_deletes(isolated_stores):
    memory_mod.set_preference("weather_location", "Pune")
    assert settings_mod.apply_settings(
        _valid_values(weather_location=""))[0] is True
    assert "No preference stored" in \
        memory_mod.get_preference("weather_location")


def test_api_key_never_in_snapshot(isolated_stores):
    config_mod.set_api_key("sk-secret-123")
    snap = settings_mod.load_snapshot()
    assert snap["api_key_configured"] is True
    blob = json.dumps(snap)
    assert "sk-secret-123" not in blob
    assert "api_key_new" in snap and snap["api_key_new"] == ""


def test_api_key_replace_and_remove(isolated_stores):
    config_mod.set_api_key("sk-old")
    ok, _ = settings_mod.apply_settings(_valid_values(api_key_new="sk-new"))
    assert ok is True
    assert config_mod.get_api_key() == "sk-new"
    ok, _ = settings_mod.apply_settings(
        _valid_values(api_key_new="", api_key_clear=True))
    assert ok is True
    assert config_mod.get_api_key() in (None, "")


# ---------------------------------------------------------------------------
# Widgets (offscreen): binding, save/cancel, error display
# ---------------------------------------------------------------------------

def _dialog(isolated_stores):
    from cat_talker.main import SettingsDialog, get_overlay_window
    window = get_overlay_window()
    dialog = SettingsDialog(window)
    return dialog


def test_dialog_construction_and_load(isolated_stores):
    config_mod.set_wake_word("Hey Jarvis", model="hey_jarvis",
                             threshold=0.7)
    dialog = _dialog(isolated_stores)
    assert dialog.wake_phrase.text() == "Hey Jarvis"
    assert dialog.wake_model.text() == "hey_jarvis"
    assert dialog.wake_threshold.value() == 0.7
    assert "Configured" not in dialog.api_status.text()
    assert "sk-" not in dialog.api_status.text()
    assert dialog.windowTitle() == "Chibi Settings"


def test_dialog_save_persists(isolated_stores):
    dialog = _dialog(isolated_stores)
    dialog.wake_enabled.setChecked(True)
    dialog.wake_phrase.setText("Hey Chibi")
    dialog.wake_threshold.setValue(0.6)
    dialog.weather_location.setText("Delhi")
    dialog._on_save()
    assert dialog.result() != 0  # accepted
    assert config_mod.get_wake_word_enabled() is True
    assert config_mod.get_wake_word_phrase() == "Hey Chibi"
    assert "Delhi" in memory_mod.get_preference("weather_location")


def test_dialog_invalid_keeps_open_and_persists_nothing(isolated_stores):
    dialog = _dialog(isolated_stores)
    dialog.wake_phrase.setText("   ")
    dialog.wake_threshold.setValue(0.5)
    dialog._on_save()
    assert dialog.isVisible() is False  # never exec'd; still rejected
    assert dialog.result() == 0  # not accepted
    assert dialog.error_label.isVisible() or dialog.error_label.text() != ""
    assert "phrase" in dialog.error_label.text()
    assert config_mod.get_wake_word_phrase() != ""  # unchanged default
    assert config_mod.get_wake_word_enabled() is False


def test_dialog_reject_persists_nothing(isolated_stores):
    dialog = _dialog(isolated_stores)
    dialog.wake_phrase.setText("Changed")
    dialog.wake_enabled.setChecked(True)
    dialog.reject()  # Cancel
    assert config_mod.get_wake_word_enabled() is False
    assert config_mod.get_wake_word_phrase() == "Hey Jarvis"


def test_dialog_runs_on_main_thread(isolated_stores):
    dialog = _dialog(isolated_stores)
    assert threading.current_thread() is threading.main_thread()
    parent = dialog.parent()
    assert parent is not None
    assert dialog.thread() is parent.thread()


def test_settings_opens_no_second_window(isolated_stores):
    from cat_talker.main import count_overlay_windows, get_overlay_window
    window = get_overlay_window()
    before = count_overlay_windows()
    dialog = _dialog(isolated_stores)
    assert count_overlay_windows() == before
    assert dialog.parent() is window
    dialog.reject()


def test_menu_settings_action_opens_dialog(isolated_stores, monkeypatch):
    from cat_talker.main import get_overlay_window
    window = get_overlay_window()
    opened = []
    monkeypatch.setattr(window, "open_settings",
                        lambda: opened.append(True))
    menu, actions = window._build_context_menu()
    assert "settings" in actions
    window._handle_menu_action(actions["settings"], actions)
    assert opened == [True]


def test_settings_never_quits_application(isolated_stores):
    """Save/Cancel paths must not touch process lifetime (F3 owns quit)."""
    from PyQt6.QtWidgets import QApplication
    dialog = _dialog(isolated_stores)
    dialog.wake_phrase.setText("Hey Chibi")
    dialog._on_save()
    assert QApplication.instance() is not None
    dialog2 = _dialog(isolated_stores)
    dialog2.reject()
    assert QApplication.instance() is not None
