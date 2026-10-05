import json
import os
import sys
from typing import Any, Dict, Optional

from cat_talker.logging_config import get_logger

logger = get_logger("cat_talker.config")


CONFIG_PATH = os.path.expanduser("~/.config/cat-talker/config.json")
DEFAULT_CONFIG = {
    "api_key": "",
    "preferred_monitor": "",
    "auto_reconnect": True,
    "voice_approval_enabled": True,
    "echo_suppress_enabled": True,
    # Local wake-word activation (optional, default OFF). When enabled,
    # the sleeping assistant monitors the already-open mic stream for
    # the configured phrase and wakes through the normal F2 path - fully
    # offline (openwakeword), no audio leaves the machine, no Gemini
    # session while sleeping. Default off because it is continuous
    # local microphone monitoring: enabling it must be explicit.
    # wake_word_model: a built-in openwakeword model key ("hey_jarvis",
    # "hey_mycroft", "alexa", "timer", "weather") or an absolute path to
    # a custom trained .onnx (e.g. a future "Hey Chibi" model).
    # wake_word_phrase is the human label of what to say.
    "wake_word_enabled": False,
    "wake_word_phrase": "Hey Jarvis",
    "wake_word_model": "hey_jarvis",
    "wake_word_threshold": 0.5,
}


def load_config() -> Dict[str, Any]:
    """Load configuration from config file, falling back to environment variables."""
    config = dict(DEFAULT_CONFIG)

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                file_config = json.load(f)
                config.update(file_config)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Failed to load config: {e}")

    # Apply environment variable overrides
    env_key = os.environ.get("GEMINI_API_KEY")
    if env_key and not config.get("api_key"):
        config["api_key"] = env_key

    return config


def save_config(config: Dict[str, Any]) -> str:
    """Save configuration to config file."""
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
        return f"Configuration saved to {CONFIG_PATH}"
    except Exception as e:
        logger.error(f"Error saving config: {e}")
        return f"Error saving config: {e}"


def get_api_key() -> Optional[str]:
    """Get the Gemini API key, preferring config file then environment variable."""
    config = load_config()
    return config.get("api_key") or os.environ.get("GEMINI_API_KEY")


def get_preferred_monitor() -> str:
    """Get the preferred monitor name for capture."""
    config = load_config()
    return config.get("preferred_monitor", "")


def set_preferred_monitor(monitor_name: str) -> str:
    """Set the preferred monitor name and save config."""
    config = load_config()
    config["preferred_monitor"] = monitor_name
    result = save_config(config)
    return result


def get_auto_reconnect() -> bool:
    """Get auto-reconnect setting."""
    config = load_config()
    return config.get("auto_reconnect", True)


def set_auto_reconnect(enabled: bool) -> str:
    """Set auto-reconnect setting and save config."""
    config = load_config()
    config["auto_reconnect"] = enabled
    return save_config(config)


def get_voice_approval() -> bool:
    """Get voice approval enabled setting."""
    config = load_config()
    return config.get("voice_approval_enabled", True)


def get_echo_suppress() -> bool:
    """Get echo-suppression-of-speaker-audio setting (default on).

    Environment override for controlled experiments (no DSP change):
    CAT_TALKER_ECHO_SUPPRESS=0/false/no/off disables, =1/true/yes/on
    enables. Unset (or anything else) falls back to the config file.
    AudioInterface reads this once at construction, so changing it
    requires a process restart (F3 quit + start).
    """
    override = os.environ.get("CAT_TALKER_ECHO_SUPPRESS", "").strip().lower()
    if override in ("0", "false", "no", "off"):
        return False
    if override in ("1", "true", "yes", "on"):
        return True
    config = load_config()
    return config.get("echo_suppress_enabled", True)


def set_echo_suppress(enabled: bool) -> str:
    """Enable/disable echo suppression and save config."""
    config = load_config()
    config["echo_suppress_enabled"] = enabled
    result = save_config(config)
    return result


def set_voice_approval(enabled: bool) -> str:
    """Set voice approval enabled setting and save config."""
    config = load_config()
    config["voice_approval_enabled"] = enabled
    return save_config(config)


def get_wake_word_enabled() -> bool:
    """Local wake-word activation enabled (default off)."""
    return bool(load_config().get("wake_word_enabled", False))


def set_wake_word_enabled(enabled: bool) -> str:
    """Enable/disable local wake-word activation and save config."""
    config = load_config()
    config["wake_word_enabled"] = bool(enabled)
    return save_config(config)


def get_wake_word_phrase() -> str:
    """Human label of the wake phrase (default "Hey Jarvis")."""
    phrase = load_config().get("wake_word_phrase", "Hey Jarvis")
    return phrase if isinstance(phrase, str) and phrase.strip() else "Hey Jarvis"


def get_wake_word_model() -> str:
    """Built-in openwakeword model key or absolute custom .onnx path."""
    model = load_config().get("wake_word_model", "hey_jarvis")
    return model if isinstance(model, str) and model.strip() else "hey_jarvis"


def get_wake_word_threshold() -> float:
    """Detection threshold in (0, 1]; out-of-range values clamp to 0.5."""
    try:
        threshold = float(load_config().get("wake_word_threshold", 0.5))
    except (TypeError, ValueError):
        return 0.5
    if not 0.0 < threshold <= 1.0:
        return 0.5
    return threshold


def set_wake_word(phrase: str, model: str = "", threshold: float = 0.5) -> str:
    """Configure the wake phrase/model/threshold and save config."""
    config = load_config()
    if isinstance(phrase, str) and phrase.strip():
        config["wake_word_phrase"] = phrase.strip()
    if isinstance(model, str) and model.strip():
        config["wake_word_model"] = model.strip()
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = 0.5
    if not 0.0 < threshold <= 1.0:
        threshold = 0.5
    config["wake_word_threshold"] = threshold
    return save_config(config)