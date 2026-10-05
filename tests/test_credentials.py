"""Focused tests for OS-backed API-key credential storage.

The real user keyring is never touched: every test substitutes an
in-memory fake (or a failing backend) for the keyring module. Config and
memory files are redirected into tmp.
"""

import json
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cat_talker.config as config_mod
import cat_talker.credentials as credentials_mod
from cat_talker.credentials import (
    CredentialError,
    delete_key,
    get_key,
    save_key,
)


class FakeKeyring:
    def __init__(self, fail=None):
        self.store = {}
        self.fail = fail  # exception instance raised by every call
        self.calls = []

    def _maybe_fail(self, what):
        self.calls.append(what)
        if self.fail is not None:
            raise self.fail

    def get_password(self, service, account):
        self._maybe_fail("get")
        return self.store.get((service, account))

    def set_password(self, service, account, value):
        self._maybe_fail("set")
        self.store[(service, account)] = value

    def delete_password(self, service, account):
        self._maybe_fail("delete")
        try:
            del self.store[(service, account)]
        except KeyError:
            raise LookupError("no password stored")


@pytest.fixture
def fake_backend(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(credentials_mod, "_backend", lambda: fake)
    return fake


@pytest.fixture
def isolated_config(tmp_path, monkeypatch, fake_backend):
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        str(tmp_path / "config.json"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return tmp_path, fake_backend


def _raw_config(tmp_path):
    with open(tmp_path / "config.json", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Secure save/get/delete + status
# ---------------------------------------------------------------------------

def test_save_get_delete_roundtrip(fake_backend):
    assert fake_backend.store == {}
    ok, _ = save_key("sk-test-123")
    assert ok is True
    assert get_key() == "sk-test-123"
    ok, _ = delete_key()
    assert ok is True
    assert get_key() is None


def test_empty_key_rejected_without_backend_call(fake_backend):
    assert save_key("   ")[0] is False
    assert save_key(None)[0] is False
    assert fake_backend.calls == []


def test_delete_missing_counts_as_removed(fake_backend):
    ok, message = delete_key()
    assert ok is True
    assert "nothing to remove" in message


def test_configured_status(isolated_config):
    assert config_mod.is_api_key_configured() is False
    assert config_mod.credential_status()["source"] == "missing"
    assert config_mod.set_api_key("sk-a") == "API key stored securely."
    assert config_mod.is_api_key_configured() is True
    assert config_mod.get_api_key() == "sk-a"
    assert config_mod.credential_status()["source"] == "secure-store"


# ---------------------------------------------------------------------------
# Migration from legacy plaintext config
# ---------------------------------------------------------------------------

def test_migration_moves_key_and_scrubs_file(isolated_config):
    tmp_path, _ = isolated_config
    with open(tmp_path / "config.json", "w", encoding="utf-8") as f:
        json.dump({"api_key": "sk-legacy", "preferred_monitor": "m0",
                   "wake_word_enabled": True}, f)
    status, _ = config_mod.migrate_legacy_api_key()
    assert status == "migrated"
    assert get_key() == "sk-legacy"
    raw = _raw_config(tmp_path)
    assert raw.get("api_key") in (None, "")
    assert raw["preferred_monitor"] == "m0"
    assert raw["wake_word_enabled"] is True


def test_migration_is_idempotent(isolated_config):
    tmp_path, fake_backend = isolated_config
    with open(tmp_path / "config.json", "w", encoding="utf-8") as f:
        json.dump({"api_key": "sk-x"}, f)
    assert config_mod.migrate_legacy_api_key()[0] == "migrated"
    assert config_mod.migrate_legacy_api_key()[0] == "nothing-to-migrate"
    assert fake_backend.store[
        (credentials_mod.SERVICE_NAME,
         credentials_mod.ACCOUNT_NAME)] == "sk-x"


def test_get_api_key_auto_migrates(isolated_config):
    tmp_path, _ = isolated_config
    with open(tmp_path / "config.json", "w", encoding="utf-8") as f:
        json.dump({"api_key": "sk-auto"}, f)
    assert config_mod.get_api_key() == "sk-auto"
    assert _raw_config(tmp_path).get("api_key") in (None, "")
    assert get_key() == "sk-auto"


# ---------------------------------------------------------------------------
# Failure honesty: never plaintext, never silent
# ---------------------------------------------------------------------------

def test_backend_missing_saves_nothing(isolated_config, monkeypatch):
    tmp_path, _ = isolated_config
    monkeypatch.setattr(credentials_mod, "_backend", lambda: None)
    message = config_mod.set_api_key("sk-nope")
    assert message.startswith("Error"), message
    assert not os.path.exists(tmp_path / "config.json") or \
        _raw_config(tmp_path).get("api_key", "") == ""


def test_backend_failure_keeps_legacy_untouched(isolated_config, monkeypatch):
    tmp_path, _ = isolated_config
    with open(tmp_path / "config.json", "w", encoding="utf-8") as f:
        json.dump({"api_key": "sk-keep"}, f)
    monkeypatch.setattr(credentials_mod, "_backend", lambda: None)
    status, message = config_mod.migrate_legacy_api_key()
    assert status == "backend-unavailable"
    assert _raw_config(tmp_path).get("api_key") == "sk-keep"
    assert "Error" in message
    # Degraded but working: the legacy value still unlocks the app, and
    # the status says so honestly instead of pretending secure storage.
    assert config_mod.get_api_key() == "sk-keep"
    assert config_mod.credential_status()["source"] == "legacy-plaintext"


def test_failing_backend_reports_error_not_plaintext(isolated_config,
                                                     monkeypatch, caplog):
    tmp_path, _ = isolated_config
    monkeypatch.setattr(credentials_mod, "_backend",
                        lambda: FakeKeyring(fail=OSError("store locked")))
    with caplog.at_level(logging.WARNING):
        assert config_mod.set_api_key("sk-SUPER-SECRET-99").startswith("Error")
    assert get_key() is None
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "sk-SUPER-SECRET-99" not in blob
    for record in caplog.records:
        assert "sk-SUPER-SECRET-99" not in str(record.args)


def test_raw_key_never_in_credential_errors(monkeypatch, caplog):
    class _ExplodingBackend:
        def get_password(self, *a):
            raise RuntimeError("backend unavailable: locked")
        def set_password(self, *a):
            raise RuntimeError("cannot store: sk-LEAK-2 echoed by backend")
        def delete_password(self, *a):
            raise RuntimeError("cannot delete: collection locked")

    monkeypatch.setattr(credentials_mod, "_backend", lambda: _ExplodingBackend())
    with caplog.at_level(logging.WARNING):
        assert get_key() is None
        assert save_key("sk-LEAK-2")[0] is False
        assert delete_key()[0] is False
    blob = "\n".join([r.getMessage() for r in caplog.records]
                     + [str(r.args) for r in caplog.records])
    # The write path redacts the key it was given, even when the backend
    # echoes it back inside an error.
    assert "sk-LEAK-2" not in blob, blob


# ---------------------------------------------------------------------------
# Environment precedence + settings integration
# ---------------------------------------------------------------------------

def test_environment_fallback_and_precedence(isolated_config, monkeypatch):
    tmp_path, fake_backend = isolated_config
    monkeypatch.setenv("GEMINI_API_KEY", "sk-env")
    assert config_mod.get_api_key() == "sk-env"
    assert config_mod.credential_status()["source"] == "environment"
    config_mod.set_api_key("sk-secure")
    # Stored credential keeps its established first position.
    assert config_mod.get_api_key() == "sk-secure"
    assert config_mod.credential_status()["source"] == "secure-store"


def test_settings_roundtrip_uses_secure_store(isolated_config):
    from cat_talker import settings as settings_mod
    values = dict(settings_mod.load_snapshot())
    assert values["api_key_configured"] is False
    values.update(settings_mod.load_snapshot())
    values["api_key_new"] = "sk-ui-1"
    ok, _ = settings_mod.apply_settings(values)
    assert ok is True
    assert config_mod.get_api_key() == "sk-ui-1"
    raw_path = isolated_config[0] / "config.json"
    with open(raw_path, encoding="utf-8") as f:
        assert "sk-ui-1" not in f.read()
    values = dict(settings_mod.load_snapshot())
    assert values["api_key_configured"] is True
    values["api_key_clear"] = True
    values["api_key_new"] = ""
    ok, _ = settings_mod.apply_settings(values)
    assert ok is True
    assert config_mod.is_api_key_configured() is False
