"""Encryption of remote-destination credentials at rest.

A Fernet key is generated once and stored in the data directory with
restrictive permissions. Destination passwords/keys are stored encrypted in
the database, never in plaintext.
"""
from __future__ import annotations

import os
from functools import lru_cache

from cryptography.fernet import Fernet

from .config import get_settings


@lru_cache
def _fernet() -> Fernet:
    settings = get_settings()
    path = settings.secret_key_path
    if path.exists():
        key = path.read_bytes()
    else:
        key = Fernet.generate_key()
        path.write_bytes(key)
        os.chmod(path, 0o600)
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    if plaintext is None:
        plaintext = ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
