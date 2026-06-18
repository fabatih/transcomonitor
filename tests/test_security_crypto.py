"""tests/test_security_crypto.py — Tests for utils/security.py and utils/crypto.py"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import crypto
from utils.security import hash_password, verify_password


# ─────────────────────────────────────────────────────────────────────────
# Security (bcrypt)
# ─────────────────────────────────────────────────────────────────────────

class TestSecurity:
    def test_hash_roundtrip(self):
        h = hash_password("hunter2")
        assert verify_password("hunter2", h)
        assert not verify_password("hunter2!", h)

    def test_unicode_password(self):
        h = hash_password("àéîôü€")
        assert verify_password("àéîôü€", h)

    def test_different_salts_each_call(self):
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2  # bcrypt salt differs
        assert verify_password("same", h1)
        assert verify_password("same", h2)

    def test_verify_invalid_hash_returns_false(self):
        assert not verify_password("any", "not-a-bcrypt-hash")
        assert not verify_password("any", "")


# ─────────────────────────────────────────────────────────────────────────
# Crypto (Fernet)
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolate_crypto(monkeypatch):
    """Reset crypto module state for each test."""
    crypto.reset_for_testing()
    yield
    crypto.reset_for_testing()


class TestCrypto:
    def test_without_key_falls_back_to_plaintext(self, monkeypatch):
        monkeypatch.delenv("DB_ENCRYPTION_KEY", raising=False)
        assert not crypto.is_encryption_available()
        assert crypto.encrypt_value("secret") == "secret"
        assert crypto.decrypt_value("secret") == "secret"

    def test_with_key_roundtrip(self, monkeypatch):
        monkeypatch.setenv("DB_ENCRYPTION_KEY", "my-super-secret-passphrase")
        assert crypto.is_encryption_available()
        token = crypto.encrypt_value("hello-world")
        assert token.startswith("gAAAAA"), "Fernet tokens start with gAAAAA"
        assert token != "hello-world"
        assert crypto.decrypt_value(token) == "hello-world"

    def test_decrypt_plaintext_passthrough(self, monkeypatch):
        """Decrypt of a non-Fernet value returns it unchanged (legacy compat)."""
        monkeypatch.setenv("DB_ENCRYPTION_KEY", "key")
        assert crypto.decrypt_value("legacy-plaintext-value") == "legacy-plaintext-value"

    def test_sensitive_keys_aws_standard_naming(self):
        # transcomonitor uses AWS standard naming (not icd11pycode's AWS_KEY_SECRET)
        assert crypto.is_sensitive_key("AWS_SECRET_ACCESS_KEY")
        assert crypto.is_sensitive_key("WHO_CLIENT_SECRET")
        assert crypto.is_sensitive_key("MISTRAL_API_KEY")
        assert crypto.is_sensitive_key("DATABASE_URL")
        # Non-sensitive
        assert not crypto.is_sensitive_key("AWS_ACCESS_KEY_ID")  # ID is public
        assert not crypto.is_sensitive_key("WHO_CLIENT_ID")
        assert not crypto.is_sensitive_key("S3_BUCKET")
        assert not crypto.is_sensitive_key("S3_REGION")
