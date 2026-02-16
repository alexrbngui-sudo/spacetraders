"""In-process priority rate limiter for the fleet commander.

Replaces the SQLite-backed SharedRateLimiter when all ships run in one
process. Zero I/O — pure asyncio token bucket with a priority queue.

Priorities:
    CRITICAL (0) — refuel when stranded, emergency actions
    HIGH (1)     — buy/sell at market (revenue-generating)
    NORMAL (2)   — navigate, dock, orbit
    LOW (3)      — status refresh, get_ship
    BACKGROUND (4) — probe drift, idle polling
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from enum import IntEnum
from typing import AsyncIterator

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    """Request priority levels (lower = higher priority)."""

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    BACKGROUND = 4


class RequestScheduler:
    """In-memory priority rate limiter for a single-process fleet.

    Uses a token bucket refilled at `rate` tokens/sec (default 2.0 = API limit).
    Waiters are served in priority order via a PriorityQueue.
    """

    def __init__(self, rate: float = 2.0, burst: int = 10) -> None:
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._queue: asyncio.PriorityQueue[tuple[int, float, asyncio.Event]] = (
            asyncio.PriorityQueue()
        )
        self._drain_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the background drain loop. Call once after event loop is running."""
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(
                self._drain_loop(), name="scheduler-drain",
            )

    async def stop(self) -> None:
        """Stop the drain loop and wake all waiters (for shutdown)."""
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None

        # Wake everyone so they can see the shutdown signal
        while not self._queue.empty():
            try:
                _, _, event = self._queue.get_nowait()
                event.set()
            except asyncio.QueueEmpty:
                break

    async def acquire(self, priority: Priority = Priority.NORMAL) -> None:
        """Wait for a rate limit token at the given priority level."""
        # Fast path: token available and no one waiting
        self._refill()
        if self._tokens >= 1.0 and self._queue.empty():
            self._tokens -= 1.0
            return

        # Slow path: enqueue and wait
        event = asyncio.Event()
        await self._queue.put((priority.value, time.monotonic(), event))
        await event.wait()

    @asynccontextmanager
    async def priority_context(
        self, priority: Priority,
    ) -> AsyncIterator[None]:
        """Context manager that acquires at the given priority."""
        await self.acquire(priority)
        yield

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last_refill = now

    async def _drain_loop(self) -> None:
        """Background loop: refill tokens and wake highest-priority waiter."""
        try:
            while True:
                await asyncio.sleep(0.1)  # 10 Hz tick
                self._refill()

                while self._tokens >= 1.0 and not self._queue.empty():
                    try:
                        _, _, event = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    self._tokens -= 1.0
                    event.set()
        except asyncio.CancelledError:
            return
