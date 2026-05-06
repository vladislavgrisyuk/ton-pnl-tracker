"""Unit tests for the token analytics aggregator and pool event parser."""

from __future__ import annotations

import math

from ton_pnl.models import TON_ASSET_ID, Swap, TokenInfo
from ton_pnl.token_analytics import _build_rows, extract_pool_trader_swaps

JETTON_ADDR = "0:abcd00000000000000000000000000000000000000000000000000000000abcd"
JETTON = TokenInfo(asset_id=JETTON_ADDR, symbol="FOO", name="Foo Token", decimals=9)
TON_TOKEN = TokenInfo(asset_id=TON_ASSET_ID, symbol="TON", name="Toncoin", decimals=9)


def _buy_swap(wallet: str, ts: int, ton_amount: float, jetton_amount: float, usd: float) -> Swap:
    return Swap(
        timestamp=ts,
        event_id=f"buy-{ts}",
        dex="dedust",
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


def _sell_swap(wallet: str, ts: int, jetton_amount: float, ton_amount: float, usd: float) -> Swap:
    return Swap(
        timestamp=ts,
        event_id=f"sell-{ts}",
        dex="dedust",
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


def test_build_rows_full_round_trip() -> None:
    from ton_pnl.token_analytics import TraderSwap

    wallet = "0:1111111111111111111111111111111111111111111111111111111111111111"
    swaps = [
        TraderSwap(wallet=wallet, swap=_buy_swap(wallet, 1, 10, 100, usd=50)),
        TraderSwap(wallet=wallet, swap=_sell_swap(wallet, 2, 100, 15, usd=80)),
    ]
    rows = _build_rows(swaps, token=JETTON, current_price_usd=1.0)
    assert len(rows) == 1
    row = rows[0]
    assert row.wallet == wallet
    assert math.isclose(row.total_bought, 100.0)
    assert math.isclose(row.total_sold, 100.0)
    assert math.isclose(row.estimated_balance, 0.0, abs_tol=1e-9)
    assert math.isclose(row.realized_pnl_usd, 30.0)
    assert math.isclose(row.unrealized_pnl_usd, 0.0)
    assert math.isclose(row.total_pnl_usd, 30.0)
    assert row.trade_count == 2
    assert row.only_sells is False
    assert row.sold_more_than_bought is False


def test_build_rows_only_sells_flagged() -> None:
    from ton_pnl.token_analytics import TraderSwap

    wallet = "0:2222222222222222222222222222222222222222222222222222222222222222"
    swaps = [TraderSwap(wallet=wallet, swap=_sell_swap(wallet, 1, 50, 20, usd=100))]
    rows = _build_rows(swaps, token=JETTON, current_price_usd=2.0)
    row = rows[0]
    assert row.only_sells is True
    assert row.sold_more_than_bought is True
    assert math.isclose(row.total_sold, 50.0)
    assert math.isclose(row.realized_pnl_usd, 100.0)
    # Negative balance must not contribute to current value.
    assert math.isclose(row.unrealized_pnl_usd, 0.0)
    assert math.isclose(row.total_pnl_usd, 100.0)


def test_build_rows_partial_position_unrealized() -> None:
    from ton_pnl.token_analytics import TraderSwap

    wallet = "0:3333333333333333333333333333333333333333333333333333333333333333"
    swaps = [
        TraderSwap(wallet=wallet, swap=_buy_swap(wallet, 1, 10, 100, usd=100)),
        TraderSwap(wallet=wallet, swap=_sell_swap(wallet, 2, 40, 6, usd=60)),
    ]
    rows = _build_rows(swaps, token=JETTON, current_price_usd=2.0)
    row = rows[0]
    assert math.isclose(row.estimated_balance, 60.0)
    assert math.isclose(row.realized_pnl_usd, -40.0)
    assert math.isclose(row.current_value_usd, 120.0)
    assert math.isclose(row.unrealized_pnl_usd, 120.0)
    assert math.isclose(row.total_pnl_usd, 80.0)


def test_build_rows_sorted_by_last_trade() -> None:
    from ton_pnl.token_analytics import TraderSwap

    a = "0:a" + "0" * 63
    b = "0:b" + "0" * 63
    swaps = [
        TraderSwap(wallet=a, swap=_buy_swap(a, 1, 1, 10, usd=10)),
        TraderSwap(wallet=a, swap=_sell_swap(a, 2, 10, 2, usd=20)),
        TraderSwap(wallet=b, swap=_buy_swap(b, 3, 1, 10, usd=20)),
        TraderSwap(wallet=b, swap=_sell_swap(b, 4, 10, 0.5, usd=5)),
    ]
    rows = _build_rows(swaps, token=JETTON, current_price_usd=0.0)
    # Sorted by last_trade_ts descending: b (ts=4) before a (ts=2)
    assert [row.wallet for row in rows] == [b, a]


def test_extract_pool_trader_swaps_attributes_user_wallet() -> None:
    user = "0:ef5884b7694d01abd0bb6ba27009560429c3272d46a20009fd6c36b7f7a9e38d"
    other = "0:e5898916f91ca8d0210fa53873c5b1d3f7b362bbd04e1cbc2276ca1f0a3e90bf"
    events = [
        {
            "event_id": "ev1",
            "timestamp": 1778058310,
            "actions": [
                {
                    "type": "JettonSwap",
                    "status": "ok",
                    "JettonSwap": {
                        "dex": "dedust",
                        "amount_in": "",
                        "amount_out": "578329329981423",
                        "ton_in": 100_000_000_000,
                        "user_wallet": {"address": user, "is_wallet": True},
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
        }
    ]
    swaps = extract_pool_trader_swaps(events)
    assert len(swaps) == 1
    assert swaps[0].wallet == user
    assert swaps[0].swap.asset_in.asset_id == TON_ASSET_ID
    assert swaps[0].swap.asset_out.asset_id == JETTON_ADDR


def test_extract_pool_trader_swaps_handles_flawed_transfer_buy() -> None:
    user = "0:ef5884b7694d01abd0bb6ba27009560429c3272d46a20009fd6c36b7f7a9e38d"
    routing_contract = "0:dae153a74d894bbc32748198cd626e4f5df4a69ad2fa56ce80fc2644b5708d20"
    events = [
        {
            "event_id": "ev2",
            "timestamp": 1778060792,
            "account": {"address": user, "is_wallet": True},
            "actions": [
                {
                    "type": "SmartContractExec",
                    "status": "ok",
                    "SmartContractExec": {
                        "executor": {"address": user, "is_wallet": True},
                        "contract": {"address": routing_contract},
                        "ton_attached": 5_200_000_000,
                    },
                },
                {
                    "type": "FlawedJettonTransfer",
                    "status": "ok",
                    "FlawedJettonTransfer": {
                        "sender": {"address": routing_contract},
                        "recipient": {"address": user, "is_wallet": True},
                        "received_amount": "100000000000",
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
    swaps = extract_pool_trader_swaps(events)
    assert len(swaps) == 1
    assert swaps[0].wallet == user
    assert swaps[0].swap.asset_in.asset_id == TON_ASSET_ID
    assert swaps[0].swap.asset_out.asset_id == JETTON_ADDR
    assert math.isclose(swaps[0].swap.amount_in, 5.2)
    assert math.isclose(swaps[0].swap.amount_out, 100.0)
