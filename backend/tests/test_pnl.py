"""Unit tests for the FIFO PnL aggregator and the swap normalizer."""

from __future__ import annotations

import math

from ton_pnl.models import TON_ASSET_ID, Swap, TokenInfo
from ton_pnl.pnl import compute_pnl
from ton_pnl.swaps import extract_swaps

JETTON_ADDR = "0:abcd00000000000000000000000000000000000000000000000000000000abcd"
JETTON = TokenInfo(asset_id=JETTON_ADDR, symbol="FOO", name="Foo Token", decimals=9)
TON_TOKEN = TokenInfo(asset_id=TON_ASSET_ID, symbol="TON", name="Toncoin", decimals=9)


def _ton_jetton_swap(ts: int, ton_amount: float, jetton_amount: float, usd: float) -> Swap:
    """Helper: TON → jetton at the given USD value."""
    return Swap(
        timestamp=ts,
        event_id=f"ev-{ts}",
        dex="stonfi",
        asset_in=TON_TOKEN,
        asset_out=JETTON,
        amount_in_raw=int(ton_amount * 10**9),
        amount_out_raw=int(jetton_amount * 10**9),
        amount_in=ton_amount,
        amount_out=jetton_amount,
        ton_in=ton_amount,
        ton_out=None,
        usd_value=usd,
    )


def _jetton_ton_swap(ts: int, jetton_amount: float, ton_amount: float, usd: float) -> Swap:
    """Helper: jetton → TON at the given USD value."""
    return Swap(
        timestamp=ts,
        event_id=f"ev-{ts}",
        dex="stonfi",
        asset_in=JETTON,
        asset_out=TON_TOKEN,
        amount_in_raw=int(jetton_amount * 10**9),
        amount_out_raw=int(ton_amount * 10**9),
        amount_in=jetton_amount,
        amount_out=ton_amount,
        ton_in=None,
        ton_out=ton_amount,
        usd_value=usd,
    )


def test_realized_pnl_full_round_trip() -> None:
    # Buy 100 FOO for 10 TON ($50), then sell 100 FOO for 15 TON ($80).
    # Realized on FOO = +$30. TON ends with 15 units (cost basis $80, $5.33/TON);
    # at current $8/TON, unrealized on TON = 15 * 8 - 80 = $40.
    swaps = [
        _ton_jetton_swap(1, ton_amount=10, jetton_amount=100, usd=50),
        _jetton_ton_swap(2, jetton_amount=100, ton_amount=15, usd=80),
    ]
    rows, realized, unrealized = compute_pnl(
        swaps, current_prices_usd={JETTON_ADDR: 1.0, TON_ASSET_ID: 8.0}
    )
    foo_row = next(r for r in rows if r.token.asset_id == JETTON_ADDR)
    ton_row = next(r for r in rows if r.token.asset_id == TON_ASSET_ID)
    assert math.isclose(foo_row.realized_pnl_usd, 30.0, abs_tol=1e-6)
    assert math.isclose(foo_row.current_balance, 0.0, abs_tol=1e-9)
    assert math.isclose(foo_row.unrealized_pnl_usd, 0.0, abs_tol=1e-6)
    assert math.isclose(ton_row.realized_pnl_usd, 0.0, abs_tol=1e-6)
    assert math.isclose(ton_row.current_balance, 15.0, abs_tol=1e-9)
    assert math.isclose(ton_row.unrealized_pnl_usd, 40.0, abs_tol=1e-6)
    assert math.isclose(realized, 30.0, abs_tol=1e-6)
    assert math.isclose(unrealized, 40.0, abs_tol=1e-6)


def test_fifo_partial_sell() -> None:
    # Buy 100 FOO for $100 ($1 each), buy 100 FOO for $300 ($3 each),
    # sell 150 FOO for $600 ($4 each).
    # FIFO: 100 @ $1 + 50 @ $3 = $250 cost basis sold; proceeds = $600 → realized = +$350.
    # Remaining: 50 FOO @ $3 cost basis = $150 cost. At current $5 → unrealized = $250 - $150 = $100.
    swaps = [
        _ton_jetton_swap(1, ton_amount=10, jetton_amount=100, usd=100),
        _ton_jetton_swap(2, ton_amount=30, jetton_amount=100, usd=300),
        _jetton_ton_swap(3, jetton_amount=150, ton_amount=60, usd=600),
    ]
    rows, realized, unrealized = compute_pnl(
        swaps,
        current_prices_usd={JETTON_ADDR: 5.0, TON_ASSET_ID: 10.0},
        current_balances={JETTON_ADDR: 50.0},
    )
    foo_row = next(r for r in rows if r.token.asset_id == JETTON_ADDR)
    assert math.isclose(foo_row.realized_pnl_usd, 350.0, abs_tol=1e-6)
    assert math.isclose(foo_row.current_balance, 50.0)
    assert math.isclose(foo_row.cost_basis_remaining_usd, 150.0, abs_tol=1e-6)
    assert math.isclose(foo_row.unrealized_pnl_usd, 100.0, abs_tol=1e-6)
    assert math.isclose(realized, 350.0, abs_tol=1e-6)
    assert math.isclose(unrealized, 100.0, abs_tol=1e-6)


def test_oversell_with_auto_funded_cost_basis() -> None:
    # Wallet pre-funded with the token before history starts. We auto-fund the
    # missing cost basis at the swap's own USD value, so realized PnL is zero
    # rather than phantom profit. The TON received still produces a buy lot.
    swaps = [_jetton_ton_swap(1, jetton_amount=10, ton_amount=5, usd=50)]
    rows, realized, _ = compute_pnl(
        swaps, current_prices_usd={JETTON_ADDR: 5.0, TON_ASSET_ID: 10.0}
    )
    foo_row = next(r for r in rows if r.token.asset_id == JETTON_ADDR)
    assert math.isclose(foo_row.realized_pnl_usd, 0.0, abs_tol=1e-6)
    assert math.isclose(realized, 0.0, abs_tol=1e-6)


def test_extract_swaps_filters_by_user_wallet() -> None:
    wallet = "0:1111111111111111111111111111111111111111111111111111111111111111"
    other = "0:2222222222222222222222222222222222222222222222222222222222222222"
    events = [
        {
            "event_id": "e1",
            "timestamp": 100,
            "actions": [
                {
                    "type": "JettonSwap",
                    "status": "ok",
                    "JettonSwap": {
                        "dex": "stonfi",
                        "amount_in": "0",
                        "amount_out": "1000000000",
                        "ton_in": 1_000_000_000,
                        "user_wallet": {"address": wallet},
                        "router": {"address": other},
                        "jetton_master_out": {
                            "address": JETTON_ADDR,
                            "name": "Foo",
                            "symbol": "FOO",
                            "decimals": 9,
                        },
                    },
                }
            ],
        },
        {
            "event_id": "e2",
            "timestamp": 200,
            "actions": [
                {
                    "type": "JettonSwap",
                    "status": "ok",
                    "JettonSwap": {
                        "dex": "stonfi",
                        "amount_in": "0",
                        "amount_out": "1000000000",
                        "ton_in": 1_000_000_000,
                        "user_wallet": {"address": other},
                        "router": {"address": other},
                        "jetton_master_out": {
                            "address": JETTON_ADDR,
                            "name": "Foo",
                            "symbol": "FOO",
                            "decimals": 9,
                        },
                    },
                }
            ],
        },
    ]
    swaps = extract_swaps(events, wallet_address=wallet)
    assert len(swaps) == 1
    assert swaps[0].event_id == "e1"
    assert swaps[0].asset_in.asset_id == TON_ASSET_ID
    assert swaps[0].asset_out.asset_id == JETTON_ADDR


def test_extract_swaps_keeps_failed_actions_with_funds_moved() -> None:
    # STON.fi v2 multi-hop swaps sometimes show status="failed" even though
    # funds settled. We still include them because the JettonSwap payload
    # carries the post-trade amounts.
    wallet = "0:1111111111111111111111111111111111111111111111111111111111111111"
    events = [
        {
            "event_id": "e1",
            "timestamp": 100,
            "actions": [
                {
                    "type": "JettonSwap",
                    "status": "failed",
                    "JettonSwap": {
                        "dex": "stonfi",
                        "amount_in": "0",
                        "amount_out": "1000000000",
                        "ton_in": 1_000_000_000,
                        "user_wallet": {"address": wallet},
                        "jetton_master_out": {
                            "address": JETTON_ADDR,
                            "name": "Foo",
                            "symbol": "FOO",
                            "decimals": 9,
                        },
                    },
                }
            ],
        }
    ]
    swaps = extract_swaps(events, wallet_address=wallet)
    assert len(swaps) == 1
    assert swaps[0].asset_in.asset_id == TON_ASSET_ID
    assert swaps[0].asset_out.asset_id == JETTON_ADDR


def test_extract_swaps_skips_zero_amount_actions() -> None:
    wallet = "0:1111111111111111111111111111111111111111111111111111111111111111"
    events = [
        {
            "event_id": "e1",
            "timestamp": 100,
            "actions": [
                {
                    "type": "JettonSwap",
                    "status": "failed",
                    "JettonSwap": {
                        "dex": "stonfi",
                        "amount_in": "0",
                        "amount_out": "0",
                        "ton_in": 1_000_000_000,
                        "user_wallet": {"address": wallet},
                        "jetton_master_out": {
                            "address": JETTON_ADDR,
                            "name": "Foo",
                            "symbol": "FOO",
                            "decimals": 9,
                        },
                    },
                }
            ],
        }
    ]
    assert extract_swaps(events, wallet_address=wallet) == []
