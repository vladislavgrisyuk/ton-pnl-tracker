"""Round-trip tests for the tonviewer AES decryption helper.

We do not rely on a live tonviewer response here — instead we encrypt a known
plaintext with CryptoJS-compatible OpenSSL framing and verify the helper
decrypts it back. This keeps the test deterministic and offline.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from ton_pnl.tonviewer_crypt import decrypt_tonviewer_payload


def _encrypt_cryptojs(plaintext: bytes, passphrase: str, *, salt: bytes | None = None) -> str:
    """Encrypt with the same OpenSSL EVP_BytesToKey + AES-256-CBC scheme CryptoJS uses."""

    if salt is None:
        salt = secrets.token_bytes(8)
    key_iv = b""
    prev = b""
    while len(key_iv) < 48:
        prev = hashlib.md5(prev + passphrase.encode("utf-8") + salt).digest()
        key_iv += prev
    key, iv = key_iv[:32], key_iv[32:48]
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))
    return base64.b64encode(b"Salted__" + salt + ciphertext).decode("ascii")


def test_decrypts_round_trip() -> None:
    plaintext = b'{"hello":"world","nested":{"value":42}}'
    encrypted = _encrypt_cryptojs(plaintext, "tv22-asrr11")
    assert decrypt_tonviewer_payload(encrypted, "tv22-asrr11") == plaintext


def test_decrypts_known_salt() -> None:
    # Pinning the salt makes the ciphertext deterministic and protects against
    # accidental changes to the EVP_BytesToKey implementation.
    encrypted = _encrypt_cryptojs(b"ton", "secret", salt=b"\x00" * 8)
    assert decrypt_tonviewer_payload(encrypted, "secret") == b"ton"


def test_rejects_bad_prefix() -> None:
    with pytest.raises(ValueError, match="Salted__"):
        decrypt_tonviewer_payload(base64.b64encode(b"NotSaltednope").decode(), "secret")


def test_rejects_wrong_passphrase() -> None:
    encrypted = _encrypt_cryptojs(b"payload", "right")
    with pytest.raises(ValueError):
        decrypt_tonviewer_payload(encrypted, "wrong")
