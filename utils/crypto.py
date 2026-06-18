"""
utils/crypto.py — Symmetric Fernet encryption for sensitive config values.

Pattern repris d'icd11pycode/utils/crypto.py avec adaptation des noms standard
AWS (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY) au lieu des noms legacy
icd11pycode (AWS_KEY_ID / AWS_KEY_SECRET).

DB_ENCRYPTION_KEY environment variable derives a Fernet (AES-128-CBC +
HMAC-SHA256) cipher. If unset, encryption is disabled and values are stored
plaintext (with a startup warning).

Encrypted tokens are easily identified by their `gAAAAA` prefix (Fernet
spec), so legacy plaintext / base64 values can coexist during migration.
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional

_FERNET_PREFIX = b"gAAAAA"

_fernet = None  # type: ignore
_encryption_available: bool = False
_init_done: bool = False


def _init_fernet() -> None:
    """Lazy-init Fernet cipher from env var. Idempotent."""
    global _fernet, _encryption_available, _init_done
    if _init_done:
        return
    raw_key = os.environ.get("DB_ENCRYPTION_KEY", "").strip()
    if not raw_key:
        print("[WARNING] DB_ENCRYPTION_KEY not set — secrets stored unencrypted in DB")
        _encryption_available = False
        _init_done = True
        return
    try:
        from cryptography.fernet import Fernet
        # Derive a deterministic 32-byte key from the user passphrase
        derived = hashlib.sha256(raw_key.encode()).digest()
        key = base64.urlsafe_b64encode(derived)
        _fernet = Fernet(key)
        _encryption_available = True
    except ImportError:
        print("[WARNING] cryptography not installed — secrets stored unencrypted")
        _encryption_available = False
    _init_done = True


def is_encryption_available() -> bool:
    _init_fernet()
    return _encryption_available


def encrypt_value(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns Fernet token as UTF-8 string.
    If encryption is not available, returns plaintext unchanged."""
    _init_fernet()
    if not _encryption_available or _fernet is None:
        return plaintext
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a Fernet token back to plaintext.
    If the value is not a Fernet token (legacy plaintext), returns it as-is.
    Used during migration from previously unencrypted config storage."""
    _init_fernet()
    if not _encryption_available or _fernet is None:
        return ciphertext
    try:
        raw = ciphertext.encode("utf-8")
        if raw.startswith(_FERNET_PREFIX):
            return _fernet.decrypt(raw).decode("utf-8")
    except Exception:
        pass
    return ciphertext


# ─────────────────────────────────────────────────────────────────────────
# Sensitive keys — adapted to AWS standard naming (vs icd11pycode legacy)
# ─────────────────────────────────────────────────────────────────────────

SENSITIVE_CONFIG_KEYS = frozenset({
    # WHO
    "WHO_CLIENT_SECRET",
    # AWS standard naming (boto3 native)
    "AWS_SECRET_ACCESS_KEY",
    # Mistral (V2)
    "MISTRAL_API_KEY",
    # PostgreSQL (V1) — DSN contains password
    "DATABASE_URL",
    # DB encryption key itself should NOT be stored, but protect if mishandled
    "DB_ENCRYPTION_KEY",
})


def is_sensitive_key(key: str) -> bool:
    return key in SENSITIVE_CONFIG_KEYS


def reset_for_testing() -> None:
    """Reset module state — for tests only."""
    global _fernet, _encryption_available, _init_done
    _fernet = None
    _encryption_available = False
    _init_done = False
