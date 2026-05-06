"""FastAPI entrypoint exposing wallet PnL endpoints.

The single business endpoint is ``GET /api/wallet/{address}/pnl`` which:

1. Fetches the account events from tonapi.io (paginated by ``before_lt``).
2. Extracts ``JettonSwap`` actions where ``user_wallet`` matches the request.
3. Annotates each swap with a USD value (TON-leg ground truth or GeckoTerminal).
4. Runs the FIFO PnL aggregator across the swaps.
5. Joins on-chain jetton balances + current GeckoTerminal prices for unrealized.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .address import friendly_to_raw
from .geckoterminal import GeckoTerminalClient
from .models import (
    TON_ASSET_ID,
    PnLReport,
    Swap,
    TokenAnalyticsJob,
    TokenAnalyticsProgress,
    TokenInfo,
)
from .pnl import compute_pnl
from .pricing import PriceService, annotate_usd_values
from .settings import settings
from .swaps import TON_DECIMALS, extract_swaps
from .token_analytics import analyze_pool_traders
from .tonapi import TonApiClient, TonApiError

log = logging.getLogger(__name__)

# Simple in-memory cache: wallet -> (timestamp, report). Lets the dashboard
# refresh quickly without hammering the upstream APIs.
_CACHE: dict[str, tuple[float, PnLReport]] = {}
_TOKEN_ANALYTICS_JOBS: dict[str, TokenAnalyticsJob] = {}


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
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)


class HealthResponse(BaseModel):
    status: str
    service: str


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="ton-pnl-backend")


async def _run_token_analytics_job(
    job_id: str,
    *,
    pool_address: str,
    token_address: str | None,
    limit: int,
    rps: float,
    batch_size: int,
) -> None:
    job = _TOKEN_ANALYTICS_JOBS[job_id]

    async def update_progress(
        processed_events: int,
        discovered_wallets: int,
        trade_count: int,
        message: str,
    ) -> None:
        job.progress = TokenAnalyticsProgress(
            status="running",
            processed_events=processed_events,
            discovered_wallets=discovered_wallets,
            processed_wallets=discovered_wallets,
            trade_count=trade_count,
            message=message,
        )

    try:
        job.progress = TokenAnalyticsProgress(status="running", message="Starting")
        async with TonApiClient(rps=rps) as ton, GeckoTerminalClient() as gecko:
            pricing = PriceService(gecko)
            report = await analyze_pool_traders(
                ton,
                pricing,
                pool_address=pool_address,
                token_address=token_address,
                max_events=limit,
                page_size=batch_size,
                progress=update_progress,
            )
        job.report = report
        job.progress = TokenAnalyticsProgress(
            status="completed",
            processed_events=report.processed_events,
            discovered_wallets=report.trader_count,
            processed_wallets=report.trader_count,
            trade_count=report.trade_count,
            message="Completed",
        )
    except Exception as exc:  # noqa: BLE001 - background job must capture failures
        log.exception("token analytics job failed")
        job.error = str(exc)
        job.progress = TokenAnalyticsProgress(
            status="failed",
            processed_events=job.progress.processed_events,
            discovered_wallets=job.progress.discovered_wallets,
            processed_wallets=job.progress.processed_wallets,
            trade_count=job.progress.trade_count,
            message=str(exc),
        )


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


def _to_raw_address(address: str) -> str:
    if ":" in address:
        return address
    return friendly_to_raw(address)


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
        addr = _to_raw_address(addr)
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
async def wallet_pnl(
    address: str,
    refresh: bool = False,
    limit: int | None = None,
    rps: float | None = None,
    batch_size: int | None = None,
) -> PnLReport:
    address = address.strip()
    if not address:
        raise HTTPException(status_code=400, detail="empty wallet address")
    try:
        normalized_address = _to_raw_address(address)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid wallet address") from exc
    effective_rps = rps if rps is not None else settings.tonapi_rps
    effective_batch_size = (
        batch_size if batch_size is not None else settings.tonapi_event_batch_size
    )
    if effective_rps <= 0:
        raise HTTPException(status_code=400, detail="rps must be greater than 0")
    if effective_batch_size < 1 or effective_batch_size > 100:
        raise HTTPException(status_code=400, detail="batch_size must be between 1 and 100")
    cache_key = f"{address}:{limit or 'default'}:{effective_rps}:{effective_batch_size}"
    if not refresh:
        cached = _get_cached(cache_key)
        if cached is not None:
            return cached

    warnings: list[str] = []
    try:
        async with TonApiClient(rps=effective_rps) as ton:
            account, events, jetton_balances = await asyncio.gather(
                ton.get_account(normalized_address),
                ton.iter_events(
                    normalized_address,
                    limit=effective_batch_size,
                    max_events=limit,
                ),
                ton.get_jetton_balances(normalized_address),
            )
            normalized_address = account.get("address") or normalized_address
            ton_balance = int(account.get("balance") or 0)
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


@app.post("/api/token-analytics/{pool_address}/jobs", response_model=TokenAnalyticsJob)
async def start_token_analytics_job(
    pool_address: str,
    token_address: str | None = None,
    limit: int | None = None,
    rps: float | None = None,
    batch_size: int | None = None,
) -> TokenAnalyticsJob:
    pool_address = pool_address.strip()
    token_address = token_address.strip() if token_address else None
    if not pool_address:
        raise HTTPException(status_code=400, detail="empty pool address")
    try:
        _to_raw_address(pool_address)
        if token_address:
            _to_raw_address(token_address)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid address") from exc

    effective_rps = rps if rps is not None else settings.tonapi_rps
    effective_batch_size = batch_size if batch_size is not None else 100
    effective_limit = limit if limit is not None else settings.max_events_per_wallet
    if effective_rps <= 0:
        raise HTTPException(status_code=400, detail="rps must be greater than 0")
    if effective_batch_size < 1 or effective_batch_size > 100:
        raise HTTPException(status_code=400, detail="batch_size must be between 1 and 100")
    if effective_limit < 1:
        raise HTTPException(status_code=400, detail="limit must be greater than 0")

    job_id = uuid.uuid4().hex
    job = TokenAnalyticsJob(
        job_id=job_id,
        progress=TokenAnalyticsProgress(status="queued", message="Queued"),
    )
    _TOKEN_ANALYTICS_JOBS[job_id] = job
    asyncio.create_task(
        _run_token_analytics_job(
            job_id,
            pool_address=pool_address,
            token_address=token_address,
            limit=effective_limit,
            rps=effective_rps,
            batch_size=effective_batch_size,
        )
    )
    return job


@app.get("/api/token-analytics/jobs/{job_id}", response_model=TokenAnalyticsJob)
async def get_token_analytics_job(job_id: str) -> TokenAnalyticsJob:
    job = _TOKEN_ANALYTICS_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job
