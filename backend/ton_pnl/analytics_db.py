"""Persistent storage for cross-token wallet trading stats.

Backed by a single-file SQLite DB so that the analyzer can be re-run across
multiple pools and the user can do post-hoc analysis (top winners on token X,
wallets that traded both A and B, etc.).

The schema centers on ``wallet_token_stats``: one row per ``(wallet,
token_master)`` pair, upserted whenever a fresh pool analysis completes for
that token. ``avg_buy_price_usd`` is the volume-weighted average across all
buys we have observed (``Σ buy_usd / Σ bought``); FIFO matching is left to the
realized PnL pipeline and stored separately.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import TokenInfo, TokenTraderRow

log = logging.getLogger(__name__)

# DB lives under ``backend/data/analytics.db`` by default. Operators can point
# this elsewhere (e.g. a mounted volume) via ``TON_PNL_ANALYTICS_DB_PATH``.
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "analytics.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_token_stats (
    wallet TEXT NOT NULL,
    token_master TEXT NOT NULL,
    pool_address TEXT NOT NULL,
    token_symbol TEXT,
    token_name TEXT,
    token_decimals INTEGER,
    token_image TEXT,
    total_bought REAL NOT NULL,
    total_sold REAL NOT NULL,
    estimated_balance REAL NOT NULL,
    buy_volume_usd REAL NOT NULL,
    sell_volume_usd REAL NOT NULL,
    avg_buy_price_usd REAL,
    current_price_usd REAL,
    current_value_usd REAL NOT NULL,
    realized_pnl_usd REAL NOT NULL,
    unrealized_pnl_usd REAL NOT NULL,
    total_pnl_usd REAL NOT NULL,
    trade_count INTEGER NOT NULL,
    first_trade_ts INTEGER,
    last_trade_ts INTEGER,
    only_sells INTEGER NOT NULL DEFAULT 0,
    sold_more_than_bought INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (wallet, token_master)
);

CREATE INDEX IF NOT EXISTS idx_wts_token ON wallet_token_stats(token_master);
CREATE INDEX IF NOT EXISTS idx_wts_wallet ON wallet_token_stats(wallet);
CREATE INDEX IF NOT EXISTS idx_wts_total_pnl ON wallet_token_stats(total_pnl_usd);
CREATE INDEX IF NOT EXISTS idx_wts_updated_at ON wallet_token_stats(updated_at);
"""


@dataclass
class WalletTokenStat:
    """Flat row matching ``wallet_token_stats`` (one entry per wallet+token)."""

    wallet: str
    token_master: str
    pool_address: str
    token_symbol: str | None
    token_name: str | None
    token_decimals: int | None
    token_image: str | None
    total_bought: float
    total_sold: float
    estimated_balance: float
    buy_volume_usd: float
    sell_volume_usd: float
    avg_buy_price_usd: float | None
    current_price_usd: float | None
    current_value_usd: float
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    total_pnl_usd: float
    trade_count: int
    first_trade_ts: int | None
    last_trade_ts: int | None
    only_sells: bool
    sold_more_than_bought: bool
    updated_at: int


class AnalyticsDB:
    """Single-process, single-file SQLite store with sync helpers.

    SQLite is fine for our scale (low write rate, infrequent queries from the
    UI). All writes serialize through one ``threading.Lock`` because sqlite3
    connections are not safe to share across asyncio tasks. Heavier callers
    should prefer the ``async`` wrappers which delegate to ``asyncio.to_thread``
    so they never block the event loop.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        path = Path(db_path) if db_path else DEFAULT_DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = asyncio.Lock()
        # Initialise schema synchronously on first use; idempotent.
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def _connect(self):  # noqa: ANN201 - generator type
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _row_from_trader(
        row: TokenTraderRow,
        *,
        pool_address: str,
        now: int,
    ) -> tuple[Any, ...]:
        token: TokenInfo = row.token
        return (
            row.wallet,
            token.asset_id,
            pool_address,
            token.symbol,
            token.name,
            token.decimals,
            token.image,
            row.total_bought,
            row.total_sold,
            row.estimated_balance,
            row.buy_volume_usd,
            row.sell_volume_usd,
            row.avg_buy_price_usd,
            row.current_price_usd,
            row.current_value_usd,
            row.realized_pnl_usd,
            row.unrealized_pnl_usd,
            row.total_pnl_usd,
            row.trade_count,
            row.first_trade_ts,
            row.last_trade_ts,
            int(row.only_sells),
            int(row.sold_more_than_bought),
            now,
        )

    def upsert_trader_rows_sync(
        self,
        rows: Iterable[TokenTraderRow],
        *,
        pool_address: str,
    ) -> int:
        rows_list = list(rows)
        if not rows_list:
            return 0
        now = int(time.time())
        payload = [self._row_from_trader(r, pool_address=pool_address, now=now) for r in rows_list]
        sql = """
            INSERT INTO wallet_token_stats (
                wallet, token_master, pool_address,
                token_symbol, token_name, token_decimals, token_image,
                total_bought, total_sold, estimated_balance,
                buy_volume_usd, sell_volume_usd, avg_buy_price_usd,
                current_price_usd, current_value_usd,
                realized_pnl_usd, unrealized_pnl_usd, total_pnl_usd,
                trade_count, first_trade_ts, last_trade_ts,
                only_sells, sold_more_than_bought, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?
            )
            ON CONFLICT(wallet, token_master) DO UPDATE SET
                pool_address=excluded.pool_address,
                token_symbol=excluded.token_symbol,
                token_name=excluded.token_name,
                token_decimals=excluded.token_decimals,
                token_image=excluded.token_image,
                total_bought=excluded.total_bought,
                total_sold=excluded.total_sold,
                estimated_balance=excluded.estimated_balance,
                buy_volume_usd=excluded.buy_volume_usd,
                sell_volume_usd=excluded.sell_volume_usd,
                avg_buy_price_usd=excluded.avg_buy_price_usd,
                current_price_usd=excluded.current_price_usd,
                current_value_usd=excluded.current_value_usd,
                realized_pnl_usd=excluded.realized_pnl_usd,
                unrealized_pnl_usd=excluded.unrealized_pnl_usd,
                total_pnl_usd=excluded.total_pnl_usd,
                trade_count=excluded.trade_count,
                first_trade_ts=excluded.first_trade_ts,
                last_trade_ts=excluded.last_trade_ts,
                only_sells=excluded.only_sells,
                sold_more_than_bought=excluded.sold_more_than_bought,
                updated_at=excluded.updated_at
        """
        with self._connect() as conn:
            conn.execute("BEGIN")
            conn.executemany(sql, payload)
            conn.commit()
        return len(payload)

    async def upsert_trader_rows(
        self,
        rows: Iterable[TokenTraderRow],
        *,
        pool_address: str,
    ) -> int:
        async with self._lock:
            return await asyncio.to_thread(
                self.upsert_trader_rows_sync,
                list(rows),
                pool_address=pool_address,
            )

    def query_sync(
        self,
        *,
        token_master: str | None = None,
        wallet: str | None = None,
        min_total_pnl_usd: float | None = None,
        max_total_pnl_usd: float | None = None,
        only_with_balance: bool = False,
        sort: str = "total_pnl_desc",
        limit: int = 200,
        offset: int = 0,
    ) -> list[WalletTokenStat]:
        clauses: list[str] = []
        params: list[Any] = []
        if token_master:
            clauses.append("LOWER(token_master) = LOWER(?)")
            params.append(token_master)
        if wallet:
            clauses.append("LOWER(wallet) = LOWER(?)")
            params.append(wallet)
        if min_total_pnl_usd is not None:
            clauses.append("total_pnl_usd >= ?")
            params.append(min_total_pnl_usd)
        if max_total_pnl_usd is not None:
            clauses.append("total_pnl_usd <= ?")
            params.append(max_total_pnl_usd)
        if only_with_balance:
            clauses.append("estimated_balance > 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        order_by_map = {
            "total_pnl_desc": "total_pnl_usd DESC",
            "total_pnl_asc": "total_pnl_usd ASC",
            "realized_desc": "realized_pnl_usd DESC",
            "realized_asc": "realized_pnl_usd ASC",
            "unrealized_desc": "unrealized_pnl_usd DESC",
            "buy_usd_desc": "buy_volume_usd DESC",
            "trade_count_desc": "trade_count DESC",
            "last_trade_desc": "COALESCE(last_trade_ts, 0) DESC",
            "updated_desc": "updated_at DESC",
        }
        order_by = order_by_map.get(sort, order_by_map["total_pnl_desc"])
        limit = max(1, min(limit, 5000))
        offset = max(0, offset)

        sql = f"""
            SELECT * FROM wallet_token_stats
            {where}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_stat(r) for r in rows]

    async def query(
        self,
        **kwargs: Any,
    ) -> list[WalletTokenStat]:
        return await asyncio.to_thread(self.query_sync, **kwargs)

    def list_tokens_sync(self) -> list[dict[str, Any]]:
        sql = """
            SELECT
                token_master,
                MAX(token_symbol) AS token_symbol,
                MAX(token_name) AS token_name,
                MAX(token_image) AS token_image,
                MAX(token_decimals) AS token_decimals,
                COUNT(*) AS wallet_count,
                SUM(buy_volume_usd) AS total_buy_usd,
                SUM(sell_volume_usd) AS total_sell_usd,
                MAX(updated_at) AS last_updated
            FROM wallet_token_stats
            GROUP BY token_master
            ORDER BY MAX(updated_at) DESC
        """
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    async def list_tokens(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_tokens_sync)

    def stats_sync(self) -> dict[str, Any]:
        sql = """
            SELECT
                COUNT(*) AS row_count,
                COUNT(DISTINCT wallet) AS wallet_count,
                COUNT(DISTINCT token_master) AS token_count,
                MAX(updated_at) AS last_updated
            FROM wallet_token_stats
        """
        with self._connect() as conn:
            row = conn.execute(sql).fetchone()
        if row is None:
            return {"row_count": 0, "wallet_count": 0, "token_count": 0, "last_updated": None}
        return dict(row)

    async def stats(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.stats_sync)

    @staticmethod
    def _row_to_stat(row: sqlite3.Row) -> WalletTokenStat:
        return WalletTokenStat(
            wallet=row["wallet"],
            token_master=row["token_master"],
            pool_address=row["pool_address"],
            token_symbol=row["token_symbol"],
            token_name=row["token_name"],
            token_decimals=row["token_decimals"],
            token_image=row["token_image"],
            total_bought=row["total_bought"],
            total_sold=row["total_sold"],
            estimated_balance=row["estimated_balance"],
            buy_volume_usd=row["buy_volume_usd"],
            sell_volume_usd=row["sell_volume_usd"],
            avg_buy_price_usd=row["avg_buy_price_usd"],
            current_price_usd=row["current_price_usd"],
            current_value_usd=row["current_value_usd"],
            realized_pnl_usd=row["realized_pnl_usd"],
            unrealized_pnl_usd=row["unrealized_pnl_usd"],
            total_pnl_usd=row["total_pnl_usd"],
            trade_count=row["trade_count"],
            first_trade_ts=row["first_trade_ts"],
            last_trade_ts=row["last_trade_ts"],
            only_sells=bool(row["only_sells"]),
            sold_more_than_bought=bool(row["sold_more_than_bought"]),
            updated_at=row["updated_at"],
        )


_singleton: AnalyticsDB | None = None


def get_analytics_db() -> AnalyticsDB:
    """Return a process-wide ``AnalyticsDB`` (lazy init)."""
    global _singleton
    if _singleton is None:
        from .settings import settings

        custom_path = getattr(settings, "analytics_db_path", None)
        _singleton = AnalyticsDB(custom_path or None)
    return _singleton


def reset_analytics_db_for_tests(path: str | Path | None = None) -> AnalyticsDB:
    """Replace the singleton with a fresh instance pointing at ``path``."""
    global _singleton
    _singleton = AnalyticsDB(path)
    return _singleton


__all__ = [
    "AnalyticsDB",
    "DEFAULT_DB_PATH",
    "WalletTokenStat",
    "get_analytics_db",
    "reset_analytics_db_for_tests",
]
