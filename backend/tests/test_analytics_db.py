"""Unit tests for the SQLite analytics store and the multi-pool input parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from ton_pnl.analytics_db import AnalyticsDB
from ton_pnl.models import TokenInfo, TokenTraderRow
from ton_pnl.multi_pool import parse_pool_lines

JETTON_A = TokenInfo(
    asset_id="0:aaaa00000000000000000000000000000000000000000000000000000000aaaa",
    symbol="FOO",
    name="Foo Token",
    decimals=9,
)
JETTON_B = TokenInfo(
    asset_id="0:bbbb00000000000000000000000000000000000000000000000000000000bbbb",
    symbol="BAR",
    name="Bar Token",
    decimals=9,
)
WALLET_1 = "0:1111000000000000000000000000000000000000000000000000000000001111"
WALLET_2 = "0:2222000000000000000000000000000000000000000000000000000000002222"
POOL_A = "0:dead000000000000000000000000000000000000000000000000000000000dead"
POOL_B = "0:beef000000000000000000000000000000000000000000000000000000000beef"


def _row(
    wallet: str,
    token: TokenInfo,
    *,
    bought: float = 1000.0,
    sold: float = 0.0,
    buy_usd: float = 100.0,
    sell_usd: float = 0.0,
    realized: float = 0.0,
    unrealized: float = 0.0,
    last_ts: int | None = 1_700_000_000,
    only_sells: bool = False,
) -> TokenTraderRow:
    balance = bought - sold
    return TokenTraderRow(
        wallet=wallet,
        token=token,
        total_bought=bought,
        total_sold=sold,
        estimated_balance=balance,
        buy_volume_usd=buy_usd,
        sell_volume_usd=sell_usd,
        avg_buy_price_usd=buy_usd / bought if bought > 0 else 0.0,
        current_price_usd=0.5,
        current_value_usd=max(balance, 0.0) * 0.5,
        realized_pnl_usd=realized,
        unrealized_pnl_usd=unrealized,
        total_pnl_usd=realized + unrealized,
        trade_count=1,
        first_trade_ts=last_ts,
        last_trade_ts=last_ts,
        only_sells=only_sells,
        sold_more_than_bought=balance < 0,
    )


def test_analytics_db_creates_schema_and_upserts(tmp_path: Path) -> None:
    db = AnalyticsDB(tmp_path / "a.db")
    inserted = db.upsert_trader_rows_sync(
        [_row(WALLET_1, JETTON_A, bought=2000, buy_usd=200)],
        pool_address=POOL_A,
    )
    assert inserted == 1

    # Re-run with updated numbers; primary key on (wallet, token_master) means
    # the existing row is replaced rather than duplicated.
    inserted = db.upsert_trader_rows_sync(
        [_row(WALLET_1, JETTON_A, bought=2500, buy_usd=300, realized=10)],
        pool_address=POOL_A,
    )
    assert inserted == 1

    rows = db.query_sync(token_master=JETTON_A.asset_id)
    assert len(rows) == 1
    only = rows[0]
    assert only.total_bought == pytest.approx(2500)
    assert only.buy_volume_usd == pytest.approx(300)
    assert only.realized_pnl_usd == pytest.approx(10)
    assert only.avg_buy_price_usd == pytest.approx(300 / 2500)


def test_analytics_db_filters_and_sorts(tmp_path: Path) -> None:
    db = AnalyticsDB(tmp_path / "b.db")
    db.upsert_trader_rows_sync(
        [
            _row(WALLET_1, JETTON_A, bought=1000, buy_usd=100, realized=5),
            _row(WALLET_2, JETTON_A, bought=500, buy_usd=50, realized=-20),
            _row(WALLET_1, JETTON_B, bought=2000, buy_usd=400, realized=200),
        ],
        pool_address=POOL_A,
    )

    a_rows = db.query_sync(token_master=JETTON_A.asset_id, sort="total_pnl_desc")
    assert [r.wallet for r in a_rows] == [WALLET_1, WALLET_2]

    by_wallet = db.query_sync(wallet=WALLET_1)
    tokens = {r.token_master for r in by_wallet}
    assert tokens == {JETTON_A.asset_id, JETTON_B.asset_id}

    profitable_only = db.query_sync(min_total_pnl_usd=0)
    assert all(r.total_pnl_usd >= 0 for r in profitable_only)
    assert len(profitable_only) == 2

    summaries = db.list_tokens_sync()
    assert {s["token_master"] for s in summaries} == {
        JETTON_A.asset_id,
        JETTON_B.asset_id,
    }
    foo_summary = next(s for s in summaries if s["token_master"] == JETTON_A.asset_id)
    assert foo_summary["wallet_count"] == 2

    stats = db.stats_sync()
    assert stats["row_count"] == 3
    assert stats["wallet_count"] == 2
    assert stats["token_count"] == 2


def test_analytics_db_only_with_balance(tmp_path: Path) -> None:
    db = AnalyticsDB(tmp_path / "c.db")
    db.upsert_trader_rows_sync(
        [
            _row(WALLET_1, JETTON_A, bought=1000, sold=400, buy_usd=100),
            _row(WALLET_2, JETTON_A, bought=500, sold=500, buy_usd=50),
        ],
        pool_address=POOL_A,
    )
    held = db.query_sync(only_with_balance=True)
    assert {r.wallet for r in held} == {WALLET_1}


def test_parse_pool_lines_dedupes_and_handles_token_pipe() -> None:
    raw = (
        "EQA5oOzbmWKbGReKJ6XqMmlnDfI4HDV0pgr1ZSJ-yhZz3RYc\n"
        "EQA5oOzbmWKbGReKJ6XqMmlnDfI4HDV0pgr1ZSJ-yhZz3RYc\n"  # dup
        "  \n"
        "EQ-pool|EQ-token\n"
        "0:dead000000000000000000000000000000000000000000000000000000000dead , "
        "0:beef000000000000000000000000000000000000000000000000000000000beef"
    )
    pairs = parse_pool_lines(raw)
    assert len(pairs) == 4
    assert pairs[0][0].startswith("EQA5oOzbm")
    assert pairs[1] == ("EQ-pool", "EQ-token")
    assert pairs[2][0].startswith("0:dead")
    assert pairs[3][0].startswith("0:beef")


def test_parse_pool_lines_ignores_blank_input() -> None:
    assert parse_pool_lines("") == []
    assert parse_pool_lines("\n\n   \n") == []
