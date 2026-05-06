"""Domain models for swaps and PnL aggregates."""

from __future__ import annotations

from pydantic import BaseModel, Field

# Sentinel used to represent native TON inside maps keyed by jetton master address.
# tonapi.io reports the zero-address for TON in some places (e.g. GeckoTerminal),
# but we use this readable string internally to avoid confusion with the zero jetton.
TON_ASSET_ID = "TON"


class TokenInfo(BaseModel):
    """Minimal info needed to display a token on the dashboard."""

    asset_id: str = Field(description="TON for native, otherwise jetton master address")
    symbol: str
    name: str
    decimals: int
    image: str | None = None


class Swap(BaseModel):
    """A single normalized swap performed by the wallet under review."""

    timestamp: int
    event_id: str
    dex: str
    asset_in: TokenInfo
    asset_out: TokenInfo
    amount_in_raw: int = Field(description="amount sold, in the asset's smallest units")
    amount_out_raw: int = Field(description="amount received, in the asset's smallest units")
    amount_in: float = Field(description="amount sold, scaled by decimals")
    amount_out: float = Field(description="amount received, scaled by decimals")
    ton_in: float | None = Field(
        default=None, description="TON value of the sold side at swap time, if known"
    )
    ton_out: float | None = Field(
        default=None, description="TON value of the received side at swap time, if known"
    )
    usd_value: float | None = Field(
        default=None, description="USD value of the swap (mid) at swap time, if known"
    )


class TokenPnL(BaseModel):
    """Aggregated PnL position for a single token, in USD terms."""

    token: TokenInfo
    total_bought: float = 0.0
    total_sold: float = 0.0
    current_balance: float = 0.0
    avg_buy_price_usd: float = 0.0
    cost_basis_remaining_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    total_pnl_usd: float = 0.0
    current_price_usd: float | None = None
    current_value_usd: float = 0.0
    swap_count: int = 0


class PnLReport(BaseModel):
    """Top-level response for /api/wallet/{address}/pnl."""

    wallet: str
    swap_count: int
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    total_pnl_usd: float
    tokens: list[TokenPnL]
    swaps: list[Swap]
    warnings: list[str] = []
