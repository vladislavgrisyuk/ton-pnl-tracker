"""Domain models for swaps and PnL aggregates."""

from __future__ import annotations

from typing import Literal

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


class TokenTraderRow(BaseModel):
    wallet: str
    token: TokenInfo
    total_bought: float = 0.0
    total_sold: float = 0.0
    estimated_balance: float = 0.0
    buy_volume_usd: float = 0.0
    sell_volume_usd: float = 0.0
    avg_buy_price_usd: float = 0.0
    current_price_usd: float | None = None
    current_value_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    total_pnl_usd: float = 0.0
    trade_count: int = 0
    first_trade_ts: int | None = None
    last_trade_ts: int | None = None
    only_sells: bool = False
    sold_more_than_bought: bool = False


class TokenAnalyticsProgress(BaseModel):
    status: Literal["queued", "running", "completed", "failed"]
    processed_events: int = 0
    discovered_wallets: int = 0
    processed_wallets: int = 0
    trade_count: int = 0
    message: str | None = None


class TokenAnalyticsReport(BaseModel):
    pool: str
    token: TokenInfo
    current_price_usd: float | None = None
    trader_count: int
    trade_count: int
    processed_events: int
    rows: list[TokenTraderRow]
    warnings: list[str] = []


class TokenAnalyticsJob(BaseModel):
    job_id: str
    progress: TokenAnalyticsProgress
    report: TokenAnalyticsReport | None = None
    error: str | None = None


class MultiPoolChildStatus(BaseModel):
    """Status of one pool inside a multi-pool batch."""

    pool: str = Field(description="raw pool address as accepted")
    token_address: str | None = None
    progress: TokenAnalyticsProgress
    report: TokenAnalyticsReport | None = None
    error: str | None = None
    persisted_rows: int = 0


class MultiPoolJob(BaseModel):
    """Top-level batch job that fans out N pools in parallel."""

    job_id: str
    status: Literal["queued", "running", "completed", "failed", "partial"] = "queued"
    started_at: int | None = None
    finished_at: int | None = None
    children: list[MultiPoolChildStatus] = []
    error: str | None = None
    total_persisted_rows: int = 0


class WalletTokenStatRow(BaseModel):
    """API model for ``wallet_token_stats`` rows (mirrors the SQLite schema)."""

    wallet: str
    token_master: str
    pool_address: str
    token_symbol: str | None = None
    token_name: str | None = None
    token_decimals: int | None = None
    token_image: str | None = None
    total_bought: float
    total_sold: float
    estimated_balance: float
    buy_volume_usd: float
    sell_volume_usd: float
    avg_buy_price_usd: float | None = None
    current_price_usd: float | None = None
    current_value_usd: float
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    total_pnl_usd: float
    trade_count: int
    first_trade_ts: int | None = None
    last_trade_ts: int | None = None
    only_sells: bool
    sold_more_than_bought: bool
    updated_at: int


class TokenSummaryRow(BaseModel):
    """One row per analyzed token (used by the DB browser sidebar)."""

    token_master: str
    token_symbol: str | None = None
    token_name: str | None = None
    token_image: str | None = None
    token_decimals: int | None = None
    wallet_count: int
    total_buy_usd: float
    total_sell_usd: float
    last_updated: int | None = None


class WalletTokenStatsResponse(BaseModel):
    rows: list[WalletTokenStatRow]
    tokens: list[TokenSummaryRow]
    db_stats: dict[str, int | None]
