"""Cryptographic helpers: random tokens, hashing, canonical payload hashing, secret encryption."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationError


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """One-way hash for bearer-like secrets stored server-side (session ids, CSRF, OAuth binding)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for hashing (sorted keys, no whitespace, UTF-8 preserved)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def compute_payload_hash(action_type: str, target_integration: str, payload: dict[str, Any]) -> str:
    """Hash that an approval is bound to. Any change to the executable content changes it."""
    return sha256_hex(canonical_json({"action_type": action_type, "target": target_integration, "payload": payload}))


class SecretBox:
    """Fernet-based encryption for token material at rest. Supports key rotation via MultiFernet."""

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ConfigurationError("TOKEN_ENCRYPTION_KEYS is not configured")
        try:
            self._fernet = MultiFernet([Fernet(k.encode("ascii")) for k in keys])
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("TOKEN_ENCRYPTION_KEYS contains an invalid Fernet key") from exc

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> SecretBox:
        settings = settings or get_settings()
        raw = settings.token_encryption_keys.get_secret_value() if settings.token_encryption_keys else ""
        return cls([k.strip() for k in raw.split(",") if k.strip()])

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, token: bytes) -> bytes:
        try:
            return self._fernet.decrypt(token)
        except InvalidToken as exc:
            raise ConfigurationError("Stored secret cannot be decrypted with the configured keys") from exc

    def encrypt_str(self, plaintext: str) -> bytes:
        return self.encrypt(plaintext.encode("utf-8"))

    def decrypt_str(self, token: bytes) -> str:
        return self.decrypt(token).decode("utf-8")


def generate_fernet_key() -> str:
    return Fernet.generate_key().decode("ascii")
