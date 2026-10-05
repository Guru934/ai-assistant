"""Settings form logic over the existing configuration architecture.

Pure data layer (no Qt): load_snapshot() reads the current values via
the established boundaries (config.py for application settings,
memory PreferenceStore for preference-style values); validate_settings()
checks a candidate dict without persisting anything; apply_settings()
validates everything first and persists only when every field is valid
(all-or-nothing - a bad threshold never blocks on a good phrase, and a
bad phrase never leaves a half-written config behind).

The API key is never exposed here: the snapshot carries only a
configured flag; replacement/removal go through the config boundary.
"""

import os

from cat_talker.logging_config import get_logger

logger = get_logger("cat_talker.settings")

PREFERENCE_FIELDS = ("preferred_language", "response_style",
                     "weather_location")
MAX_PREFERENCE_LEN = 200

# Snapshot keys (flat dict; widgets bind 1:1).
BOOLEAN_FIELDS = ("wake_word_enabled", "voice_approval_enabled",
                  "echo_suppress_enabled", "auto_reconnect",
                  "api_key_clear")
TEXT_FIELDS = ("wake_word_phrase", "wake_word_model", "preferred_monitor",
               "preferred_language", "response_style", "weather_location")


def _store():
    from cat_talker import memory as memory_mod
    return memory_mod.PreferenceStore()


def _preference_or_empty(store, key):
    try:
        return store.get(key)["value"]
    except (LookupError, ValueError, RuntimeError):
        return ""
    except Exception:
        return ""


def load_snapshot() -> dict:
    """Read current values. Never raises; falls back to safe defaults."""
    from cat_talker import config as config_mod
    try:
        cfg = config_mod.load_config()
    except Exception:
        cfg = {}
    store = _store()
    snapshot = {
        "wake_word_enabled": bool(cfg.get("wake_word_enabled", False)),
        "wake_word_phrase": cfg.get("wake_word_phrase", "Hey Jarvis") or "Hey Jarvis",
        "wake_word_model": cfg.get("wake_word_model", "hey_jarvis") or "hey_jarvis",
        "wake_word_threshold": cfg.get("wake_word_threshold", 0.5),
        "preferred_monitor": cfg.get("preferred_monitor", "") or "",
        "voice_approval_enabled": bool(cfg.get("voice_approval_enabled", True)),
        "echo_suppress_enabled": bool(cfg.get("echo_suppress_enabled", True)),
        "auto_reconnect": bool(cfg.get("auto_reconnect", True)),
        "api_key_configured": bool(config_mod.is_api_key_configured()),
        "api_key_new": "",
        "api_key_clear": False,
    }
    for key in PREFERENCE_FIELDS:
        snapshot[key] = _preference_or_empty(store, key)
    try:
        snapshot["wake_word_threshold"] = float(snapshot["wake_word_threshold"])
    except (TypeError, ValueError):
        snapshot["wake_word_threshold"] = 0.5
    return snapshot


def validate_settings(values) -> list:
    """Check a candidate snapshot. Returns a list of error strings
    (empty means valid). Persists nothing."""
    errors = []
    if not isinstance(values, dict):
        return ["Settings must be a mapping."]
    for key in BOOLEAN_FIELDS:
        if not isinstance(values.get(key), bool):
            errors.append(f"{key} must be true or false.")
    phrase = values.get("wake_word_phrase", "")
    if not isinstance(phrase, str) or not phrase.strip():
        errors.append("Wake phrase must not be empty.")
    model = values.get("wake_word_model", "")
    errors.extend(_validate_wake_model(model))
    try:
        threshold = float(values.get("wake_word_threshold", 0.5))
    except (TypeError, ValueError):
        errors.append("Wake threshold must be a number.")
        threshold = None
    if threshold is not None and not 0.0 < threshold <= 1.0:
        errors.append("Wake threshold must be greater than 0 and at most 1.")
    for key in ("preferred_monitor",):
        if not isinstance(values.get(key), str):
            errors.append(f"{key} must be text.")
    for key in PREFERENCE_FIELDS:
        val = values.get(key, "")
        if not isinstance(val, str):
            errors.append(f"{key} must be text.")
        elif len(val.strip()) > MAX_PREFERENCE_LEN:
            errors.append(f"{key} is too long (max {MAX_PREFERENCE_LEN}).")
    new_key = values.get("api_key_new", "")
    if new_key is None:
        new_key = ""
    if not isinstance(new_key, str):
        errors.append("Replacement API key must be text.")
    return errors


def _validate_wake_model(model) -> list:
    if not isinstance(model, str) or not model.strip():
        return ["Wake model must not be empty."]
    model = model.strip()
    looks_like_path = model.endswith(".onnx") or os.sep in model
    if looks_like_path:
        if not os.path.isfile(model):
            return [f"Wake model file not found: {model}."]
        return []
    try:
        from cat_talker import wakeword as wakeword_mod
        known = wakeword_mod.builtin_models()
    except Exception:
        known = frozenset()
    if known and model not in known:
        return [f"Unknown built-in wake model {model!r} "
                f"(known: {', '.join(sorted(known))})."]
    return []


def apply_settings(values) -> tuple:
    """Validate-then-persist. Returns (ok, message).

    ok=False leaves every store untouched. ok=True means all writes
    went through the established config/memory boundaries.
    """
    errors = validate_settings(values)
    if errors:
        return False, "; ".join(errors)
    from cat_talker import config as config_mod
    store = _store()
    try:
        outcome = []
        config_mod.set_wake_word_enabled(bool(values["wake_word_enabled"]))
        outcome.append("wake-word enabled saved")
        config_mod.set_wake_word(
            values["wake_word_phrase"].strip(),
            model=values["wake_word_model"].strip(),
            threshold=float(values["wake_word_threshold"]))
        outcome.append("wake phrase saved")
        config_mod.set_preferred_monitor(values["preferred_monitor"].strip())
        config_mod.set_voice_approval(bool(values["voice_approval_enabled"]))
        config_mod.set_echo_suppress(bool(values["echo_suppress_enabled"]))
        config_mod.set_auto_reconnect(bool(values["auto_reconnect"]))
        outcome.append("assistant options saved")
        for key in PREFERENCE_FIELDS:
            val = values.get(key, "")
            if isinstance(val, str) and val.strip():
                store.set(key, val.strip())
            else:
                try:
                    store.delete(key)
                except (ValueError, RuntimeError):
                    pass
        outcome.append("preferences saved")
        if values.get("api_key_clear"):
            config_mod.clear_api_key()
            outcome.append("stored API key removed")
        elif isinstance(values.get("api_key_new"), str) \
                and values["api_key_new"].strip():
            result = config_mod.set_api_key(values["api_key_new"].strip())
            if result.startswith("Error"):
                return False, result
            outcome.append("API key replaced")
        return True, "Settings saved: " + ", ".join(outcome) + "."
    except Exception as e:
        logger.error("settings apply failed: %s", e)
        return False, f"Could not save settings: {e}"
