from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .address import friendly_to_raw
from .models import TON_ASSET_ID, Swap, TokenAnalyticsReport, TokenInfo, TokenTraderRow
from .pricing import PriceService, annotate_usd_values
from .swaps import (
    extract_dedust_pool_swaps,
    extract_swaps,
    normalize_swap,
)
from .tonapi import TonApiClient

ProgressCallback = Callable[[int, int, int, str], Awaitable[None]]


@dataclass
class TraderSwap:
    wallet: str
    swap: Swap


@dataclass
class _TraderTotals:
    total_bought: float = 0.0
    total_sold: float = 0.0
    buy_volume_usd: float = 0.0
    sell_volume_usd: float = 0.0
    trade_count: int = 0
    first_trade_ts: int | None = None
    last_trade_ts: int | None = None


def to_raw_address(address: str) -> str:
    address = address.strip()
    if ":" in address:
        return address
    return friendly_to_raw(address)


def _address(payload: dict[str, Any] | None) -> str:
    return str((payload or {}).get("address") or "")


def _wallet_candidates(event: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    account = event.get("account") or {}
    if account.get("is_wallet"):
        candidates.append(_address(account))
    for action in event.get("actions") or []:
        action_type = action.get("type")
        if action_type == "JettonSwap":
            payload = action.get("JettonSwap") or {}
            wallet = _address(payload.get("user_wallet") or {})
            if wallet:
                candidates.append(wallet)
        elif action_type == "SmartContractExec":
            payload = action.get("SmartContractExec") or {}
            executor = payload.get("executor") or {}
            if executor.get("is_wallet"):
                candidates.append(_address(executor))
        elif action_type in {"JettonTransfer", "FlawedJettonTransfer", "TonTransfer"}:
            payload = action.get(action_type) or {}
            for side in ("sender", "recipient"):
                entity = payload.get(side) or {}
                if entity.get("is_wallet"):
                    candidates.append(_address(entity))
    seen: set[str] = set()
    out: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        lowered = candidate.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(candidate)
    return out


def extract_pool_trader_swaps(events: list[dict[str, Any]]) -> list[TraderSwap]:
    trader_swaps: list[TraderSwap] = []
    seen: set[tuple[str, str, str, str, int, int]] = set()
    for event in events:
        has_explicit_swap = False
        for action in event.get("actions") or []:
            if action.get("type") != "JettonSwap":
                continue
            has_explicit_swap = True
            payload = action.get("JettonSwap") or {}
            wallet = _address(payload.get("user_wallet") or {})
            if not wallet:
                continue
            swap = normalize_swap(event, action)
            if swap is None:
                continue
            key = (
                wallet.lower(),
                swap.event_id,
                swap.asset_in.asset_id,
                swap.asset_out.asset_id,
                swap.amount_in_raw,
                swap.amount_out_raw,
            )
            if key in seen:
                continue
            seen.add(key)
            trader_swaps.append(TraderSwap(wallet=wallet, swap=swap))
        if has_explicit_swap:
            continue
        for wallet in _wallet_candidates(event):
            for swap in extract_swaps([event], wallet_address=wallet):
                key = (
                    wallet.lower(),
                    swap.event_id,
                    swap.asset_in.asset_id,
                    swap.asset_out.asset_id,
                    swap.amount_in_raw,
                    swap.amount_out_raw,
                )
                if key in seen:
                    continue
                seen.add(key)
                trader_swaps.append(TraderSwap(wallet=wallet, swap=swap))
    trader_swaps.sort(key=lambda item: item.swap.timestamp)
    return trader_swaps


def _choose_token(trader_swaps: list[TraderSwap], token_address: str | None) -> TokenInfo | None:
    if token_address:
        target = to_raw_address(token_address).lower()
        for item in trader_swaps:
            if item.swap.asset_in.asset_id.lower() == target:
                return item.swap.asset_in
            if item.swap.asset_out.asset_id.lower() == target:
                return item.swap.asset_out
        return TokenInfo(
            asset_id=to_raw_address(token_address),
            symbol="?",
            name=to_raw_address(token_address)[:8],
            decimals=9,
        )
    counts: Counter[str] = Counter()
    metas: dict[str, TokenInfo] = {}
    for item in trader_swaps:
        for token in (item.swap.asset_in, item.swap.asset_out):
            if token.asset_id == TON_ASSET_ID:
                continue
            counts[token.asset_id] += 1
            metas.setdefault(token.asset_id, token)
    if not counts:
        return None
    asset_id = counts.most_common(1)[0][0]
    return metas[asset_id]


def _build_rows(
    trader_swaps: list[TraderSwap],
    *,
    token: TokenInfo,
    current_price_usd: float | None,
) -> list[TokenTraderRow]:
    totals: dict[str, _TraderTotals] = defaultdict(_TraderTotals)
    target = token.asset_id.lower()
    for item in trader_swaps:
        swap = item.swap
        wallet = item.wallet
        usd_value = swap.usd_value or 0.0
        changed = False
        row = totals[wallet]
        if swap.asset_out.asset_id.lower() == target:
            row.total_bought += swap.amount_out
            row.buy_volume_usd += usd_value
            changed = True
        if swap.asset_in.asset_id.lower() == target:
            row.total_sold += swap.amount_in
            row.sell_volume_usd += usd_value
            changed = True
        if not changed:
            continue
        row.trade_count += 1
        row.first_trade_ts = (
            swap.timestamp
            if row.first_trade_ts is None
            else min(row.first_trade_ts, swap.timestamp)
        )
        row.last_trade_ts = (
            swap.timestamp if row.last_trade_ts is None else max(row.last_trade_ts, swap.timestamp)
        )

    rows: list[TokenTraderRow] = []
    for wallet, total in totals.items():
        estimated_balance = total.total_bought - total.total_sold
        current_value = (
            max(estimated_balance, 0.0) * current_price_usd if current_price_usd else 0.0
        )
        realized = total.sell_volume_usd - total.buy_volume_usd
        unrealized = current_value
        avg_buy_price = total.buy_volume_usd / total.total_bought if total.total_bought > 0 else 0.0
        rows.append(
            TokenTraderRow(
                wallet=wallet,
                token=token,
                total_bought=total.total_bought,
                total_sold=total.total_sold,
                estimated_balance=estimated_balance,
                buy_volume_usd=total.buy_volume_usd,
                sell_volume_usd=total.sell_volume_usd,
                avg_buy_price_usd=avg_buy_price,
                current_price_usd=current_price_usd,
                current_value_usd=current_value,
                realized_pnl_usd=realized,
                unrealized_pnl_usd=unrealized,
                total_pnl_usd=realized + unrealized,
                trade_count=total.trade_count,
                first_trade_ts=total.first_trade_ts,
                last_trade_ts=total.last_trade_ts,
                only_sells=total.total_bought <= 1e-12 and total.total_sold > 0,
                sold_more_than_bought=estimated_balance < -1e-12,
            )
        )
    rows.sort(key=lambda row: row.last_trade_ts or 0, reverse=True)
    return rows


async def _fetch_jetton_token(ton: TonApiClient, asset_id: str) -> TokenInfo | None:
    """Pull jetton metadata from tonapi when no JettonSwap event reveals it."""
    try:
        info = await ton.get_jetton_info(asset_id)
    except Exception:  # noqa: BLE001 - upstream may be flaky / wrong address
        return None
    metadata = info.get("metadata") or {}
    decimals_raw = metadata.get("decimals")
    try:
        decimals = int(decimals_raw) if decimals_raw is not None else 9
    except (TypeError, ValueError):
        decimals = 9
    symbol = metadata.get("symbol") or "?"
    name = metadata.get("name") or symbol or asset_id[:8]
    image = metadata.get("image")
    return TokenInfo(
        asset_id=asset_id,
        symbol=symbol,
        name=name,
        decimals=decimals,
        image=image,
    )


def _swap_dedup_key(wallet: str, swap: Swap) -> tuple[str, str, str, str, int, int]:
    return (
        wallet.lower(),
        swap.event_id,
        swap.asset_in.asset_id,
        swap.asset_out.asset_id,
        swap.amount_in_raw,
        swap.amount_out_raw,
    )


async def analyze_pool_traders(
    ton: TonApiClient,
    pricing: PriceService,
    *,
    pool_address: str,
    token_address: str | None = None,
    max_events: int = 1000,
    page_size: int = 100,
    progress: ProgressCallback | None = None,
) -> TokenAnalyticsReport:
    normalized_pool = to_raw_address(pool_address)
    page_size = max(1, min(page_size, 100))
    max_events = max(1, max_events)
    before_lt: int | None = None
    events_seen = 0
    trader_swaps: list[TraderSwap] = []
    seen_keys: set[tuple[str, str, str, str, int, int]] = set()
    wallets: set[str] = set()
    warnings: list[str] = []
    all_events: list[dict[str, Any]] = []

    if progress is not None:
        await progress(0, 0, 0, "Fetching pool events")

    while events_seen < max_events:
        request_limit = min(page_size, max_events - events_seen)
        page_events, next_from = await ton.get_events_page(
            normalized_pool,
            limit=request_limit,
            before_lt=before_lt,
        )
        if not page_events:
            break
        events_seen += len(page_events)
        all_events.extend(page_events)
        page_swaps = extract_pool_trader_swaps(page_events)
        for item in page_swaps:
            key = _swap_dedup_key(item.wallet, item.swap)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            trader_swaps.append(item)
            wallets.add(item.wallet)
        if progress is not None:
            await progress(events_seen, len(wallets), len(trader_swaps), "Fetching pool events")
        if not next_from or next_from == 0:
            break
        before_lt = next_from

    token = _choose_token(trader_swaps, token_address)
    # If we did not see any JettonSwap-based hint but the user named a token,
    # grab its metadata directly so DeDust SCE-pair (BUY) reconstruction works.
    if (token is None or token.asset_id == TON_ASSET_ID) and token_address:
        fetched = await _fetch_jetton_token(ton, to_raw_address(token_address))
        if fetched is not None:
            token = fetched

    # Phase 2: reconstruct DeDust pool BUYs (and any sells that tonapi did not
    # bundle as JettonSwap) from the SCE pair pattern.
    if token is not None and token.asset_id != TON_ASSET_ID:
        for wallet, swap in extract_dedust_pool_swaps(all_events, token):
            key = _swap_dedup_key(wallet, swap)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            trader_swaps.append(TraderSwap(wallet=wallet, swap=swap))
            wallets.add(wallet)

    trader_swaps.sort(key=lambda item: item.swap.timestamp)

    if token is None:
        token = TokenInfo(
            asset_id=token_address or normalized_pool,
            symbol="?",
            name="Unknown token",
            decimals=9,
        )
        warnings.append("No non-TON token swaps were found in the fetched pool events.")

    if progress is not None:
        await progress(events_seen, len(wallets), len(trader_swaps), "Pricing swaps")

    swaps = [item.swap for item in trader_swaps]
    await annotate_usd_values(swaps, pricing)

    try:
        current_prices = await pricing.current_prices([token.asset_id])
    except Exception as exc:
        warnings.append(f"current price unavailable: {exc}")
        current_prices = {}
    current_price = current_prices.get(token.asset_id)

    filtered = [
        item
        for item in trader_swaps
        if item.swap.asset_in.asset_id.lower() == token.asset_id.lower()
        or item.swap.asset_out.asset_id.lower() == token.asset_id.lower()
    ]
    rows = _build_rows(filtered, token=token, current_price_usd=current_price)
    if progress is not None:
        await progress(events_seen, len({row.wallet for row in rows}), len(filtered), "Completed")

    return TokenAnalyticsReport(
        pool=normalized_pool,
        token=token,
        current_price_usd=current_price,
        trader_count=len(rows),
        trade_count=len(filtered),
        processed_events=events_seen,
        rows=rows,
        warnings=warnings,
    )
