"""Decrypt CryptoJS-style ``Salted__`` AES blobs returned by tonviewer.com.

tonviewer's frontend proxy (``https://tonviewer.com/api/tonapi/v2/...``) wraps
its tonapi responses in CryptoJS-compatible AES output (OpenSSL EVP_BytesToKey
with MD5, AES-256-CBC). The browser decrypts with a hardcoded passphrase before
handing the JSON to the React app. We replicate that path here so we can use
the proxy from a backend.

The default passphrase is the one observed in ``pages/_app-*.js`` at the time
of writing; if tonviewer rotates it, override it via
``TON_PNL_TONVIEWER_PASSPHRASE``.
"""

from __future__ import annotations

import base64
import hashlib

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


def _evp_bytes_to_key(password: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16) -> tuple[bytes, bytes]:
    """OpenSSL EVP_BytesToKey with MD5 — what CryptoJS uses by default."""

    out = b""
    prev = b""
    while len(out) < key_len + iv_len:
        prev = hashlib.md5(prev + password + salt).digest()
        out += prev
    return out[:key_len], out[key_len : key_len + iv_len]


def decrypt_tonviewer_payload(body: str, passphrase: str) -> bytes:
    """Decrypt a base64 ``Salted__...`` blob into raw bytes (typically JSON).

    Raises ``ValueError`` on bad input so callers can fall through to a normal
    JSON parse path (e.g. when the proxy upstream returns an unencrypted error).
    """

    try:
        raw = base64.b64decode(body, validate=False)
    except Exception as exc:  # pragma: no cover — base64 input is generally well-formed
        raise ValueError(f"not a base64 blob: {exc}") from exc
    if len(raw) < 16 or raw[:8] != b"Salted__":
        raise ValueError("missing OpenSSL Salted__ prefix")
    salt = raw[8:16]
    ciphertext = raw[16:]
    key, iv = _evp_bytes_to_key(passphrase.encode("utf-8"), salt)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    try:
        return unpad(cipher.decrypt(ciphertext), AES.block_size)
    except (ValueError, KeyError) as exc:
        raise ValueError(f"AES decrypt failed: {exc}") from exc
