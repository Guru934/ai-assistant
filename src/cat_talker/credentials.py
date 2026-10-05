"""OS-backed API-key credential storage (secret-service keyring).

The Gemini API key is never persisted as plaintext by this application:
it lives in the desktop secret store (GNOME Keyring via Secret Service
on this machine) under a fixed service/account name. config.json keeps
no key material; only unrelated settings live there.

Contract (all functions; never raises, never logs key material):
- available()            backend importable (keyring present)
- backend_working()      a live round-trip-capable store (best effort)
- get_key()              stored key or None (None also when unavailable)
- save_key(key)          (ok, message); False never writes anything
- delete_key()           (ok, message); missing key counts as removed
- key_source()           "secure-store" | "none" (env/legacy handled by
                         config.py, which defines full precedence)

Security rules enforced here, not by callers:
- No custom encryption, no master keys, no derived keys.
- Backend exceptions are sanitized: messages never include key material
  (or backend error text that could echo it).
- A failed secure write never falls back to plaintext anywhere.
"""

from cat_talker.logging_config import get_logger

logger = get_logger("cat_talker.credentials")

SERVICE_NAME = "cat-talker"
ACCOUNT_NAME = "gemini-api-key"


class CredentialError(Exception):
    """Secure-store failure. Message never contains key material."""


def _backend():
    """Return the keyring module, or None when not installed.

    Single indirection point so tests can substitute a fake without
    touching the real user keyring.
    """
    try:
        import keyring
        return keyring
    except Exception:
        return None


def available() -> bool:
    """True when a credential-store library is importable."""
    return _backend() is not None


def backend_working() -> bool:
    """Best-effort liveness check (no key material involved)."""
    kr = _backend()
    if kr is None:
        return False
    try:
        kr.get_password(SERVICE_NAME, "__cat_talker_probe__")
        return True
    except Exception as e:
        logger.warning("credential store unreachable: %s",
                       _safe_error(e))
        return False


def _safe_error(exc, redact=()) -> str:
    """One-line backend error with secret-shaped content removed.

    `redact`: known secret strings (e.g. the key being saved) replaced
    with *** wherever they appear - a backend must never be able to make
    us log key material by echoing it in an error.
    """
    try:
        text = str(exc) or repr(exc)
    except Exception:
        return "credential backend error"
    for secret in redact:
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "***")
    return " ".join(text.split())[:200] or "credential backend error"


def get_key():
    """Stored API key, or None (missing, unavailable, or any failure)."""
    kr = _backend()
    if kr is None:
        return None
    try:
        value = kr.get_password(SERVICE_NAME, ACCOUNT_NAME)
    except Exception as e:
        logger.warning("credential read failed: %s", _safe_error(e))
        return None
    if not isinstance(value, str) or not value:
        return None
    return value


def save_key(key) -> tuple:
    """Store the key. Returns (True, msg) / (False, error-msg).

    False performs no write anywhere: callers must not fall back to
    plaintext on their own.
    """
    if not isinstance(key, str) or not key.strip():
        return False, "Error: API key must not be empty."
    kr = _backend()
    if kr is None:
        return False, ("Error: secure credential storage is unavailable "
                       "(keyring backend missing). The key was NOT saved; "
                       "install keyring + SecretStorage for this desktop.")
    try:
        kr.set_password(SERVICE_NAME, ACCOUNT_NAME, key.strip())
    except Exception as e:
        logger.warning("credential write failed: %s",
                       _safe_error(e, redact=(key.strip(),)))
        return False, ("Error: secure credential storage failed; "
                       "the key was NOT saved.")
    try:
        check = kr.get_password(SERVICE_NAME, ACCOUNT_NAME)
    except Exception as e:
        logger.warning("credential verify failed: %s",
                       _safe_error(e, redact=(key.strip(),)))
        return False, ("Error: could not verify the stored key; "
                       "the key was NOT saved.")
    if check != key.strip():
        return False, ("Error: stored key verification mismatch; "
                       "the key was NOT saved.")
    return True, "API key stored securely."


def delete_key() -> tuple:
    """Remove the key. Missing counts as removed. Never raises."""
    kr = _backend()
    if kr is None:
        return False, ("Error: secure credential storage is unavailable "
                       "(keyring backend missing).")
    try:
        kr.delete_password(SERVICE_NAME, ACCOUNT_NAME)
    except Exception as e:
        message = _safe_error(e)
        # keyring raises PasswordDeleteError when nothing is stored.
        if "no password" in message.lower() or "not found" in message.lower():
            return True, "No stored API key found; nothing to remove."
        # Verify-then-report: absence (however reported) is success.
        try:
            if kr.get_password(SERVICE_NAME, ACCOUNT_NAME) in (None, ""):
                return True, "No stored API key found; nothing to remove."
        except Exception:
            pass
        logger.warning("credential delete failed: %s", message)
        return False, ("Error: could not remove the stored key; "
                       "secure storage may be locked.")
    try:
        if kr.get_password(SERVICE_NAME, ACCOUNT_NAME) not in (None, ""):
            return False, ("Error: the stored key is still present after "
                           "removal.")
    except Exception:
        pass
    return True, "Stored API key removed."


def key_source() -> str:
    """"secure-store" when a key is retrievable, else "none"."""
    return "secure-store" if get_key() else "none"
