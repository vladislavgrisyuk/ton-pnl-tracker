"""Extract and normalize swap actions from TonAPI events.

The JettonSwap action exposed by TonAPI bundles together TON↔jetton and
jetton↔jetton swaps from many DEXes (STON.fi, DeDust, …) behind a single shape:

    {
      "dex": "stonfi",
      "amount_in":  "<jetton_in raw amount, smallest units>",
      "amount_out": "<jetton_out raw amount, smallest units>",
      "ton_in":  <nanotons given by user>  | omitted,
      "ton_out": <nanotons received by user> | omitted,
      "user_wallet": ..., "router": ...,
      "jetton_master_in":  { address, name, symbol, decimals, ... } | omitted,
      "jetton_master_out": { address, name, symbol, decimals, ... } | omitted,
    }

We turn each one into a :class:`Swap` with explicit ``asset_in`` and ``asset_out``
sides where TON is represented by the sentinel :data:`TON_ASSET_ID`.
"""

from __future__ import annotations

import logging
from typing import Any

from .models import TON_ASSET_ID, Swap, TokenInfo

log = logging.getLogger(__name__)

TON_DECIMALS = 9
NANO = 10**TON_DECIMALS

# Proxy-TON jetton wrappers used internally by DEX routers (STON.fi v1/v2,
# DeDust, …). On chain they are 1:1 with native TON, so for accounting
# purposes we collapse them into the TON asset id. Stored as lowercase hex
# without the workchain prefix to match :func:`AccountAddress.address`.
PROXY_TON_ADDRESSES = {
    # STON.fi v1 pTON (EQCM3B12QK1e4yZSf8GtBRT0aLMNyEsBc_DhVfRRtOEffLez)
    "0:8cdc1d7640ad5ee32652e7c1ad0514f468b30dc8cb017f0e155f451b4e11f7cc",
    # STON.fi v2 pTON (EQBnFjAn9_hWWatVuCFnFohgHNzx7mdPx_u7Gndaiyaco6IO)
    "0:671963027f7f85659ab55b821671688601cdcf1ee674fc7fbbb1a776a18d34a3",
}


_TON_TOKEN = TokenInfo(
    asset_id=TON_ASSET_ID,
    symbol="TON",
    name="Toncoin",
    decimals=TON_DECIMALS,
    image="https://ton.org/download/ton_symbol.png",
)


def _is_proxy_ton(address: str | None) -> bool:
    if not address:
        return False
    return address.lower() in PROXY_TON_ADDRESSES


def _jetton_token(jetton: dict[str, Any]) -> TokenInfo:
    addr = jetton.get("address") or ""
    if _is_proxy_ton(addr):
        return _TON_TOKEN
    return TokenInfo(
        asset_id=addr,
        symbol=jetton.get("symbol") or "?",
        name=jetton.get("name") or jetton.get("symbol") or addr[:8],
        decimals=int(jetton.get("decimals") or 9),
        image=jetton.get("image"),
    )


def _scale(raw: int, decimals: int) -> float:
    if decimals <= 0:
        return float(raw)
    return raw / (10**decimals)


def _to_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_swap(event: dict[str, Any], action: dict[str, Any]) -> Swap | None:
    """Convert a single ``JettonSwap`` action into a :class:`Swap` if possible.

    Returns ``None`` if the action is malformed or ambiguous (both sides
    missing). NOTE: we intentionally do *not* skip ``status == "failed"``
    actions: STON.fi v2 multi-hop swaps frequently get tagged "failed" by
    tonapi.io because the trace boundary cuts off before the final settlement
    hop, even though the funds did move. Cross-checking the JettonTransfer
    actions in the same trace confirms this. We still skip swaps that have
    zero amounts on either side, which catches genuinely reverted swaps.
    """
    payload = action.get("JettonSwap") or {}
    if not payload:
        return None

    ton_in = _to_int(payload.get("ton_in"))
    ton_out = _to_int(payload.get("ton_out"))
    raw_amount_in = _to_int(payload.get("amount_in"))
    raw_amount_out = _to_int(payload.get("amount_out"))

    jetton_in = payload.get("jetton_master_in") or None
    jetton_out = payload.get("jetton_master_out") or None

    asset_in: TokenInfo | None
    asset_out: TokenInfo | None
    amount_in_raw: int
    amount_out_raw: int

    if ton_in > 0 and not jetton_in:
        # User paid TON to receive a jetton.
        asset_in = _TON_TOKEN
        amount_in_raw = ton_in
        if not jetton_out:
            return None
        asset_out = _jetton_token(jetton_out)
        amount_out_raw = raw_amount_out
    elif ton_out > 0 and not jetton_out:
        # User sold a jetton for TON.
        if not jetton_in:
            return None
        asset_in = _jetton_token(jetton_in)
        amount_in_raw = raw_amount_in
        asset_out = _TON_TOKEN
        amount_out_raw = ton_out
    elif jetton_in and jetton_out:
        # Pure jetton ↔ jetton swap (e.g. routed via TON internally).
        asset_in = _jetton_token(jetton_in)
        amount_in_raw = raw_amount_in
        asset_out = _jetton_token(jetton_out)
        amount_out_raw = raw_amount_out
    else:
        # Some DEXes occasionally produce malformed actions; ignore.
        return None

    if amount_in_raw <= 0 or amount_out_raw <= 0:
        return None

    amount_in = _scale(amount_in_raw, asset_in.decimals)
    amount_out = _scale(amount_out_raw, asset_out.decimals)

    ton_in_value: float | None = None
    ton_out_value: float | None = None
    if ton_in > 0:
        ton_in_value = ton_in / NANO
    elif asset_in.asset_id == TON_ASSET_ID:
        # pTON jetton normalized into TON: its amount IS the TON amount.
        ton_in_value = amount_in
    if ton_out > 0:
        ton_out_value = ton_out / NANO
    elif asset_out.asset_id == TON_ASSET_ID:
        ton_out_value = amount_out

    return Swap(
        timestamp=int(event.get("timestamp") or 0),
        event_id=str(event.get("event_id") or ""),
        dex=str(payload.get("dex") or "unknown"),
        asset_in=asset_in,
        asset_out=asset_out,
        amount_in_raw=amount_in_raw,
        amount_out_raw=amount_out_raw,
        amount_in=amount_in,
        amount_out=amount_out,
        ton_in=ton_in_value,
        ton_out=ton_out_value,
    )


def extract_swaps(events: list[dict[str, Any]], wallet_address: str | None = None) -> list[Swap]:
    """Walk all events and return JettonSwap actions ordered chronologically.

    When ``wallet_address`` is provided we only keep swaps where the action's
    ``user_wallet`` matches it (case-insensitive on the normalized hex form).
    This filters out unrelated actions that landed in a trace involving the
    wallet (e.g. a DEX router contract relaying funds).
    """
    target = (wallet_address or "").lower()
    swaps: list[Swap] = []
    for event in events:
        for action in event.get("actions") or []:
            if action.get("type") != "JettonSwap":
                continue
            payload = action.get("JettonSwap") or {}
            if target:
                user_wallet = (payload.get("user_wallet") or {}).get("address") or ""
                if user_wallet.lower() != target:
                    continue
            swap = normalize_swap(event, action)
            if swap is not None:
                swaps.append(swap)
    swaps.sort(key=lambda s: s.timestamp)
    return swaps
