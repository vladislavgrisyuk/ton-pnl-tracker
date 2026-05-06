"""TON address format helpers.

Tonapi.io returns "raw" addresses (``<workchain>:<account_id_hex>``) while
GeckoTerminal and most explorers want the user-friendly base64url form
(``EQ...`` for bounceable, ``UQ...`` for non-bounceable). The friendly form is
36 bytes laid out as ``[tag, workchain, account_id (32B), crc16 (2B)]``,
encoded base64url.
"""

from __future__ import annotations

import base64

# Tag byte for bounceable user-friendly addresses on mainnet (testnet flips bit 0x80).
TAG_BOUNCEABLE = 0x11
TAG_NON_BOUNCEABLE = 0x51


def _crc16_xmodem(data: bytes) -> int:
    """CRC-16/XMODEM as used by TON friendly addresses (poly 0x1021, init 0)."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def raw_to_friendly(raw_address: str, *, bounceable: bool = True) -> str:
    """Convert ``0:<hex>`` raw form into the ``EQ.../UQ...`` friendly form."""
    if ":" not in raw_address:
        # Already friendly or unknown format — leave it alone.
        return raw_address
    workchain_str, account_hex = raw_address.split(":", 1)
    workchain = int(workchain_str)
    account = bytes.fromhex(account_hex)
    if len(account) != 32:
        raise ValueError(f"unexpected account hash length {len(account)} for {raw_address!r}")
    tag = TAG_BOUNCEABLE if bounceable else TAG_NON_BOUNCEABLE
    workchain_byte = workchain & 0xFF  # signed -> unsigned wrap (0xFF == masterchain)
    body = bytes([tag, workchain_byte]) + account
    checksum = _crc16_xmodem(body).to_bytes(2, "big")
    return base64.urlsafe_b64encode(body + checksum).decode("ascii")


def friendly_to_raw(friendly: str) -> str:
    """Convert ``EQ.../UQ...`` friendly form back into ``0:<hex>``."""
    if ":" in friendly:
        return friendly
    decoded = base64.urlsafe_b64decode(friendly)
    if len(decoded) != 36:
        raise ValueError(f"friendly address {friendly!r} does not decode to 36 bytes")
    workchain = decoded[1]
    if workchain >= 0x80:  # masterchain (-1) wraps as 0xFF
        workchain -= 0x100
    account_hex = decoded[2:34].hex()
    return f"{workchain}:{account_hex}"


def to_friendly_safe(asset_id: str) -> str | None:
    """Best-effort conversion to friendly form; returns None on failure."""
    try:
        return raw_to_friendly(asset_id)
    except (ValueError, IndexError):
        return None
