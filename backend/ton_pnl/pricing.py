"""Pricing helpers: derive USD value of swaps and current token prices.

We convert every swap into a USD value at swap time using a two-step rule:

1. If one leg of the swap is TON, take that leg's TON amount as ground truth
   and convert via ``ton_usd_at(ts)``.
2. Otherwise, fall back to the most-liquid pool's OHLCV close price for the
   sold or received jetton at the candle covering the swap timestamp.

GeckoTerminal pools list TON as the *quote* token, so OHLCV ``currency=usd``
gives us the *base* token's USD price directly for jettons. For TON itself we
use the USDT/TON pool with ``currency=token`` (yielding USDT-in-TON) and invert,
since USDT is reliably pegged to ~$1.

Current token prices are pulled from GeckoTerminal's "simple" endpoint and used
for unrealized PnL.
"""

from __future__ import annotations

import asyncio
import bisect
import logging

from .address import to_friendly_safe
from .geckoterminal import TON_ZERO_ADDRESS, GeckoTerminalClient
from .models import TON_ASSET_ID, Swap

log = logging.getLogger(__name__)

# Daily/hourly candles are large enough to span a wallet's whole history without
# per-minute granularity. We use hourly because TON jettons can be very volatile.
DEFAULT_TIMEFRAME = "hour"


class PriceService:
    """Cached pricing service keyed by jetton master address."""

    def __init__(self, gecko: GeckoTerminalClient) -> None:
        self._gecko = gecko
        # asset_id -> sorted list[(ts, close_usd)] from a TON pool of that token
        self._series: dict[str, list[tuple[int, float]]] = {}
        # cached current prices in USD, keyed by lowercase asset address (TON for native)
        self._current_prices: dict[str, float] = {}
        self._top_pool_cache: dict[str, str | None] = {}

    async def _top_ton_pool_address(self, asset_address: str) -> str | None:
        cache_key = asset_address.lower()
        if cache_key in self._top_pool_cache:
            return self._top_pool_cache[cache_key]
        # GeckoTerminal accepts the user-friendly EQ-form, not the raw 0:hex form.
        friendly = to_friendly_safe(asset_address) or asset_address
        pools = await self._gecko.top_pools_for_token(friendly)
        chosen: str | None = None
        for pool in pools:
            attrs = pool.get("attributes") or {}
            rels = pool.get("relationships") or {}
            base = ((rels.get("base_token") or {}).get("data") or {}).get("id") or ""
            quote = ((rels.get("quote_token") or {}).get("data") or {}).get("id") or ""
            # Pick the first pool paired with native TON (zero address) for a clean
            # token-in-TON price series. Fall back to any pool if none found.
            if (
                TON_ZERO_ADDRESS.lower() in base.lower()
                or TON_ZERO_ADDRESS.lower() in quote.lower()
            ):
                chosen = attrs.get("address")
                break
        if chosen is None and pools:
            chosen = (pools[0].get("attributes") or {}).get("address")
        self._top_pool_cache[cache_key] = chosen
        return chosen

    async def _load_series(self, asset_id: str) -> list[tuple[int, float]]:
        if asset_id in self._series:
            return self._series[asset_id]

        if asset_id == TON_ASSET_ID:
            # GeckoTerminal pools list USDT as base / TON as quote, so OHLCV in
            # ``usd`` currency would give USDT prices, not TON. Use ``token``
            # currency to fetch USDT-in-TON over time and invert it.
            usdt_address = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"  # USDT jetton master
            pool = await self._top_ton_pool_address(usdt_address)
            if pool is None:
                self._series[asset_id] = []
                return []
            rows = await self._gecko.pool_ohlcv(
                pool, timeframe=DEFAULT_TIMEFRAME, limit=1000, currency="token"
            )
            series: list[tuple[int, float]] = []
            for row in rows:
                if not row or len(row) < 5:
                    continue
                close = float(row[4])
                if close <= 0:
                    continue
                # row[4] = USDT price in TON; invert to get TON price in USDT ≈ USD.
                series.append((int(row[0]), 1.0 / close))
            series.sort(key=lambda r: r[0])
            self._series[asset_id] = series
            return series

        pool = await self._top_ton_pool_address(asset_id)
        if pool is None:
            self._series[asset_id] = []
            return []

        rows = await self._gecko.pool_ohlcv(pool, timeframe=DEFAULT_TIMEFRAME, limit=1000)
        # row format: [ts, open, high, low, close, volume]; close is base-token USD price.
        series = sorted((int(r[0]), float(r[4])) for r in rows if r and len(r) >= 5)
        self._series[asset_id] = series
        return series

    @staticmethod
    def _interpolate(series: list[tuple[int, float]], ts: int) -> float | None:
        if not series:
            return None
        timestamps = [s[0] for s in series]
        idx = bisect.bisect_left(timestamps, ts)
        if idx == 0:
            return series[0][1]
        if idx >= len(series):
            return series[-1][1]
        return series[idx - 1][1]

    async def usd_price_at(self, asset_id: str, ts: int) -> float | None:
        """Best-effort USD price of one unit of ``asset_id`` at timestamp ``ts``."""
        series = await self._load_series(asset_id)
        return self._interpolate(series, ts)

    async def current_prices(self, asset_ids: list[str]) -> dict[str, float]:
        """Return a {asset_id -> USD price} map for the given assets (current)."""
        # GeckoTerminal expects friendly EQ addresses and returns them in the same form,
        # while our internal asset_ids are 0:hex (or the TON sentinel). Build a
        # bidirectional map so we can answer in the caller's id space.
        friendly_for: dict[str, str] = {}
        for asset_id in asset_ids:
            if asset_id == TON_ASSET_ID:
                friendly_for[asset_id] = TON_ZERO_ADDRESS
            else:
                friendly = to_friendly_safe(asset_id)
                if friendly:
                    friendly_for[asset_id] = friendly

        prices = await self._gecko.simple_token_prices_usd(list(friendly_for.values()))
        prices_lower = {k.lower(): v for k, v in prices.items()}

        out: dict[str, float] = {}
        for asset_id, friendly in friendly_for.items():
            value = prices_lower.get(friendly.lower())
            if value is not None:
                out[asset_id] = value
        return out


async def annotate_usd_values(
    swaps: list[Swap],
    pricing: PriceService,
) -> list[Swap]:
    """Fill in ``usd_value`` and ton-leg values for each swap, in place."""
    if not swaps:
        return swaps

    # Pre-warm series for assets we will need.
    asset_ids = {s.asset_in.asset_id for s in swaps} | {s.asset_out.asset_id for s in swaps}
    asset_ids.add(TON_ASSET_ID)
    await asyncio.gather(*(pricing._load_series(a) for a in asset_ids), return_exceptions=True)

    for swap in swaps:
        ts = swap.timestamp
        ton_usd = await pricing.usd_price_at(TON_ASSET_ID, ts)

        # Prefer the TON leg as ground truth for USD value when present.
        if swap.ton_in is not None and ton_usd is not None:
            swap.usd_value = swap.ton_in * ton_usd
        elif swap.ton_out is not None and ton_usd is not None:
            swap.usd_value = swap.ton_out * ton_usd
        else:
            # Pure jetton-jetton: try to value the bought side via its TON price.
            in_price = await pricing.usd_price_at(swap.asset_in.asset_id, ts)
            out_price = await pricing.usd_price_at(swap.asset_out.asset_id, ts)
            candidates = [
                in_price * swap.amount_in if in_price is not None else None,
                out_price * swap.amount_out if out_price is not None else None,
            ]
            valid = [c for c in candidates if c is not None and c > 0]
            if valid:
                swap.usd_value = sum(valid) / len(valid)

    return swaps
