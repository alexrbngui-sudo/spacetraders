"""Core mining loop — survey, extract, jettison junk, track yield.

Designed to run at a single asteroid until cargo is full or the
asteroid appears dry for the target resource.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from spacetraders.client import ApiError, SpaceTradersClient
from spacetraders.api import fleet, mining, navigation
from spacetraders.data.asteroid_db import AsteroidDatabase
from spacetraders.data.market_db import MarketDatabase
from spacetraders.models import Ship, Survey

logger = logging.getLogger(__name__)

# After this many consecutive non-target extractions, asteroid is "dry"
DRY_THRESHOLD = 15

# Re-survey every N extractions
SURVEY_INTERVAL = 3

# Max cooldown wait to prevent stuck sleeps (extraction cooldowns max ~70s)
MAX_COOLDOWN_WAIT = 120


@dataclass
class MiningResult:
    """Summary of a mining session at one asteroid."""

    asteroid: str
    target_resource: str
    total_extractions: int = 0
    target_hits: int = 0
    target_units_mined: int = 0
    junk_units_jettisoned: int = 0
    junk_units_sold: int = 0
    credits_earned: int = 0
    cargo_target_units: int = 0
    stopped_reason: str = ""
    consecutive_misses: int = 0

    @property
    def hit_rate(self) -> float:
        if self.total_extractions == 0:
            return 0.0
        return self.target_hits / self.total_extractions

    def summary(self) -> str:
        rate = f"{self.hit_rate:.0%}" if self.total_extractions > 0 else "N/A"
        lines = [
            f"Mining at {self.asteroid}: "
            f"{self.target_units_mined} {self.target_resource} from "
            f"{self.total_extractions} extractions (hit rate: {rate}). "
            f"Reason stopped: {self.stopped_reason}",
        ]
        if self.credits_earned > 0:
            lines.append(
                f"Earned {self.credits_earned:,} credits from selling "
                f"{self.junk_units_sold} byproduct units"
            )
        return " | ".join(lines)


async def wait_for_cooldown(client: SpaceTradersClient, ship_symbol: str) -> None:
    """Wait until ship cooldown expires."""
    cd = await fleet.get_cooldown(client, ship_symbol)
    if cd and cd.remaining_seconds > 0:
        wait = min(cd.remaining_seconds + 1, MAX_COOLDOWN_WAIT)
        if cd.remaining_seconds + 1 > MAX_COOLDOWN_WAIT:
            logger.warning("  Cooldown %ds exceeds max, clamping to %ds", cd.remaining_seconds, MAX_COOLDOWN_WAIT)
        else:
            logger.info("  Cooldown: %ds, waiting...", cd.remaining_seconds)
        await asyncio.sleep(wait)


async def ensure_orbit(client: SpaceTradersClient, ship: Ship) -> Ship:
    """Ensure ship is in orbit (required for mining)."""
    if ship.nav.status.value == "DOCKED":
        await fleet.orbit(client, ship.symbol)
        logger.info("  Moved to orbit")
        ship = await fleet.get_ship(client, ship.symbol)
    return ship


async def jettison_junk(
    client: SpaceTradersClient,
    ship_symbol: str,
    target: str,
) -> int:
    """Jettison everything that isn't the target resource. Returns units jettisoned."""
    cargo = await fleet.get_cargo(client, ship_symbol)
    jettisoned = 0
    for item in cargo.inventory:
        if item.symbol != target:
            await fleet.jettison_cargo(client, ship_symbol, item.symbol, item.units)
            logger.info("  Jettisoned %dx %s", item.units, item.symbol)
            jettisoned += item.units
    return jettisoned


async def sell_or_jettison_junk(
    client: SpaceTradersClient,
    ship_symbol: str,
    target: str,
    has_marketplace: bool,
    *,
    market_db: MarketDatabase | None = None,
    system_symbol: str = "",
    waypoint_symbol: str = "",
) -> tuple[int, int, int]:
    """Sell non-target cargo at marketplace, or jettison if unavailable.

    Returns:
        (units_sold, credits_earned, units_jettisoned)
    """
    if not has_marketplace:
        jettisoned = await jettison_junk(client, ship_symbol, target)
        return 0, 0, jettisoned

    cargo = await fleet.get_cargo(client, ship_symbol)
    junk = [item for item in cargo.inventory if item.symbol != target]
    if not junk:
        return 0, 0, 0

    # Dock to sell
    await fleet.dock(client, ship_symbol)

    units_sold = 0
    credits_earned = 0
    units_jettisoned = 0
    market_cached = False

    for item in junk:
        try:
            result = await fleet.sell_cargo(client, ship_symbol, item.symbol, item.units)
            transaction = result.get("transaction", {})
            total_price = transaction.get("totalPrice", 0)
            units_sold += item.units
            credits_earned += total_price
            balance = result.get("agent", {}).get("credits", "?")
            logger.info(
                "  Sold %dx %s for %d credits. Balance: %s",
                item.units, item.symbol, total_price,
                f"{balance:,}" if isinstance(balance, int) else balance,
            )

            # Cache market data once per session at this marketplace
            if not market_cached and market_db and system_symbol and waypoint_symbol:
                try:
                    market = await navigation.get_market(client, system_symbol, waypoint_symbol)
                    if market.trade_goods:
                        market_db.update_market(waypoint_symbol, market.trade_goods)
                    market_cached = True
                except ApiError:
                    pass  # Non-critical — skip caching

        except ApiError as e:
            logger.info(
                "  Can't sell %s at this market (%d), jettisoning",
                item.symbol, e.code,
            )
            await fleet.jettison_cargo(client, ship_symbol, item.symbol, item.units)
            logger.info("  Jettisoned %dx %s", item.units, item.symbol)
            units_jettisoned += item.units

    # Return to orbit for mining
    await fleet.orbit(client, ship_symbol)

    return units_sold, credits_earned, units_jettisoned


def count_target(ship: Ship, target: str) -> int:
    """Count units of target resource in cargo."""
    return sum(i.units for i in ship.cargo.inventory if i.symbol == target)


def pick_best_survey(surveys: list[Survey], target: str) -> Survey | None:
    """Pick survey with most deposits matching the target resource."""
    best: Survey | None = None
    best_score = 0
    now = datetime.now(timezone.utc)
    for s in surveys:
        if s.expiration <= now:
            continue
        score = sum(1 for d in s.deposits if d.symbol == target)
        if score > best_score:
            best = s
            best_score = score
    if best:
        total = len(best.deposits)
        logger.info("  Best survey: %d/%d %s deposits", best_score, total, target)
    return best


async def mine_asteroid(
    client: SpaceTradersClient,
    ship_symbol: str,
    target: str,
    cargo_target: int,
    asteroid_db: AsteroidDatabase,
    dry_threshold: int = DRY_THRESHOLD,
    *,
    has_marketplace: bool = False,
    market_db: MarketDatabase | None = None,
    system_symbol: str = "",
    waypoint_symbol: str = "",
) -> MiningResult:
    """Mine at the current asteroid until cargo full or asteroid dry.

    Args:
        client: API client
        ship_symbol: Ship to mine with
        target: Resource symbol to mine for (e.g. "COPPER_ORE")
        cargo_target: Stop when this many units of target are in cargo
        asteroid_db: Database for tracking yield stats
        dry_threshold: Stop after this many consecutive non-target extractions
        has_marketplace: If True, sell junk at local market instead of jettisoning
        market_db: Optional market cache for recording prices during sells
        system_symbol: System symbol (needed for market API calls)
        waypoint_symbol: Waypoint symbol (needed for market API calls)

    Returns:
        MiningResult with statistics
    """
    ship = await fleet.get_ship(client, ship_symbol)
    asteroid = ship.nav.waypoint_symbol

    result = MiningResult(asteroid=asteroid, target_resource=target)
    logger.info("=" * 50)
    logger.info("MINING %s at %s", target, asteroid)
    logger.info("=" * 50)

    # Ensure we're in orbit
    ship = await ensure_orbit(client, ship)

    # Clear initial cooldown
    await wait_for_cooldown(client, ship_symbol)

    # Clear junk cargo
    sold, earned, jettisoned = await sell_or_jettison_junk(
        client, ship_symbol, target, has_marketplace,
        market_db=market_db, system_symbol=system_symbol,
        waypoint_symbol=waypoint_symbol,
    )
    result.junk_units_sold += sold
    result.credits_earned += earned
    result.junk_units_jettisoned += jettisoned

    # Check starting cargo
    ship = await fleet.get_ship(client, ship_symbol)
    current_target = count_target(ship, target)
    result.cargo_target_units = current_target
    logger.info("Starting: %d/%d %s", current_target, cargo_target, target)

    if current_target >= cargo_target:
        result.stopped_reason = "already_full"
        return result

    current_survey: Survey | None = None
    consecutive_misses = 0

    while current_target < cargo_target:
        # Survey periodically
        if result.total_extractions % SURVEY_INTERVAL == 0:
            await wait_for_cooldown(client, ship_symbol)
            try:
                surveys, _ = await mining.create_survey(client, ship_symbol)
                current_survey = pick_best_survey(surveys, target)
                await wait_for_cooldown(client, ship_symbol)
            except ApiError as e:
                logger.warning("  Survey failed (%d): %s", e.code, e)
                current_survey = None
                await wait_for_cooldown(client, ship_symbol)

        # Extract
        try:
            extraction, cooldown = await mining.extract(client, ship_symbol, current_survey)
        except ApiError as e:
            if e.code == 4000:  # Cooldown
                remaining = e.data.get("cooldown", {}).get("remainingSeconds", 30)
                logger.info("  Extract cooldown: %ds", remaining)
                await asyncio.sleep(remaining + 1)
                continue
            elif e.code in (4221, 4224):  # Survey expired/invalid
                logger.info("  Survey expired, clearing")
                current_survey = None
                continue
            else:
                logger.error("  Extract error (%d): %s", e.code, e)
                result.stopped_reason = f"extract_error_{e.code}"
                break

        result.total_extractions += 1
        got_target = extraction.yield_.symbol == target

        if got_target:
            result.target_hits += 1
            result.target_units_mined += extraction.yield_.units
            consecutive_misses = 0
            logger.info(
                "  Extracted: %dx %s *** TARGET",
                extraction.yield_.units, extraction.yield_.symbol,
            )
        else:
            consecutive_misses += 1
            logger.info(
                "  Extracted: %dx %s (miss %d/%d)",
                extraction.yield_.units, extraction.yield_.symbol,
                consecutive_misses, dry_threshold,
            )

        # Track in asteroid database
        asteroid_db.record_extraction(asteroid, target, got_target)

        # Sell or jettison junk
        sold, earned, jettisoned = await sell_or_jettison_junk(
            client, ship_symbol, target, has_marketplace,
            market_db=market_db, system_symbol=system_symbol,
            waypoint_symbol=waypoint_symbol,
        )
        result.junk_units_sold += sold
        result.credits_earned += earned
        result.junk_units_jettisoned += jettisoned

        # Update cargo count
        ship = await fleet.get_ship(client, ship_symbol)
        current_target = count_target(ship, target)
        result.cargo_target_units = current_target
        logger.info("  Cargo: %d/%d %s", current_target, cargo_target, target)

        # Check dry threshold
        if consecutive_misses >= dry_threshold:
            result.stopped_reason = "asteroid_dry"
            result.consecutive_misses = consecutive_misses
            asteroid_db.blacklist(
                asteroid,
                target,
                f"Dry after {result.total_extractions} extractions "
                f"({result.hit_rate:.0%} hit rate)",
            )
            logger.warning(
                "  ASTEROID DRY: %d consecutive misses. Blacklisting %s.",
                consecutive_misses, asteroid,
            )
            break

        # Rate check every 10 extractions
        if result.total_extractions % 10 == 0 and result.total_extractions >= 10:
            rate = result.hit_rate
            logger.info(
                "  === RATE CHECK: %d/%d hits (%.0f%%) ===",
                result.target_hits, result.total_extractions, rate * 100,
            )
            if rate < 0.05:
                result.stopped_reason = "low_hit_rate"
                asteroid_db.blacklist(
                    asteroid,
                    target,
                    f"Low rate after {result.total_extractions} extractions ({rate:.0%})",
                )
                logger.warning("  Hit rate too low (%.0f%%). Blacklisting.", rate * 100)
                break

        # Wait for cooldown
        logger.info("  Cooldown: %ds", cooldown.remaining_seconds)
        await asyncio.sleep(cooldown.remaining_seconds + 1)

    if current_target >= cargo_target:
        result.stopped_reason = "cargo_full"

    logger.info("=" * 50)
    logger.info("MINING COMPLETE: %s", result.summary())
    logger.info("=" * 50)

    return result
