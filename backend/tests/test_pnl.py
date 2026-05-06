"""Unit tests for the FIFO PnL aggregator and the swap normalizer."""

from __future__ import annotations

import math

from ton_pnl.models import TON_ASSET_ID, Swap, TokenInfo
from ton_pnl.pnl import compute_pnl
from ton_pnl.swaps import extract_dedust_pool_swaps, extract_swaps

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


_DEDUST_NATIVE_PROOF = (
    "b5ee9c72010101010025000045800be0ac9f6bec08f07b6ae0639c39ecd1511a9a9adb4dbc9b6"
    "445697241adfb000021"
)
_DEDUST_JETTON_PROOF = (
    "b5ee9c72010101010046000087800be0ac9f6bec08f07b6ae0639c39ecd1511a9a9adb4dbc9b6"
    "445697241adfb00002201f376d013fbb9543e845e202d998d48f41969489d0399f74cbb68d439"
    "645512dd"
)


def _dedust_swap_external(
    *,
    kind_out: bool,
    amount_in_raw: int,
    sender: str,
    query_id: str = "1",
    proof: str | None = None,
) -> dict:
    # Default proof: TON-vault for buys (kind_out=False), jetton-vault for
    # sells (kind_out=True). Tests can override to model multi-hop traces.
    if proof is None:
        proof = _DEDUST_JETTON_PROOF if kind_out else _DEDUST_NATIVE_PROOF
    return {
        "type": "SmartContractExec",
        "status": "ok",
        "SmartContractExec": {
            "executor": {"address": "0:dae153a74d894bbc"},
            "contract": {"address": "0:39a0ecdb99629b19"},
            "ton_attached": 199_502_798,
            "operation": "DedustSwapExternal",
            "payload": (
                f'Amount: "{amount_in_raw}"\n'
                f"Current:\n"
                f"  KindOut: {'true' if kind_out else 'false'}\n"
                f'  Limit: "0"\n'
                f"  Next: null\n"
                f"Proof: {proof}\n"
                f"QueryId: {query_id}\n"
                f"SenderAddr: {sender}\n"
                f"SwapParams:\n"
                f"  Deadline: 1778069866\n"
                f"  RecipientAddr: {sender}\n"
            ),
        },
    }


def _dedust_payout_from_pool(*, amount_out_raw: int, recipient: str, query_id: str = "1") -> dict:
    return {
        "type": "SmartContractExec",
        "status": "ok",
        "SmartContractExec": {
            "executor": {"address": "0:39a0ecdb99629b19"},
            "contract": {"address": "0:8a1a62ca9ab30105"},
            "ton_attached": 198_079_058,
            "operation": "DedustPayoutFromPool",
            "payload": (
                f'Amount: "{amount_out_raw}"\n'
                f"Payload: null\n"
                f"QueryId: {query_id}\n"
                f"RecipientAddr: {recipient}\n"
            ),
        },
    }


def test_extract_dedust_pool_swaps_buy() -> None:
    """DeDust pool BUY: paired SCE actions reconstruct as TON→jetton swap."""
    user = "0:546e4b605cff5d4a022172803a0c037cc7739e6fd136b6f84ae1ebb058c0f5e4"
    event = {
        "event_id": "ev-buy",
        "timestamp": 1_700_000_000,
        "actions": [
            _dedust_swap_external(kind_out=False, amount_in_raw=20_000_000_000, sender=user),
            _dedust_payout_from_pool(amount_out_raw=76_996_315_246_370, recipient=user),
        ],
    }
    pairs = extract_dedust_pool_swaps([event], JETTON)
    assert len(pairs) == 1
    wallet, swap = pairs[0]
    assert wallet == user
    assert swap.asset_in.asset_id == TON_ASSET_ID
    assert swap.asset_out.asset_id == JETTON_ADDR
    assert math.isclose(swap.amount_in, 20.0, abs_tol=1e-9)
    assert math.isclose(swap.amount_out, 76_996.315_246_370, abs_tol=1e-3)
    assert swap.ton_in == 20.0
    assert swap.ton_out is None
    assert swap.dex == "dedust"


def test_extract_dedust_pool_swaps_sell() -> None:
    """DeDust pool SELL: KindOut=true means output is TON, input is jetton."""
    user = "0:b4d19ef7122c68d877ebb084c4c5c465285efbb954f5c67125b77679fc26cbd8"
    event = {
        "event_id": "ev-sell",
        "timestamp": 1_700_000_100,
        "actions": [
            _dedust_swap_external(kind_out=True, amount_in_raw=203_325_109_936_319, sender=user),
            _dedust_payout_from_pool(amount_out_raw=52_050_933_060, recipient=user),
        ],
    }
    pairs = extract_dedust_pool_swaps([event], JETTON)
    assert len(pairs) == 1
    wallet, swap = pairs[0]
    assert wallet == user
    assert swap.asset_in.asset_id == JETTON_ADDR
    assert swap.asset_out.asset_id == TON_ASSET_ID
    assert math.isclose(swap.amount_in, 203_325.109_936_319, abs_tol=1e-3)
    assert math.isclose(swap.amount_out, 52.050_933_060, abs_tol=1e-9)
    assert swap.ton_in is None
    assert swap.ton_out == 52.050_933_060


def test_extract_dedust_pool_swaps_skips_refund() -> None:
    """When swap-external Amount equals payout Amount the trace is a refund."""
    user = "0:11" + "0" * 60
    event = {
        "event_id": "ev-refund",
        "timestamp": 1_700_000_200,
        "actions": [
            _dedust_swap_external(kind_out=False, amount_in_raw=15_769_936_267, sender=user),
            _dedust_payout_from_pool(amount_out_raw=15_769_936_267, recipient=user),
        ],
    }
    assert extract_dedust_pool_swaps([event], JETTON) == []


def test_extract_dedust_pool_swaps_skips_multi_hop() -> None:
    """Multi-hop traces (multiple SCE pairs in one event) are intentionally
    skipped because the upstream SwapExternal Amount is denominated in some
    pre-route asset (not TON), and treating it as TON-in produces grossly
    inflated buy volume.
    """
    user_a = "0:aa" + "0" * 60
    user_b = "0:bb" + "0" * 60
    event = {
        "event_id": "ev-multi",
        "timestamp": 1_700_000_300,
        "actions": [
            _dedust_swap_external(
                kind_out=False, amount_in_raw=10_000_000_000, sender=user_a, query_id="100"
            ),
            _dedust_swap_external(
                kind_out=True, amount_in_raw=500_000_000_000, sender=user_b, query_id="200"
            ),
            _dedust_payout_from_pool(
                amount_out_raw=10_000_000_000_000, recipient=user_a, query_id="100"
            ),
            _dedust_payout_from_pool(
                amount_out_raw=99_000_000_000, recipient=user_b, query_id="200"
            ),
        ],
    }
    assert extract_dedust_pool_swaps([event], JETTON) == []


def test_extract_dedust_pool_swaps_skips_routed_buy() -> None:
    """Single-hop traces whose SwapExternal Proof claims a jetton-vault input
    (rather than the native TON vault) are the tail of a multi-hop and would
    inflate buy USD if treated as TON-in. They must be skipped.
    """
    user = "0:dd" + "0" * 60
    event = {
        "event_id": "ev-routed-buy",
        "timestamp": 1_700_000_700,
        "actions": [
            _dedust_swap_external(
                kind_out=False,
                amount_in_raw=231_492_324_667_621,  # would imply $530K of TON
                sender=user,
                proof=_DEDUST_JETTON_PROOF,
            ),
            _dedust_payout_from_pool(amount_out_raw=38_839_069_724, recipient=user),
        ],
    }
    assert extract_dedust_pool_swaps([event], JETTON) == []


def test_extract_dedust_pool_swaps_skips_jetton_master() -> None:
    """Internal contract operations from the jetton master itself aren't
    real user trades and must be filtered out.
    """
    event = {
        "event_id": "ev-master",
        "timestamp": 1_700_000_500,
        "actions": [
            _dedust_swap_external(
                kind_out=False, amount_in_raw=2_847_378_365_823_771, sender=JETTON_ADDR
            ),
            _dedust_payout_from_pool(amount_out_raw=496_381_554_251, recipient=JETTON_ADDR),
        ],
    }
    assert extract_dedust_pool_swaps([event], JETTON) == []


def test_extract_dedust_pool_swaps_requires_jetton_token() -> None:
    """Without a target jetton we cannot annotate the non-TON side and skip."""
    user = "0:cc" + "0" * 60
    event = {
        "event_id": "ev-no-token",
        "timestamp": 1_700_000_400,
        "actions": [
            _dedust_swap_external(kind_out=False, amount_in_raw=10_000_000_000, sender=user),
            _dedust_payout_from_pool(amount_out_raw=20_000_000_000, recipient=user),
        ],
    }
    assert extract_dedust_pool_swaps([event], None) == []
    assert extract_dedust_pool_swaps([event], TON_TOKEN) == []
