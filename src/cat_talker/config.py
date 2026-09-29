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


def set_voice_approval(enabled: bool) -> str:
    """Set voice approval enabled setting and save config."""
    config = load_config()
    config["voice_approval_enabled"] = enabled
    return save_config(config)