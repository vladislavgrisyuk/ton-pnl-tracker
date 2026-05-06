"""FIFO PnL calculation across all swaps for one wallet.

Each swap moves balance from one asset to another. We treat the *bought* side
as a position opening at the implied USD price and the *sold* side as
realizing PnL against the FIFO queue of prior buys for that asset.

TON itself is treated like any other asset: when a wallet swaps jettons back
to TON we close the jetton position (and conceptually open a TON position),
but we do not roll TON forward as profit because the user is just holding
liquidity in another denomination. To keep the report intuitive we report
realized + unrealized PnL per token (including TON) in USD.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from .models import Swap, TokenInfo, TokenPnL


@dataclass
class _Lot:
    """A single FIFO lot recording how much of an asset was bought and at what cost.

    ``cost_per_unit`` is fixed at lot creation time. ``amount_remaining`` shrinks
    as later sells consume the lot; ``cost_basis_usd`` mirrors that so the
    aggregate cost basis of the remaining position can be summed cheaply.
    """

    amount_remaining: float
    cost_basis_usd: float
    cost_per_unit: float


def _empty_state() -> dict[str, deque[_Lot]]:
    return defaultdict(deque)


def compute_pnl(
    swaps: list[Swap],
    *,
    current_prices_usd: dict[str, float],
    current_balances: dict[str, float] | None = None,
    tokens_meta: dict[str, TokenInfo] | None = None,
) -> tuple[list[TokenPnL], float, float]:
    """Aggregate swaps into per-token PnL using FIFO matching.

    Parameters
    ----------
    swaps:
        All swaps for the wallet, oldest first.
    current_prices_usd:
        Latest USD price per asset id. Missing entries are treated as 0.
    current_balances:
        On-chain balances at "now" by asset id (scaled). When None, we use the
        sum of unmatched FIFO lots. Providing real balances is more accurate
        because it accounts for transfers in/out of the wallet that are not
        swaps.
    tokens_meta:
        Optional override for TokenInfo per asset id (e.g. to display the on-
        chain balance asset symbols).
    """
    lots: dict[str, deque[_Lot]] = _empty_state()
    realized: dict[str, float] = defaultdict(float)
    bought: dict[str, float] = defaultdict(float)
    sold: dict[str, float] = defaultdict(float)
    swap_count: dict[str, int] = defaultdict(int)
    unmatched_buys: dict[str, float] = defaultdict(float)
    unmatched_buys_usd: dict[str, float] = defaultdict(float)
    meta: dict[str, TokenInfo] = dict(tokens_meta or {})

    for swap in swaps:
        usd_value = swap.usd_value or 0.0
        meta.setdefault(swap.asset_in.asset_id, swap.asset_in)
        meta.setdefault(swap.asset_out.asset_id, swap.asset_out)

        # --- closing the asset_in position (sell side) ---
        sell_amount = swap.amount_in
        sell_proceeds = usd_value
        sold[swap.asset_in.asset_id] += sell_amount
        swap_count[swap.asset_in.asset_id] += 1

        remaining = sell_amount
        queue = lots[swap.asset_in.asset_id]
        while remaining > 1e-18 and queue:
            lot = queue[0]
            take = min(lot.amount_remaining, remaining)
            cost_share = take * lot.cost_per_unit
            proceeds_share = sell_proceeds * (take / sell_amount) if sell_amount else 0.0
            realized[swap.asset_in.asset_id] += proceeds_share - cost_share
            lot.amount_remaining -= take
            lot.cost_basis_usd -= cost_share
            remaining -= take
            if lot.amount_remaining <= 1e-12:
                queue.popleft()
        # When the wallet sells more of an asset than our visible history shows
        # it bought (TON used as base currency, jettons received via transfer or
        # airdrop, etc.) we auto-fund the missing cost basis at the swap's own
        # USD value. That keeps realized PnL on TON ≈ 0 across normal trading
        # and avoids booking phantom profit on un-tracked airdrops.
        if remaining > 1e-12 and sell_amount:
            unmatched_cost = sell_proceeds * (remaining / sell_amount)
            unmatched_buys[swap.asset_in.asset_id] += remaining
            unmatched_buys_usd[swap.asset_in.asset_id] += unmatched_cost

        # --- opening the asset_out position (buy side) ---
        buy_amount = swap.amount_out
        buy_cost = usd_value
        bought[swap.asset_out.asset_id] += buy_amount
        swap_count[swap.asset_out.asset_id] += 1
        if buy_amount > 0:
            cost_per_unit = buy_cost / buy_amount if buy_amount else 0.0
            lots[swap.asset_out.asset_id].append(
                _Lot(
                    amount_remaining=buy_amount,
                    cost_basis_usd=buy_cost,
                    cost_per_unit=cost_per_unit,
                )
            )

    # --- aggregate per-token report ---
    asset_ids = set(meta) | set(bought) | set(sold)
    if current_balances:
        asset_ids |= set(current_balances)

    rows: list[TokenPnL] = []
    total_realized = 0.0
    total_unrealized = 0.0

    for asset_id in asset_ids:
        token = meta.get(
            asset_id,
            TokenInfo(asset_id=asset_id, symbol="?", name=asset_id[:8], decimals=9),
        )
        queue = lots.get(asset_id) or deque()
        balance_from_lots = sum(lot.amount_remaining for lot in queue)
        if current_balances is not None and asset_id in current_balances:
            current_balance = current_balances[asset_id]
        else:
            current_balance = balance_from_lots

        # Reconcile FIFO remainder with the wallet's on-chain balance. Outflows
        # that are not swaps (TON transfers, jetton transfers out, gas burns,
        # bridges, …) silently leave the wallet, so the FIFO tail can exceed the
        # actual balance. We drain oldest-first to match, preserving the cost
        # basis of the most recent acquisitions which is what the user typically
        # cares about for unrealized PnL.
        if current_balances is not None and balance_from_lots > current_balance + 1e-9:
            excess = balance_from_lots - current_balance
            while excess > 1e-12 and queue:
                lot = queue[0]
                take = min(lot.amount_remaining, excess)
                lot.amount_remaining -= take
                lot.cost_basis_usd -= take * lot.cost_per_unit
                excess -= take
                if lot.amount_remaining <= 1e-12:
                    queue.popleft()

        fifo_cost = sum(lot.cost_basis_usd for lot in queue)
        fifo_balance = sum(lot.amount_remaining for lot in queue)
        avg_buy_price = fifo_cost / fifo_balance if fifo_balance > 0 else 0.0
        current_price = current_prices_usd.get(asset_id)
        current_value = current_balance * current_price if current_price else 0.0

        # If the on-chain balance exceeds what FIFO tracked (incoming transfer
        # we didn't see, airdrop, ...), price the excess at current price so it
        # contributes 0 to unrealized PnL — we have no honest cost basis for it.
        excess = max(0.0, current_balance - fifo_balance)
        excess_cost = excess * current_price if current_price else 0.0
        cost_basis_remaining = fifo_cost + excess_cost

        if current_price is not None:
            unrealized = current_value - cost_basis_remaining
        else:
            unrealized = 0.0
        total_pnl = realized[asset_id] + unrealized

        rows.append(
            TokenPnL(
                token=token,
                total_bought=bought[asset_id],
                total_sold=sold[asset_id],
                current_balance=current_balance,
                avg_buy_price_usd=avg_buy_price,
                cost_basis_remaining_usd=cost_basis_remaining,
                realized_pnl_usd=realized[asset_id],
                unrealized_pnl_usd=unrealized,
                total_pnl_usd=total_pnl,
                current_price_usd=current_price,
                current_value_usd=current_value,
                swap_count=swap_count[asset_id],
            )
        )
        total_realized += realized[asset_id]
        total_unrealized += unrealized

    rows.sort(key=lambda r: abs(r.total_pnl_usd), reverse=True)
    return rows, total_realized, total_unrealized
