"""Cross-process rate limiter backed by SQLite.

All SpaceTraders scripts share one token bucket via a SQLite file,
ensuring the fleet collectively stays under the API's 2 req/s limit
regardless of how many processes are running.

Usage:
    # Created automatically by SpaceTradersClient from settings.data_dir
    limiter = SharedRateLimiter(db_path=Path("data/rate_limiter.db"))
    await limiter.acquire()  # blocks until a token is available
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS rate_limiter (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tokens REAL NOT NULL,
    last_refill REAL NOT NULL
)
"""


class SharedRateLimiter:
    """Cross-process token bucket rate limiter backed by SQLite.

    Uses time.time() (wall clock) so all processes share the same
    time reference. SQLite WAL mode + BEGIN IMMEDIATE serializes
    token acquisition across processes.
    """

    def __init__(
        self,
        db_path: Path,
        rate: float = 2.0,
        burst: int = 10,
    ) -> None:
        self.rate = rate
        self.burst = burst
        self._lock = asyncio.Lock()  # serialize within same process
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), timeout=5.0, check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_TABLE)
        self._conn.execute(
            "INSERT OR IGNORE INTO rate_limiter (id, tokens, last_refill) "
            "VALUES (1, ?, ?)",
            (float(burst), time.time()),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()

    async def acquire(self) -> None:
        """Block until a token is available, then consume it."""
        while True:
            async with self._lock:
                wait = self._try_acquire()
            if wait <= 0:
                return
            logger.debug("Shared rate limiter: waiting %.2fs", wait)
            await asyncio.sleep(wait)

    def _try_acquire(self) -> float:
        """Try to take a token. Returns 0.0 on success, or seconds to wait."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT tokens, last_refill FROM rate_limiter WHERE id = 1",
            ).fetchone()
            tokens, last_refill = row
            now = time.time()
            elapsed = max(0.0, now - last_refill)
            tokens = min(self.burst, tokens + elapsed * self.rate)

            if tokens >= 1.0:
                self._conn.execute(
                    "UPDATE rate_limiter SET tokens = ?, last_refill = ? WHERE id = 1",
                    (tokens - 1.0, now),
                )
                self._conn.commit()
                return 0.0

            # Not enough tokens — update state and report wait time
            self._conn.execute(
                "UPDATE rate_limiter SET tokens = ?, last_refill = ? WHERE id = 1",
                (tokens, now),
            )
            self._conn.commit()
            return (1.0 - tokens) / self.rate
        except Exception:
            self._conn.rollback()
            raise
