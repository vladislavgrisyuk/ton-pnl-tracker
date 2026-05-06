"""FastAPI entrypoint exposing wallet PnL endpoints.

The single business endpoint is ``GET /api/wallet/{address}/pnl`` which:

1. Fetches the account events from tonapi.io (paginated by ``before_lt``).
2. Extracts ``JettonSwap`` actions where ``user_wallet`` matches the request.
3. Annotates each swap with a USD value (TON-leg ground truth or GeckoTerminal).
4. Runs the FIFO PnL aggregator across the swaps.
5. Joins on-chain jetton balances + current GeckoTerminal prices for unrealized.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .geckoterminal import GeckoTerminalClient
from .models import TON_ASSET_ID, PnLReport, Swap, TokenInfo
from .pnl import compute_pnl
from .pricing import PriceService, annotate_usd_values
from .settings import settings
from .swaps import TON_DECIMALS, extract_swaps
from .tonapi import TonApiClient, TonApiError

log = logging.getLogger(__name__)

# Simple in-memory cache: wallet -> (timestamp, report). Lets the dashboard
# refresh quickly without hammering the upstream APIs.
_CACHE: dict[str, tuple[float, PnLReport]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    yield


app = FastAPI(
    title="TON PnL Tracker",
    version="0.1.0",
    description="Aggregates jetton swaps for a TON wallet and computes FIFO PnL.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


class HealthResponse(BaseModel):
    status: str
    service: str


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="ton-pnl-backend")


def _get_cached(wallet: str) -> PnLReport | None:
    import time

    entry = _CACHE.get(wallet)
    if not entry:
        return None
    ts, report = entry
    if time.time() - ts > settings.cache_ttl_seconds:
        return None
    return report


def _set_cached(wallet: str, report: PnLReport) -> None:
    import time

    _CACHE[wallet] = (time.time(), report)


def _balance_map(
    raw_jetton_balances: list[dict[str, Any]],
    ton_balance_nanotons: int,
) -> tuple[dict[str, float], dict[str, TokenInfo]]:
    balances: dict[str, float] = {}
    metas: dict[str, TokenInfo] = {}
    for entry in raw_jetton_balances:
        jetton = entry.get("jetton") or {}
        addr = jetton.get("address")
        if not addr:
            continue
        decimals = int(jetton.get("decimals") or 9)
        try:
            raw = int(entry.get("balance") or "0")
        except (TypeError, ValueError):
            raw = 0
        balances[addr] = raw / (10**decimals)
        metas[addr] = TokenInfo(
            asset_id=addr,
            symbol=jetton.get("symbol") or "?",
            name=jetton.get("name") or addr[:8],
            decimals=decimals,
            image=jetton.get("image"),
        )
    balances[TON_ASSET_ID] = ton_balance_nanotons / (10**TON_DECIMALS)
    metas[TON_ASSET_ID] = TokenInfo(
        asset_id=TON_ASSET_ID,
        symbol="TON",
        name="Toncoin",
        decimals=TON_DECIMALS,
        image="https://ton.org/download/ton_symbol.png",
    )
    return balances, metas


@app.get("/api/wallet/{address}/pnl", response_model=PnLReport)
async def wallet_pnl(address: str, refresh: bool = False, limit: int | None = None) -> PnLReport:
    address = address.strip()
    if not address:
        raise HTTPException(status_code=400, detail="empty wallet address")
    cache_key = f"{address}:{limit or 'default'}"
    if not refresh:
        cached = _get_cached(cache_key)
        if cached is not None:
            return cached

    warnings: list[str] = []
    try:
        async with TonApiClient() as ton:
            account = await ton.get_account(address)
            normalized_address = account.get("address") or address
            ton_balance = int(account.get("balance") or 0)
            events = await ton.iter_events(address, max_events=limit)
            jetton_balances = await ton.get_jetton_balances(address)
    except TonApiError as exc:
        log.exception("tonapi error")
        raise HTTPException(status_code=502, detail=f"tonapi error: {exc}") from exc

    swaps: list[Swap] = extract_swaps(events, wallet_address=normalized_address)

    balances, metas = _balance_map(jetton_balances, ton_balance)

    async with GeckoTerminalClient() as gecko:
        pricing = PriceService(gecko)
        await annotate_usd_values(swaps, pricing)
        # Current prices for unrealized PnL — only fetch for tokens we actually hold
        # or have traded (avoid unbounded fan-out).
        held_or_traded = list(
            {a for s in swaps for a in (s.asset_in.asset_id, s.asset_out.asset_id)}
            | set(balances.keys())
        )
        try:
            current_prices = await pricing.current_prices(held_or_traded)
        except Exception as exc:  # noqa: BLE001 - upstream API may be flaky
            log.warning("current price fetch failed: %s", exc)
            warnings.append(f"current prices unavailable: {exc}")
            current_prices = {}

    rows, total_realized, total_unrealized = compute_pnl(
        swaps,
        current_prices_usd=current_prices,
        current_balances=balances,
        tokens_meta=metas,
    )

    report = PnLReport(
        wallet=normalized_address,
        swap_count=len(swaps),
        realized_pnl_usd=total_realized,
        unrealized_pnl_usd=total_unrealized,
        total_pnl_usd=total_realized + total_unrealized,
        tokens=rows,
        swaps=swaps,
        warnings=warnings,
    )
    _set_cached(cache_key, report)
    return report
