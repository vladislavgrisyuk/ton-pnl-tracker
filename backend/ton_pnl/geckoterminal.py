"""Async wrapper around the public GeckoTerminal API.

GeckoTerminal exposes on-chain DEX data for many networks, including TON. The
free tier is limited to ~10 requests per minute per IP, so we cache aggressively
on top of the wrapper. The functions here only return *raw* data; downstream
code is responsible for joining OHLCV candles to swap timestamps.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .settings import settings

log = logging.getLogger(__name__)

# Address GeckoTerminal uses to represent native TON in pool relationships.
TON_ZERO_ADDRESS = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"


class GeckoTerminalError(RuntimeError):
    pass


class GeckoTerminalClient:
    """Light wrapper for the public GeckoTerminal endpoints we use."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        self._base_url = (base_url or settings.geckoterminal_base_url).rstrip("/")
        self._network = settings.geckoterminal_network
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Accept": "application/json;version=20230203"},
            timeout=timeout or settings.request_timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GeckoTerminalClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        backoff = 1.0
        for attempt in range(4):
            try:
                resp = await self._client.get(path, params=params)
            except httpx.RequestError as exc:
                if attempt == 3:
                    raise GeckoTerminalError(f"network error: {exc}") from exc
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return {}
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                log.warning("geckoterminal %s -> %s, retrying", path, resp.status_code)
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            raise GeckoTerminalError(
                f"geckoterminal {path} returned {resp.status_code}: {resp.text[:200]}"
            )
        raise GeckoTerminalError("geckoterminal request exhausted retries")

    async def top_pools_for_token(self, token_address: str) -> list[dict[str, Any]]:
        data = await self._get(
            f"/networks/{self._network}/tokens/{token_address}/pools",
            params={"page": 1},
        )
        return data.get("data") or []

    async def token_info(self, token_address: str) -> dict[str, Any]:
        data = await self._get(f"/networks/{self._network}/tokens/{token_address}")
        return (data or {}).get("data") or {}

    async def simple_token_prices_usd(self, addresses: list[str]) -> dict[str, float]:
        """Return current USD prices keyed by lowercase token address."""
        if not addresses:
            return {}
        # GeckoTerminal accepts up to 30 addresses joined by commas.
        chunks = [addresses[i : i + 30] for i in range(0, len(addresses), 30)]
        out: dict[str, float] = {}
        for chunk in chunks:
            joined = ",".join(chunk)
            data = await self._get(f"/simple/networks/{self._network}/token_price/{joined}")
            prices = (data.get("data") or {}).get("attributes", {}).get("token_prices") or {}
            for addr, price in prices.items():
                try:
                    out[addr.lower()] = float(price)
                except (TypeError, ValueError):
                    continue
        return out

    async def pool_ohlcv(
        self,
        pool_address: str,
        *,
        timeframe: str = "minute",
        aggregate: int = 1,
        before_timestamp: int | None = None,
        limit: int = 1000,
        currency: str = "usd",
    ) -> list[list[float]]:
        """Return OHLCV candles for the given pool.

        Each row is ``[ts, open, high, low, close, volume]``.
        """
        params: dict[str, Any] = {
            "aggregate": aggregate,
            "limit": limit,
            "currency": currency,
        }
        if before_timestamp is not None:
            params["before_timestamp"] = before_timestamp
        data = await self._get(
            f"/networks/{self._network}/pools/{pool_address}/ohlcv/{timeframe}",
            params=params,
        )
        attributes = (data.get("data") or {}).get("attributes") or {}
        return attributes.get("ohlcv_list") or []
