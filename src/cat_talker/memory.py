"""Explicit user preference store (standard library only).

Architecture:
    Gemini -> get/set/delete_preference [tools.py] -> this module
    -> the existing local memory file (~/.config/cat-talker/memory.json,
    shared with save_user_preference; no second database is created).

This is NOT an autonomous memory system: there is no extraction,
no summarization, no background writes. Every write is an explicit
tool call with a key from ALLOWED_KEYS. Anything merely mentioned in
conversation is never persisted.
"""

import json
import os
from datetime import datetime, timezone

# Small closed key set: validation stays consistent and the design is
# easy to extend by adding one entry here (plus any value rule below).
ALLOWED_KEYS = frozenset({
    "preferred_name",
    "preferred_language",
    "temperature_unit",
    "weather_location",
    "response_style",
})

TEMPERATURE_UNITS = ("C", "F")

# Conservative bound: preferences are short human strings, not documents.
MAX_VALUE_LEN = 200

DEFAULT_MEMORY_PATH = os.path.expanduser("~/.config/cat-talker/memory.json")


def normalize_key(key) -> str:
    """Validate and normalize a preference key. Raises ValueError."""
    if not isinstance(key, str):
        raise ValueError("Preference error: key must be text.")
    cleaned = key.strip().lower()
    if cleaned not in ALLOWED_KEYS:
        allowed = ", ".join(sorted(ALLOWED_KEYS))
        raise ValueError(
            f"Preference error: unknown key '{key.strip()}'. "
            f"Allowed keys: {allowed}.")
    return cleaned


def normalize_value(key: str, value) -> str:
    """Validate and normalize a preference value. Raises ValueError."""
    if not isinstance(value, str):
        raise ValueError("Preference error: value must be text.")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Preference error: value must not be empty.")
    if len(cleaned) > MAX_VALUE_LEN:
        raise ValueError(
            f"Preference error: value too long "
            f"(max {MAX_VALUE_LEN} characters).")
    if key == "temperature_unit":
        upper = cleaned.upper()
        if upper not in TEMPERATURE_UNITS:
            raise ValueError(
                "Preference error: temperature_unit must be 'C' or 'F'.")
        return upper
    return cleaned


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PreferenceStore:
    """Validated read/write/delete over the shared local memory file.

    The file holds a plain JSON object also used by save_user_preference;
    unknown keys are preserved untouched. Preference entries are stored
    as {"value": ..., "updated_at": ...} so reads stay simple while the
    write time is recorded.
    """

    def __init__(self, path: str | None = None):
        self.path = path or DEFAULT_MEMORY_PATH

    # -- storage ----------------------------------------------------
    def _read_all(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            raise RuntimeError(
                f"Preference error: stored data unreadable: {e}") from e
        if not isinstance(data, dict):
            raise RuntimeError("Preference error: stored data malformed.")
        return data

    def _write_all(self, data: dict):
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except OSError as e:
            raise RuntimeError(
                f"Preference error: could not save: {e}") from e

    # -- public API --------------------------------------------------
    def get(self, key: str) -> dict:
        """Return {"key", "value", "updated_at"} or raise LookupError."""
        norm = normalize_key(key)
        entry = self._read_all().get(norm)
        if not isinstance(entry, dict) or "value" not in entry:
            raise LookupError(
                f"No preference stored for '{norm}'.")
        return {"key": norm,
                "value": entry["value"],
                "updated_at": entry.get("updated_at", "unknown")}

    def set(self, key: str, value: str) -> dict:
        """Create or update a preference (upsert). Returns the entry."""
        norm = normalize_key(key)
        cleaned = normalize_value(norm, value)
        data = self._read_all()
        entry = {"value": cleaned, "updated_at": _utc_now_iso()}
        data[norm] = entry
        self._write_all(data)
        return {"key": norm, **entry}

    def delete(self, key: str) -> bool:
        """Delete a preference. True if one existed, False if no-op."""
        norm = normalize_key(key)
        data = self._read_all()
        if norm not in data:
            return False
        del data[norm]
        self._write_all(data)
        return True


_DEFAULT_STORE = PreferenceStore()


def get_preference(key: str) -> str:
    """Public tool: read an explicitly stored preference. Never raises."""
    try:
        entry = _DEFAULT_STORE.get(key)
        return f"{entry['key']} = {entry['value']}"
    except (ValueError, LookupError, RuntimeError) as e:
        return str(e)
    except Exception as e:
        return f"Preference error: {e}"


def set_preference(key: str, value: str) -> str:
    """Public tool: explicitly store a preference. Never raises."""
    try:
        entry = _DEFAULT_STORE.set(key, value)
        return f"Saved {entry['key']} = {entry['value']}"
    except (ValueError, RuntimeError) as e:
        return str(e)
    except Exception as e:
        return f"Preference error: {e}"


def delete_preference(key: str) -> str:
    """Public tool: explicitly delete a preference. Never raises."""
    try:
        if _DEFAULT_STORE.delete(key):
            norm = key.strip().lower()
            return f"Deleted preference '{norm}'."
        norm = key.strip().lower() if isinstance(key, str) else key
        return f"No preference stored for '{norm}'; nothing to delete."
    except (ValueError, RuntimeError) as e:
        return str(e)
    except Exception as e:
        return f"Preference error: {e}"
