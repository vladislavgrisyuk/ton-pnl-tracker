"""Parallel multi-pool analytics with SQLite persistence.

The user pastes N pool addresses; we fan them out across a bounded
``asyncio.Semaphore`` (so 10 pools really do run with up to 10 concurrent
tonapi pipelines) and persist each completed result into the analytics DB so
the dashboard's ``DB browser`` view can query across pools later.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .analytics_db import AnalyticsDB, get_analytics_db
from .geckoterminal import GeckoTerminalClient
from .models import (
    MultiPoolChildStatus,
    MultiPoolJob,
    TokenAnalyticsProgress,
    TokenAnalyticsReport,
)
from .pricing import PriceService
from .settings import settings
from .token_analytics import analyze_pool_traders, to_raw_address
from .tonapi import TonApiClient

log = logging.getLogger(__name__)

OnUpdate = Callable[[MultiPoolJob], Awaitable[None] | None]


def _normalize_pool_token_pair(item: str) -> tuple[str, str | None]:
    """Accept ``pool`` or ``pool|token`` lines from the textarea."""
    item = item.strip()
    if not item:
        return ("", None)
    if "|" in item:
        pool, _, token = item.partition("|")
        return (pool.strip(), token.strip() or None)
    return (item, None)


def parse_pool_lines(raw: str) -> list[tuple[str, str | None]]:
    """Split a free-form textarea into ``(pool, token?)`` tuples, deduplicating."""
    pairs: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for line in raw.replace(",", "\n").splitlines():
        pool, token = _normalize_pool_token_pair(line)
        if not pool:
            continue
        try:
            normalized = to_raw_address(pool).lower()
        except Exception:  # noqa: BLE001 - we surface bad input via the API
            normalized = pool.lower()
        key = f"{normalized}|{(token or '').lower()}"
        if key in seen:
            continue
        seen.add(key)
        pairs.append((pool, token))
    return pairs


class MultiPoolJobRunner:
    """Owns the lifecycle of one multi-pool batch.

    Each pool gets its own ``TonApiClient`` and ``GeckoTerminalClient`` to
    avoid sharing rate-limiter state across siblings — that way 10 pools at
    rps=3 each peak around 30 RPS toward tonviewer, which is exactly the
    observed sustained ceiling.
    """

    def __init__(
        self,
        *,
        pools: list[tuple[str, str | None]],
        limit: int,
        rps: float,
        batch_size: int,
        max_concurrency: int,
        db: AnalyticsDB | None = None,
        on_update: OnUpdate | None = None,
    ) -> None:
        from uuid import uuid4

        self.job_id = uuid4().hex
        self._db = db or get_analytics_db()
        self._on_update = on_update
        self._limit = max(1, limit)
        self._rps = max(0.1, rps)
        self._batch_size = max(1, min(batch_size, 100))
        self._max_concurrency = max(1, min(max_concurrency, settings.multi_pool_max_concurrency))

        children = [
            MultiPoolChildStatus(
                pool=pool,
                token_address=token,
                progress=TokenAnalyticsProgress(status="queued", message="Queued"),
            )
            for pool, token in pools
        ]
        self.job = MultiPoolJob(
            job_id=self.job_id,
            status="queued",
            children=children,
        )
        self._lock = asyncio.Lock()

    async def _broadcast(self) -> None:
        if self._on_update is None:
            return
        result = self._on_update(self.job)
        if asyncio.iscoroutine(result):
            await result

    async def _set_child_progress(
        self,
        idx: int,
        progress: TokenAnalyticsProgress,
    ) -> None:
        async with self._lock:
            self.job.children[idx].progress = progress

    async def _set_child_done(
        self,
        idx: int,
        *,
        report: TokenAnalyticsReport | None = None,
        error: str | None = None,
        persisted_rows: int = 0,
    ) -> None:
        async with self._lock:
            child = self.job.children[idx]
            child.report = report
            child.error = error
            child.persisted_rows = persisted_rows
            if error is not None:
                child.progress = TokenAnalyticsProgress(status="failed", message=error)
            else:
                events = report.processed_events if report else child.progress.processed_events
                wallets = report.trader_count if report else child.progress.discovered_wallets
                trades = report.trade_count if report else child.progress.trade_count
                child.progress = TokenAnalyticsProgress(
                    status="completed",
                    processed_events=events,
                    discovered_wallets=wallets,
                    processed_wallets=wallets,
                    trade_count=trades,
                    message="Completed",
                )
            self.job.total_persisted_rows = sum(c.persisted_rows for c in self.job.children)

    async def _run_one(
        self,
        idx: int,
        pool: str,
        token: str | None,
        sem: asyncio.Semaphore,
    ) -> None:
        async with sem:
            await self._set_child_progress(
                idx,
                TokenAnalyticsProgress(status="running", message="Starting"),
            )
            await self._broadcast()

            async def update_progress(
                processed_events: int,
                discovered_wallets: int,
                trade_count: int,
                message: str,
            ) -> None:
                await self._set_child_progress(
                    idx,
                    TokenAnalyticsProgress(
                        status="running",
                        processed_events=processed_events,
                        discovered_wallets=discovered_wallets,
                        processed_wallets=discovered_wallets,
                        trade_count=trade_count,
                        message=message,
                    ),
                )
                await self._broadcast()

            try:
                async with (
                    TonApiClient(rps=self._rps) as ton,
                    GeckoTerminalClient() as gecko,
                ):
                    pricing = PriceService(gecko)
                    report = await analyze_pool_traders(
                        ton,
                        pricing,
                        pool_address=pool,
                        token_address=token,
                        max_events=self._limit,
                        page_size=self._batch_size,
                        progress=update_progress,
                    )
            except Exception as exc:  # noqa: BLE001 - surface per-child failure
                log.exception("multi-pool child failed: pool=%s", pool)
                await self._set_child_done(idx, error=str(exc))
                await self._broadcast()
                return

            persisted = 0
            try:
                normalized_pool = to_raw_address(pool)
                persisted = await self._db.upsert_trader_rows(
                    report.rows,
                    pool_address=normalized_pool,
                )
            except Exception as exc:  # noqa: BLE001 - persistence shouldn't fail the job
                log.exception("multi-pool persistence failed: pool=%s", pool)
                report.warnings.append(f"DB persistence failed: {exc}")

            await self._set_child_done(idx, report=report, persisted_rows=persisted)
            await self._broadcast()

    async def run(self) -> None:
        self.job.status = "running"
        self.job.started_at = int(time.time())
        await self._broadcast()

        sem = asyncio.Semaphore(self._max_concurrency)
        coros = [
            self._run_one(idx, child.pool, child.token_address, sem)
            for idx, child in enumerate(self.job.children)
        ]
        try:
            await asyncio.gather(*coros, return_exceptions=False)
        except Exception as exc:  # noqa: BLE001 - any leak is captured here
            log.exception("multi-pool runner aborted")
            self.job.error = str(exc)

        successes = sum(1 for c in self.job.children if c.progress.status == "completed")
        failures = len(self.job.children) - successes
        if failures == 0:
            self.job.status = "completed"
        elif successes == 0:
            self.job.status = "failed"
        else:
            self.job.status = "partial"
        self.job.finished_at = int(time.time())
        await self._broadcast()


__all__ = [
    "MultiPoolJobRunner",
    "parse_pool_lines",
]
