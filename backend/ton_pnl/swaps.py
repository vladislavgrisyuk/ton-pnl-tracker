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
import re
from typing import Any

from .address import friendly_to_raw
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
    "0:8cdc1d7640ad5ee326527fc1ad0514f468b30dc84b0173f0e155f451b4e11f7c",
    # STON.fi v2 pTON (EQBnFjAn9_hWWatVuCFnFohgHNzx7mdPx_u7Gndaiyaco6IO)
    "0:67163027f7f85659ab55b821671688601cdcf1ee674fc7fbbb1a775a8b269ca3",
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
    return _normalize_address(address).lower() in PROXY_TON_ADDRESSES


def _normalize_address(address: str) -> str:
    if ":" in address:
        return address
    try:
        return friendly_to_raw(address)
    except (ValueError, IndexError):
        return address


def _jetton_token(jetton: dict[str, Any]) -> TokenInfo:
    addr = _normalize_address(jetton.get("address") or "")
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


def _extract_ton_attached(event: dict[str, Any], wallet_address: str) -> int:
    target = wallet_address.lower()
    for action in event.get("actions") or []:
        if action.get("type") != "SmartContractExec":
            continue
        payload = action.get("SmartContractExec") or {}
        executor = (payload.get("executor") or {}).get("address") or ""
        if executor.lower() != target:
            continue
        attached = _to_int(payload.get("ton_attached"))
        if attached > 0:
            return attached
    # Fallback for pool events: the executor is the pool, not the user.
    # Look for a TonTransfer from the user to the event's account (the pool).
    pool_address = (event.get("account") or {}).get("address") or ""
    if pool_address:
        pool = pool_address.lower()
        for action in event.get("actions") or []:
            if action.get("type") != "TonTransfer":
                continue
            payload = action.get("TonTransfer") or {}
            sender = (payload.get("sender") or {}).get("address") or ""
            recipient = (payload.get("recipient") or {}).get("address") or ""
            if sender.lower() == target and recipient.lower() == pool:
                amount = _to_int(payload.get("amount"))
                if amount > 0:
                    return amount
    # Fallback 3: pool events (e.g. DeDust) where TON reach the pool via
    # SmartContractExec — the executor is the vault/router, not the user,
    # and ton_attached are the user's TON. We intentionally do NOT enforce
    # contract==pool because TonAPI may return friendly vs raw addresses.
    for action in event.get("actions") or []:
        if action.get("type") != "SmartContractExec":
            continue
        payload = action.get("SmartContractExec") or {}
        attached = _to_int(payload.get("ton_attached"))
        if attached > 0:
            return attached
    return 0


def _extract_ton_received(event: dict[str, Any], wallet_address: str, sender_address: str) -> int:
    target = wallet_address.lower()
    sender = sender_address.lower()
    total = 0
    for action in event.get("actions") or []:
        if action.get("type") != "TonTransfer":
            continue
        payload = action.get("TonTransfer") or {}
        ton_sender = (payload.get("sender") or {}).get("address") or ""
        recipient = (payload.get("recipient") or {}).get("address") or ""
        if ton_sender.lower() == sender and recipient.lower() == target:
            total += _to_int(payload.get("amount"))
    return total


def normalize_flawed_transfer_buy(
    event: dict[str, Any], action: dict[str, Any], wallet_address: str
) -> Swap | None:
    payload = action.get("FlawedJettonTransfer") or {}
    if not payload:
        return None
    recipient = (payload.get("recipient") or {}).get("address") or ""
    if recipient.lower() != wallet_address.lower():
        return None
    ton_attached = _extract_ton_attached(event, wallet_address)
    if ton_attached <= 0:
        return None
    raw_amount_out = _to_int(payload.get("received_amount"))
    if raw_amount_out <= 0:
        return None
    asset_out = _jetton_token(payload.get("jetton") or {})
    amount_out = _scale(raw_amount_out, asset_out.decimals)
    return Swap(
        timestamp=int(event.get("timestamp") or 0),
        event_id=str(event.get("event_id") or ""),
        dex="dedust",
        asset_in=_TON_TOKEN,
        asset_out=asset_out,
        amount_in_raw=ton_attached,
        amount_out_raw=raw_amount_out,
        amount_in=ton_attached / NANO,
        amount_out=amount_out,
        ton_in=ton_attached / NANO,
        ton_out=None,
    )


def normalize_transfer_swap(
    event: dict[str, Any], action: dict[str, Any], wallet_address: str
) -> Swap | None:
    payload = action.get("JettonTransfer") or {}
    if not payload:
        return None
    sender = (payload.get("sender") or {}).get("address") or ""
    recipient = (payload.get("recipient") or {}).get("address") or ""
    raw_jetton_amount = _to_int(payload.get("amount"))
    if raw_jetton_amount <= 0:
        return None
    token = _jetton_token(payload.get("jetton") or {})
    jetton_amount = _scale(raw_jetton_amount, token.decimals)
    if recipient.lower() == wallet_address.lower():
        ton_attached = _extract_ton_attached(event, wallet_address)
        if ton_attached <= 0:
            return None
        return Swap(
            timestamp=int(event.get("timestamp") or 0),
            event_id=str(event.get("event_id") or ""),
            dex="dedust",
            asset_in=_TON_TOKEN,
            asset_out=token,
            amount_in_raw=ton_attached,
            amount_out_raw=raw_jetton_amount,
            amount_in=ton_attached / NANO,
            amount_out=jetton_amount,
            ton_in=ton_attached / NANO,
            ton_out=None,
        )
    if sender.lower() == wallet_address.lower():
        ton_received = _extract_ton_received(event, wallet_address, recipient)
        if ton_received <= 0:
            return None
        return Swap(
            timestamp=int(event.get("timestamp") or 0),
            event_id=str(event.get("event_id") or ""),
            dex="dedust",
            asset_in=token,
            asset_out=_TON_TOKEN,
            amount_in_raw=raw_jetton_amount,
            amount_out_raw=ton_received,
            amount_in=jetton_amount,
            amount_out=ton_received / NANO,
            ton_in=None,
            ton_out=ton_received / NANO,
        )
    return None


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
        has_explicit_swap = any(
            action.get("type") == "JettonSwap" for action in event.get("actions") or []
        )
        for action in event.get("actions") or []:
            action_type = action.get("type")
            if action_type == "JettonSwap":
                payload = action.get("JettonSwap") or {}
                if target:
                    user_wallet = (payload.get("user_wallet") or {}).get("address") or ""
                    if user_wallet.lower() != target:
                        continue
                swap = normalize_swap(event, action)
            elif action_type == "FlawedJettonTransfer" and target:
                swap = normalize_flawed_transfer_buy(event, action, target)
            elif action_type == "JettonTransfer" and target and not has_explicit_swap:
                swap = normalize_transfer_swap(event, action, target)
            else:
                continue
            if swap is not None:
                swaps.append(swap)
    swaps.sort(key=lambda s: s.timestamp)
    return swaps


# --- DeDust pool-side reconstruction ----------------------------------------
#
# When we query events on a DeDust pool address (instead of a wallet),
# tonapi often only bundles SELLs of the pool's jetton into a tidy
# ``JettonSwap`` action. BUYs (TON-in / jetton-out) are not bundled — they
# arrive as a pair of ``SmartContractExec`` actions:
#
#   1. ``DedustSwapExternal`` (router/native-vault → pool): announces a swap
#      with input asset ``Amount`` and ``KindOut`` (false = output is jetton,
#      true = output is TON), plus the user's ``SenderAddr`` / ``RecipientAddr``.
#   2. ``DedustPayoutFromPool`` (pool → output vault): the output asset's raw
#      ``Amount`` and the user's ``RecipientAddr``.
#
# Both share a ``QueryId`` we use to pair them inside the same trace.

DEDUST_SWAP_EXTERNAL = "DedustSwapExternal"
DEDUST_PAYOUT_FROM_POOL = "DedustPayoutFromPool"

_PAYLOAD_KV_RE = re.compile(r"^\s*([A-Za-z]+):\s*(.+?)\s*$")


def _parse_dedust_payload(text: str | None) -> dict[str, str]:
    """Parse the YAML-ish ``payload`` string emitted with a DeDust SCE action."""
    out: dict[str, str] = {}
    if not text:
        return out
    for line in text.split("\n"):
        match = _PAYLOAD_KV_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        out[key] = value
    return out


def normalize_dedust_pool_swap(
    event: dict[str, Any],
    swap_external_action: dict[str, Any],
    payout_action: dict[str, Any],
    target_token: TokenInfo,
) -> tuple[str, Swap] | None:
    """Reconstruct a :class:`Swap` from a paired DeDust SCE action set.

    ``target_token`` provides the jetton metadata (decimals, symbol) for the
    non-TON side of the swap, since the pool-side trace does not include it
    inline. Returns ``(user_wallet_raw_hex, Swap)`` or ``None`` if the actions
    don't form a coherent swap (refund, missing fields, malformed numbers).
    """
    if not target_token or target_token.asset_id == TON_ASSET_ID:
        return None
    swap_kv = _parse_dedust_payload(
        (swap_external_action.get("SmartContractExec") or {}).get("payload")
    )
    payout_kv = _parse_dedust_payload((payout_action.get("SmartContractExec") or {}).get("payload"))
    user_wallet = (
        swap_kv.get("SenderAddr")
        or swap_kv.get("RecipientAddr")
        or payout_kv.get("RecipientAddr")
        or ""
    ).strip()
    if not user_wallet:
        return None
    user_wallet = _normalize_address(user_wallet)
    amount_in_raw = _to_int(swap_kv.get("Amount"))
    amount_out_raw = _to_int(payout_kv.get("Amount"))
    if amount_in_raw <= 0 or amount_out_raw <= 0:
        return None
    if amount_in_raw == amount_out_raw:
        # Refund / no-op — both legs report the same number.
        return None
    output_is_ton = swap_kv.get("KindOut", "").strip().lower() == "true"
    if output_is_ton:
        # SELL: jetton in, TON out.
        asset_in = target_token
        asset_out = _TON_TOKEN
        amount_in = _scale(amount_in_raw, target_token.decimals)
        amount_out = amount_out_raw / NANO
        ton_in_value: float | None = None
        ton_out_value: float | None = amount_out
    else:
        # BUY: TON in, jetton out.
        asset_in = _TON_TOKEN
        asset_out = target_token
        amount_in = amount_in_raw / NANO
        amount_out = _scale(amount_out_raw, target_token.decimals)
        ton_in_value = amount_in
        ton_out_value = None
    return user_wallet, Swap(
        timestamp=int(event.get("timestamp") or 0),
        event_id=str(event.get("event_id") or ""),
        dex="dedust",
        asset_in=asset_in,
        asset_out=asset_out,
        amount_in_raw=amount_in_raw,
        amount_out_raw=amount_out_raw,
        amount_in=amount_in,
        amount_out=amount_out,
        ton_in=ton_in_value,
        ton_out=ton_out_value,
    )


def extract_dedust_pool_swaps(
    events: list[dict[str, Any]],
    target_token: TokenInfo | None,
) -> list[tuple[str, Swap]]:
    """Find DedustSwapExternal+DedustPayoutFromPool pairs and synthesize swaps.

    Each yielded item is ``(user_wallet_raw, Swap)``. Pairs are matched by
    their ``QueryId`` so multi-hop traces with several swaps in one event are
    handled. Falls back to positional matching when QueryId is unavailable.
    """
    if target_token is None or target_token.asset_id == TON_ASSET_ID:
        return []
    out: list[tuple[str, Swap]] = []
    for event in events:
        swap_exts: list[tuple[str, dict[str, Any]]] = []
        payouts: list[tuple[str, dict[str, Any]]] = []
        for action in event.get("actions") or []:
            if action.get("type") != "SmartContractExec":
                continue
            sce = action.get("SmartContractExec") or {}
            op = sce.get("operation")
            if op == DEDUST_SWAP_EXTERNAL:
                qid = _parse_dedust_payload(sce.get("payload")).get("QueryId", "")
                swap_exts.append((qid, action))
            elif op == DEDUST_PAYOUT_FROM_POOL:
                qid = _parse_dedust_payload(sce.get("payload")).get("QueryId", "")
                payouts.append((qid, action))
        if not swap_exts or not payouts:
            continue
        # Pair by QueryId when present; otherwise by position.
        used_payouts: set[int] = set()
        for qid, swap_ext in swap_exts:
            payout_idx: int | None = None
            if qid:
                for i, (pq, _payout) in enumerate(payouts):
                    if i in used_payouts:
                        continue
                    if pq == qid:
                        payout_idx = i
                        break
            if payout_idx is None:
                for i in range(len(payouts)):
                    if i not in used_payouts:
                        payout_idx = i
                        break
            if payout_idx is None:
                continue
            used_payouts.add(payout_idx)
            payout = payouts[payout_idx][1]
            result = normalize_dedust_pool_swap(event, swap_ext, payout, target_token)
            if result:
                out.append(result)
    return out
