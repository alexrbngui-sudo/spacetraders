"""Tests for SharedRateLimiter."""

import asyncio
import time
from pathlib import Path

import pytest

from spacetraders.rate_limiter import SharedRateLimiter


@pytest.fixture
def limiter(tmp_path: Path) -> SharedRateLimiter:
    rl = SharedRateLimiter(db_path=tmp_path / "rl.db", rate=2.0, burst=10)
    yield rl
    rl.close()


class TestSharedRateLimiter:
    async def test_acquire_returns_immediately_when_tokens_available(
        self, limiter: SharedRateLimiter,
    ) -> None:
        """Fresh limiter has burst tokens — should not block."""
        start = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

    async def test_exhausting_tokens_causes_wait(self, tmp_path: Path) -> None:
        """After burst is exhausted, acquire should block."""
        rl = SharedRateLimiter(db_path=tmp_path / "rl2.db", rate=10.0, burst=2)
        try:
            # Drain the 2 burst tokens
            await rl.acquire()
            await rl.acquire()
            # Third should wait ~0.1s (1 token / 10 per sec)
            start = time.monotonic()
            await rl.acquire()
            elapsed = time.monotonic() - start
            assert elapsed >= 0.05, f"Should have waited, only {elapsed:.3f}s"
        finally:
            rl.close()

    async def test_two_limiters_share_state(self, tmp_path: Path) -> None:
        """Two SharedRateLimiter instances on the same DB share tokens."""
        db = tmp_path / "shared.db"
        rl1 = SharedRateLimiter(db_path=db, rate=10.0, burst=3)
        rl2 = SharedRateLimiter(db_path=db, rate=10.0, burst=3)
        try:
            # rl1 takes 3 tokens (exhausts burst)
            await rl1.acquire()
            await rl1.acquire()
            await rl1.acquire()
            # rl2 should have to wait — same token pool
            start = time.monotonic()
            await rl2.acquire()
            elapsed = time.monotonic() - start
            assert elapsed >= 0.05, f"Should share tokens, only {elapsed:.3f}s"
        finally:
            rl1.close()
            rl2.close()

    def test_close_is_safe(self, tmp_path: Path) -> None:
        """Closing the limiter should not raise."""
        rl = SharedRateLimiter(db_path=tmp_path / "close.db")
        rl.close()
