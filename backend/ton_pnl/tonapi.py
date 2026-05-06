"""Thin async wrapper around the public REST API at tonapi.io.

Only the endpoints needed by the PnL pipeline are implemented. The OpenAPI spec
of TonAPI is large and the action schema is technically unstable, so we keep the
parser tolerant of missing fields.

The same client also supports the **tonviewer.com frontend proxy** as an
alternative source. tonviewer wraps tonapi responses in CryptoJS-style AES
(``Salted__`` base64); when the base URL points at tonviewer we automatically
decrypt the body before parsing JSON.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any

import httpx

from .settings import settings
from .tonviewer_crypt import decrypt_tonviewer_payload

log = logging.getLogger(__name__)


class TonApiError(RuntimeError):
    """Raised when tonapi.io returns a non-2xx response we cannot recover from."""


class TonApiClient:
    """Async client for tonapi.io.

    The free tier of tonapi.io is rate-limited; pass an API token via
    ``TON_PNL_TONAPI_TOKEN`` to lift the limits when running this in production.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_cookies: dict[str, str] | None = None,
        rps: float | None = None,
    ) -> None:
        self._base_url = (base_url or settings.tonapi_base_url).rstrip("/")
        self._token = token if token is not None else settings.tonapi_token
        self._timeout = timeout or settings.request_timeout_seconds
        self._rps = rps if rps is not None else settings.tonapi_rps
        self._window_seconds = 1.0 if self._rps >= 1 else 1 / self._rps if self._rps > 0 else 0
        self._max_requests_per_window = int(self._rps) if self._rps >= 1 else 1
        self._request_times: deque[float] = deque()
        self._rate_lock = asyncio.Lock()
        # Build the header set: a permissive Accept first, then operator-supplied
        # overrides (browser fingerprint when proxying through tonviewer), then
        # the bearer token last so it always wins over a stray Authorization.
        headers: dict[str, str] = {"Accept": "application/json"}
        if extra_headers is None:
            extra_headers = settings.tonapi_headers
        if extra_headers:
            headers.update(extra_headers)
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        cookies = extra_cookies if extra_cookies is not None else settings.tonapi_cookies
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            cookies=cookies or None,
            timeout=self._timeout,
        )
        # Heuristic: if the configured base URL points at tonviewer's frontend
        # proxy, expect AES-wrapped responses and decrypt them transparently.
        # ``TON_PNL_TONVIEWER_PASSPHRASE`` lets operators override the key.
        self._tonviewer_mode = "tonviewer.com" in self._base_url
        self._tonviewer_passphrase = settings.tonviewer_passphrase

    async def _throttle(self) -> None:
        if self._rps <= 0:
            return
        while True:
            async with self._rate_lock:
                now = time.monotonic()
                cutoff = now - self._window_seconds
                while self._request_times and self._request_times[0] <= cutoff:
                    self._request_times.popleft()
                if len(self._request_times) < self._max_requests_per_window:
                    self._request_times.append(now)
                    return
                wait_for = self._window_seconds - (now - self._request_times[0])
            await asyncio.sleep(max(wait_for, 0.001))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> TonApiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _decode_body(self, body: str, path: str) -> dict[str, Any]:
        """Parse a 200 OK body — decrypt first when running in tonviewer mode."""

        if self._tonviewer_mode:
            try:
                plaintext = decrypt_tonviewer_payload(body, self._tonviewer_passphrase)
            except ValueError:
                # tonviewer occasionally returns plain JSON (errors, redirects).
                # Fall through and let json.loads fail loudly if it really is bad.
                log.debug("tonviewer payload not encrypted at %s, falling back to plain JSON", path)
                return json.loads(body)
            return json.loads(plaintext)
        return json.loads(body)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        # Retry transient 429 / 5xx with exponential backoff.
        backoff = 0.5
        for attempt in range(5):
            try:
                await self._throttle()
                resp = await self._client.get(path, params=params)
            except httpx.RequestError as exc:
                if attempt == 4:
                    raise TonApiError(f"network error talking to tonapi: {exc}") from exc
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code == 200:
                try:
                    return self._decode_body(resp.text, path)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise TonApiError(f"tonapi {path} returned malformed body: {exc}") from exc
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                log.warning("tonapi %s -> %s, retrying", path, resp.status_code)
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            raise TonApiError(f"tonapi {path} returned {resp.status_code}: {resp.text[:200]}")
        raise TonApiError("tonapi request exhausted retries")

    async def get_account(self, address: str) -> dict[str, Any]:
        return await self._get(f"/v2/accounts/{address}")

    async def iter_events(
        self,
        address: str,
        *,
        limit: int | None = None,
        max_events: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch up to ``max_events`` events for the account, paginating with before_lt."""

        cap = max_events if max_events is not None else settings.max_events_per_wallet
        page_size = limit or settings.tonapi_event_batch_size
        events: list[dict[str, Any]] = []
        before_lt: int | None = None
        while len(events) < cap:
            page_events, next_from = await self.get_events_page(
                address,
                limit=min(page_size, cap - len(events)),
                before_lt=before_lt,
            )
            if not page_events:
                break
            events.extend(page_events)
            if not next_from or next_from == 0:
                break
            before_lt = next_from
        return events[:cap]

    async def get_events_page(
        self,
        address: str,
        *,
        limit: int,
        before_lt: int | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        params: dict[str, Any] = {"limit": limit, "sort_order": "desc"}
        if before_lt is not None:
            params["before_lt"] = before_lt
        page = await self._get(f"/v2/accounts/{address}/events", params=params)
        return page.get("events") or [], page.get("next_from")

    async def get_jetton_balances(self, address: str) -> list[dict[str, Any]]:
        data = await self._get(f"/v2/accounts/{address}/jettons")
        return data.get("balances") or []
