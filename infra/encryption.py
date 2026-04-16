"""Fernet encryption for provider credentials at rest."""

from __future__ import annotations

from cryptography.fernet import Fernet

from config import settings

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = settings.gateway_salt_key
        if not key:
            raise RuntimeError("GATEWAY_SALT_KEY must be set for credential encryption")
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()
