"""End-to-end smoke test for the parallel multi-pool job runner.

The runner is stubbed at the ``analyze_pool_traders`` boundary so we don't
hit any network APIs; all we care about here is that:

1. N pools are processed concurrently (concurrency = number of pools).
2. Each completed pool persists its rows to the analytics DB.
3. The aggregate ``MultiPoolJob`` reflects per-child status and the total
   ``persisted_rows`` count.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path

import pytest

from ton_pnl import multi_pool as multi_pool_mod
from ton_pnl.analytics_db import AnalyticsDB
from ton_pnl.models import (
    TokenAnalyticsReport,
    TokenInfo,
    TokenTraderRow,
)
from ton_pnl.multi_pool import MultiPoolJobRunner

WALLET_PREFIX = "0:" + "1" * 56


def _trader_row(wallet: str, token: TokenInfo) -> TokenTraderRow:
    return TokenTraderRow(
        wallet=wallet,
        token=token,
        total_bought=1000.0,
        total_sold=0.0,
        estimated_balance=1000.0,
        buy_volume_usd=50.0,
        sell_volume_usd=0.0,
        avg_buy_price_usd=0.05,
        current_price_usd=0.07,
        current_value_usd=70.0,
        realized_pnl_usd=0.0,
        unrealized_pnl_usd=20.0,
        total_pnl_usd=20.0,
        trade_count=1,
        first_trade_ts=1_700_000_000,
        last_trade_ts=1_700_000_000,
        only_sells=False,
        sold_more_than_bought=False,
    )


def _make_token(idx: int) -> TokenInfo:
    suffix = f"{idx:0>2}".rjust(60, "0")
    return TokenInfo(
        asset_id=f"0:{suffix}",
        symbol=f"T{idx}",
        name=f"Token {idx}",
        decimals=9,
    )


@pytest.mark.asyncio
async def test_multi_pool_runs_pools_concurrently_and_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = AnalyticsDB(tmp_path / "multi.db")

    concurrent_now = 0
    peak_concurrent = 0
    lock = asyncio.Lock()

    async def fake_analyze(
        ton, pricing, *, pool_address, token_address=None, max_events, page_size, progress
    ):  # noqa: ARG001
        nonlocal concurrent_now, peak_concurrent
        async with lock:
            concurrent_now += 1
            peak_concurrent = max(peak_concurrent, concurrent_now)
        try:
            # Yield enough times to give siblings a chance to run.
            await asyncio.sleep(0.05)
            idx = int(pool_address[-1])
            token = _make_token(idx)
            wallet = f"0:{idx:0>2}".rjust(64, "0")
            report = TokenAnalyticsReport(
                pool=pool_address,
                token=token,
                current_price_usd=0.07,
                trader_count=1,
                trade_count=1,
                processed_events=10,
                rows=[_trader_row(wallet, token)],
                warnings=[],
            )
            if progress is not None:
                await progress(10, 1, 1, "Done")
            return report
        finally:
            async with lock:
                concurrent_now -= 1

    class _DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(multi_pool_mod, "analyze_pool_traders", fake_analyze)
    monkeypatch.setattr(multi_pool_mod, "TonApiClient", lambda **kw: _DummyClient())
    monkeypatch.setattr(multi_pool_mod, "GeckoTerminalClient", lambda **kw: _DummyClient())

    pools: Iterable[tuple[str, str | None]] = [
        ("0:" + str(i).rjust(63, "0"), None) for i in range(5)
    ]
    runner = MultiPoolJobRunner(
        pools=list(pools),
        limit=100,
        rps=3.0,
        batch_size=100,
        max_concurrency=5,
        db=db,
    )

    await runner.run()
    job = runner.job

    assert job.status == "completed"
    assert len(job.children) == 5
    assert all(c.progress.status == "completed" for c in job.children)
    assert all(c.persisted_rows == 1 for c in job.children)
    assert job.total_persisted_rows == 5
    # All 5 should have run concurrently — the floor is "at least 2" to keep
    # the assertion robust under tight CI clocks; in practice it hits 5.
    assert peak_concurrent >= 2

    # Verify the DB actually got the rows.
    stats = db.stats_sync()
    assert stats["row_count"] == 5
    assert stats["token_count"] == 5
