"""Async HTTP client with rate limiting for the SpaceTraders API."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
from pydantic import BaseModel

from spacetraders.config import Settings
from spacetraders.models import Meta
from spacetraders.rate_limiter import SharedRateLimiter

if TYPE_CHECKING:
    from spacetraders.fleet.scheduler import RequestScheduler

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class ApiError(Exception):
    """Raised when the API returns an error response."""

    def __init__(self, message: str, code: int, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


class SpaceTradersClient:
    """Async HTTP client for the SpaceTraders API."""

    MAX_RETRIES = 5
    BACKOFF_SCHEDULE = (5, 10, 20, 40, 60)
    CIRCUIT_BREAKER_THRESHOLD = 10
    CIRCUIT_BREAKER_PAUSE = 120

    def __init__(
        self,
        settings: Settings,
        *,
        scheduler: RequestScheduler | None = None,
    ) -> None:
        self.settings = settings
        self.base_url = settings.base_url.rstrip("/")
        self._scheduler = scheduler
        self._rate_limiter: SharedRateLimiter | None = None
        if scheduler is None:
            self._rate_limiter = SharedRateLimiter(
                db_path=settings.data_dir / "rate_limiter.db",
            )
        self._consecutive_failures = 0
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {settings.token}"},
            timeout=30.0,
        )

    async def close(self) -> None:
        if self._rate_limiter is not None:
            self._rate_limiter.close()
        await self._client.aclose()

    async def __aenter__(self) -> SpaceTradersClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        _retry: int = 0,
    ) -> dict[str, Any]:
        if self._scheduler is not None:
            await self._scheduler.acquire()
        elif self._rate_limiter is not None:
            await self._rate_limiter.acquire()

        # Circuit breaker: if too many consecutive failures, pause before trying
        if self._consecutive_failures >= self.CIRCUIT_BREAKER_THRESHOLD:
            logger.warning(
                "Circuit breaker: %d consecutive failures, pausing %ds...",
                self._consecutive_failures, self.CIRCUIT_BREAKER_PAUSE,
            )
            await asyncio.sleep(self.CIRCUIT_BREAKER_PAUSE)
            self._consecutive_failures = 0

        logger.debug("%s %s params=%s json=%s", method, path, params, json)
        try:
            response = await self._client.request(method, path, json=json, params=params)
        except httpx.TransportError as exc:
            self._consecutive_failures += 1
            if _retry < self.MAX_RETRIES:
                wait = self.BACKOFF_SCHEDULE[_retry]
                logger.warning(
                    "Transport error on %s %s: %s, retry %d/%d in %ds...",
                    method, path, exc, _retry + 1, self.MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                return await self._request(
                    method, path, json=json, params=params, _retry=_retry + 1,
                )
            raise

        if response.status_code == 204:
            self._consecutive_failures = 0
            return {}

        body = response.json()
        logger.debug("Response %d: %s", response.status_code, body)

        if "error" in body:
            err = body["error"]
            # API sometimes returns error as a plain string instead of dict
            if isinstance(err, str):
                code = response.status_code
                message = err
                data = {}
            else:
                code = err.get("code", response.status_code)
                message = err.get("message", "Unknown API error")
                data = err.get("data")
            # Retry on rate limit (429 / code 429)
            if (code == 429 or response.status_code == 429) and _retry < self.MAX_RETRIES:
                retry_after = response.headers.get("retry-after")
                wait = int(retry_after) if retry_after else self.BACKOFF_SCHEDULE[_retry]
                logger.warning(
                    "Rate limited on %s %s, retry %d/%d in %ds...",
                    method, path, _retry + 1, self.MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                return await self._request(
                    method, path, json=json, params=params, _retry=_retry + 1,
                )
            # Retry on server errors (3000 = server didn't return valid response)
            if (code == 3000 or response.status_code >= 500) and _retry < self.MAX_RETRIES:
                self._consecutive_failures += 1
                wait = self.BACKOFF_SCHEDULE[_retry]
                logger.warning(
                    "Server error %d on %s %s, retry %d/%d in %ds...",
                    code, method, path, _retry + 1, self.MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                return await self._request(
                    method, path, json=json, params=params, _retry=_retry + 1,
                )
            raise ApiError(message=message, code=code, data=data)

        response.raise_for_status()
        self._consecutive_failures = 0
        return body

    async def get(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request("POST", path, json=json, params=params)

    async def patch(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request("PATCH", path, json=json)

    async def get_paginated(
        self,
        path: str,
        *,
        limit: int = 20,
        params: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], Meta]:
        """Fetch all pages from a paginated endpoint."""
        all_items: list[dict[str, Any]] = []
        page = 1
        extra_params = params or {}

        while True:
            body = await self.get(
                path, params={**extra_params, "page": page, "limit": limit}
            )
            data = body.get("data", [])
            meta = Meta.model_validate(body.get("meta", {"total": 0, "page": 1, "limit": limit}))

            all_items.extend(data)

            if len(all_items) >= meta.total or not data:
                break
            page += 1

        return all_items, meta
