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


def test_extract_swaps_collapses_pton_to_ton() -> None:
    wallet = "0:1111111111111111111111111111111111111111111111111111111111111111"
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
                        "amount_in": "1000000000",
                        "amount_out": "2000000000",
                        "user_wallet": {"address": wallet},
                        "jetton_master_in": {
                            "address": JETTON_ADDR,
                            "name": "Foo",
                            "symbol": "FOO",
                            "decimals": 9,
                        },
                        "jetton_master_out": {
                            "address": "EQCM3B12QK1e4yZSf8GtBRT0aLMNyEsBc_DhVfRRtOEffLez",
                            "name": "Proxy TON",
                            "symbol": "pTON",
                            "decimals": 9,
                        },
                    },
                }
            ],
        }
    ]
    swaps = extract_swaps(events, wallet_address=wallet)
    assert len(swaps) == 1
    assert swaps[0].asset_in.asset_id == JETTON_ADDR
    assert swaps[0].asset_out.asset_id == TON_ASSET_ID
    assert math.isclose(swaps[0].ton_out or 0, 2.0)


def test_extract_swaps_converts_flawed_transfer_buy() -> None:
    wallet = "0:1111111111111111111111111111111111111111111111111111111111111111"
    events = [
        {
            "event_id": "e1",
            "timestamp": 100,
            "actions": [
                {
                    "type": "SmartContractExec",
                    "status": "ok",
                    "SmartContractExec": {
                        "executor": {"address": wallet},
                        "contract": {
                            "address": "0:2222222222222222222222222222222222222222222222222222222222222222"
                        },
                        "ton_attached": 5_200_000_000,
                    },
                },
                {
                    "type": "FlawedJettonTransfer",
                    "status": "ok",
                    "FlawedJettonTransfer": {
                        "recipient": {"address": wallet},
                        "received_amount": "38062985940916",
                        "jetton": {
                            "address": JETTON_ADDR,
                            "name": "Foo",
                            "symbol": "FOO",
                            "decimals": 9,
                        },
                    },
                },
            ],
        }
    ]
    swaps = extract_swaps(events, wallet_address=wallet)
    assert len(swaps) == 1
    assert swaps[0].asset_in.asset_id == TON_ASSET_ID
    assert swaps[0].asset_out.asset_id == JETTON_ADDR
    assert math.isclose(swaps[0].amount_in, 5.2)
    assert math.isclose(swaps[0].amount_out, 38062.985940916)


def test_extract_swaps_converts_plain_transfer_buy_and_sell() -> None:
    wallet = "0:1111111111111111111111111111111111111111111111111111111111111111"
    contract = "0:2222222222222222222222222222222222222222222222222222222222222222"
    events = [
        {
            "event_id": "buy",
            "timestamp": 100,
            "actions": [
                {
                    "type": "SmartContractExec",
                    "status": "ok",
                    "SmartContractExec": {
                        "executor": {"address": wallet},
                        "contract": {"address": contract},
                        "ton_attached": 5_200_000_000,
                    },
                },
                {
                    "type": "JettonTransfer",
                    "status": "ok",
                    "JettonTransfer": {
                        "sender": {"address": contract},
                        "recipient": {"address": wallet},
                        "amount": "100000000000",
                        "jetton": {
                            "address": JETTON_ADDR,
                            "name": "Foo",
                            "symbol": "FOO",
                            "decimals": 9,
                        },
                    },
                },
            ],
        },
        {
            "event_id": "sell",
            "timestamp": 200,
            "actions": [
                {
                    "type": "JettonTransfer",
                    "status": "ok",
                    "JettonTransfer": {
                        "sender": {"address": wallet},
                        "recipient": {"address": contract},
                        "amount": "50000000000",
                        "jetton": {
                            "address": JETTON_ADDR,
                            "name": "Foo",
                            "symbol": "FOO",
                            "decimals": 9,
                        },
                    },
                },
                {
                    "type": "TonTransfer",
                    "status": "ok",
                    "TonTransfer": {
                        "sender": {"address": contract},
                        "recipient": {"address": wallet},
                        "amount": 3_000_000_000,
                    },
                },
            ],
        },
    ]
    swaps = extract_swaps(events, wallet_address=wallet)
    assert len(swaps) == 2
    assert swaps[0].asset_in.asset_id == TON_ASSET_ID
    assert swaps[0].asset_out.asset_id == JETTON_ADDR
    assert math.isclose(swaps[0].amount_in, 5.2)
    assert math.isclose(swaps[0].amount_out, 100.0)
    assert swaps[1].asset_in.asset_id == JETTON_ADDR
    assert swaps[1].asset_out.asset_id == TON_ASSET_ID
    assert math.isclose(swaps[1].amount_in, 50.0)
    assert math.isclose(swaps[1].amount_out, 3.0)


def test_extract_swaps_pool_event_buy_via_pool_sce() -> None:
    """Pool events: TON arrives via SmartContractExec on the pool itself.

    In DeDust pool events (GET /v2/accounts/{pool}/events) the executor is
    the vault/router contract, not the user's wallet, and ton_attached are
    the user's TON attached to the payout call.
    """
    pool = "0:8a1a62ca9ab30105d0790c92da556ce212361c34db5bc0703f4c0856cc377c88"
    vault = "0:39a0ecdb99629b19178a27a5ea3269670df2381c3574a60af565227eca1673dd"
    user = "0:b6b2033652c71af9a96551e47908f66e5a93821667738dab9805139eb5c44e55"
    event = {
        "event_id": "ev-pool-buy",
        "timestamp": 100,
        "account": {"address": pool, "is_wallet": False},
        "actions": [
            {
                "type": "SmartContractExec",
                "status": "ok",
                "SmartContractExec": {
                    "executor": {"address": vault, "is_wallet": False},
                    "contract": {"address": pool, "is_wallet": False},
                    "ton_attached": 198_079_058,
                    "operation": "DedustPayoutFromPool",
                    "payload": 'Amount: "17721960974981"\nRecipientAddr: 0:b6b2033652c71af9a96551e47908f66e5a93821667738dab9805139eb5c44e55',
                },
            },
            {
                "type": "JettonTransfer",
                "status": "ok",
                "JettonTransfer": {
                    "sender": {"address": pool, "is_wallet": False},
                    "recipient": {"address": user, "is_wallet": True},
                    "amount": "17721960974981",
                    "jetton": {
                        "address": JETTON_ADDR,
                        "name": "Foo",
                        "symbol": "FOO",
                        "decimals": 9,
                    },
                },
            },
        ],
    }
    swaps = extract_swaps([event], wallet_address=user)
    assert len(swaps) == 1
    assert swaps[0].asset_in.asset_id == TON_ASSET_ID
    assert swaps[0].asset_out.asset_id == JETTON_ADDR
    assert math.isclose(swaps[0].amount_in, 0.198_079_058)
    assert math.isclose(swaps[0].amount_out, 17721.960974981)
