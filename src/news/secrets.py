"""Encryption at rest for provider keys (AES-256-GCM). The key comes from
NEWS_SECRET_KEY (32 bytes, base64); encrypted values are never returned by
any API or logged."""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecretsUnavailable(Exception):
    pass


def _key() -> bytes:
    raw = os.environ.get("NEWS_SECRET_KEY", "")
    try:
        key = base64.b64decode(raw)
    except ValueError as e:
        raise SecretsUnavailable("NEWS_SECRET_KEY is not valid base64") from e
    if len(key) != 32:
        raise SecretsUnavailable("NEWS_SECRET_KEY must be 32 bytes, base64-encoded")
    return key


def encrypt(plain: str) -> str:
    nonce = os.urandom(12)
    return "v1:" + base64.b64encode(nonce + AESGCM(_key()).encrypt(nonce, plain.encode(), None)).decode()


def decrypt(token: str) -> str:
    if not token.startswith("v1:"):
        raise SecretsUnavailable("Unknown secret format")
    blob = base64.b64decode(token[3:])
    return AESGCM(_key()).decrypt(blob[:12], blob[12:], None).decode()
