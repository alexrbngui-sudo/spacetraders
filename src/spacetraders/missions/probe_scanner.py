"""Probe market scanner — sends probes to visit all marketplaces in the system.

Usage:
    # Single probe (backward compatible)
    python -m spacetraders.missions.probe_scanner --ship UTMOSTLY-2
    python -m spacetraders.missions.probe_scanner --ship UTMOSTLY-2 --continuous

    # Multi-probe fleet (probes coordinate via shared market_db)
    python -m spacetraders.missions.probe_scanner --ships UTMOSTLY-2 UTMOSTLY-4 --continuous
    python -m spacetraders.missions.probe_scanner --ships UTMOSTLY-2 UTMOSTLY-4 --continuous --max-age 60

Probes are solar-powered (no fuel cost) and travel via DRIFT mode.
At each marketplace, fetches full market data and caches it to markets.db.

In continuous mode, probes loop forever:
  - Cycle 1: scans all marketplaces (full discovery pass)
  - Cycle 2+: only visits markets with stale data (older than --max-age minutes)
  - Sleeps 5 min if all data is fresh

Multi-probe coordination:
  - All probes share one market_db (SQLite WAL)
  - Before each stop, a probe re-checks freshness — skips if another probe already scanned it
  - Each probe routes independently via nearest-neighbor from its current position
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from spacetraders.client import ApiError, SpaceTradersClient
from spacetraders.config import load_settings
from spacetraders.api import fleet, navigation
from spacetraders.data.fleet_db import FleetDB
from spacetraders.data.market_db import MarketDatabase
from spacetraders.missions.router import waypoint_distance
from spacetraders.fleet_registry import ship_name
from spacetraders.models import Ship, Waypoint

logger = logging.getLogger("spacetraders.missions")

# Max wait for probe transit (DRIFT is slow)
MAX_TRANSIT_WAIT = 1800  # 30 min covers longest in-system drift

EST = ZoneInfo("America/New_York")


class ShipLogger(logging.LoggerAdapter):
    """Logger that prefixes messages with the ship nickname."""

    def process(
        self, msg: str, kwargs: dict,  # type: ignore[override]
    ) -> tuple[str, dict]:
        return f"[{self.extra['ship']}] {msg}", kwargs


def find_marketplace_waypoints(waypoints: list[Waypoint]) -> list[Waypoint]:
    """Filter waypoints that have a MARKETPLACE trait."""
    return [
        wp for wp in waypoints
        if any(t.symbol == "MARKETPLACE" for t in wp.traits)
    ]


def plan_scan_route(
    start_wp: Waypoint, markets: list[Waypoint],
) -> list[Waypoint]:
    """Order marketplaces by nearest-neighbor from starting position."""
    remaining = list(markets)
    route: list[Waypoint] = []
    current = start_wp

    while remaining:
        nearest = min(remaining, key=lambda wp: waypoint_distance(current, wp))
        route.append(nearest)
        remaining.remove(nearest)
        current = nearest

    return route


async def wait_for_probe_arrival(
    client: SpaceTradersClient, ship_symbol: str,
    log: logging.LoggerAdapter | logging.Logger | None = None,
) -> Ship:
    """Wait for probe to arrive at destination."""
    log = log or logger
    ship = await fleet.get_ship(client, ship_symbol)
    if ship.nav.status.value != "IN_TRANSIT":
        return ship

    now = datetime.now(timezone.utc)
    arrival = ship.nav.route.arrival
    raw_wait = (arrival - now).total_seconds()
    wait_secs = min(max(raw_wait + 2, 0), MAX_TRANSIT_WAIT)

    if wait_secs > 0:
        log.info(
            "  In transit -> %s, waiting %.0fs (%.1f min)",
            ship.nav.route.destination.symbol, wait_secs, wait_secs / 60,
        )
        elapsed = 0.0
        while elapsed < wait_secs:
            chunk = min(60.0, wait_secs - elapsed)
            await asyncio.sleep(chunk)
            elapsed += chunk
            if elapsed < wait_secs:
                remaining = wait_secs - elapsed
                eta = datetime.now(EST) + timedelta(seconds=remaining)
                eta_str = eta.strftime("%-I:%M %p EST")
                log.info(
                    "  [heartbeat] transit -- %.0f/%.0fs (ETA: %s)",
                    elapsed, wait_secs, eta_str,
                )

    # Poll until arrived
    for _ in range(12):
        ship = await fleet.get_ship(client, ship_symbol)
        if ship.nav.status.value != "IN_TRANSIT":
            return ship
        await asyncio.sleep(10)

    return await fleet.get_ship(client, ship_symbol)


async def scan_marketplace(
    client: SpaceTradersClient,
    ship: Ship,
    waypoint: Waypoint,
    market_db: MarketDatabase,
    log: logging.LoggerAdapter | logging.Logger | None = None,
) -> bool:
    """Navigate to a marketplace, fetch and cache market data.

    Returns True if data was cached, False on failure.
    """
    log = log or logger
    system = ship.nav.system_symbol

    # Wait for arrival if ship is mid-transit (e.g. from a previous crashed run)
    if ship.nav.status.value == "IN_TRANSIT":
        ship = await wait_for_probe_arrival(client, ship.symbol, log=log)

    # Navigate if not already there
    if ship.nav.waypoint_symbol != waypoint.symbol:
        if ship.nav.status.value == "DOCKED":
            await fleet.orbit(client, ship.symbol)

        if ship.nav.flight_mode.value != "DRIFT":
            await fleet.set_flight_mode(client, ship.symbol, "DRIFT")

        await fleet.navigate(client, ship.symbol, waypoint.symbol)
        ship = await wait_for_probe_arrival(client, ship.symbol, log=log)

        if ship.nav.waypoint_symbol != waypoint.symbol:
            log.warning("  Failed to arrive at %s", waypoint.symbol)
            return False

    log.info("  At %s (%s)", waypoint.symbol, waypoint.type.value)

    if ship.nav.status.value != "DOCKED":
        await fleet.dock(client, ship.symbol)

    try:
        market = await navigation.get_market(client, system, waypoint.symbol)
        if market.trade_goods:
            market_db.update_market(waypoint.symbol, market.trade_goods)
            log.info(
                "  Cached %d trade goods at %s",
                len(market.trade_goods), waypoint.symbol,
            )
            return True
        else:
            log.info("  No trade goods at %s (need ship docked?)", waypoint.symbol)
            return False
    except ApiError as e:
        log.warning("  Market fetch failed at %s (%d): %s", waypoint.symbol, e.code, e)
        return False


def _is_market_fresh(
    waypoint: str,
    market_db: MarketDatabase,
    max_age_min: float,
) -> bool:
    """Check if a specific market's data is fresh enough to skip."""
    prices = market_db.get_prices(waypoint)
    if not prices:
        return False
    oldest_str = min(p.updated_at for p in prices)
    oldest = datetime.fromisoformat(oldest_str)
    age_min = (datetime.now(timezone.utc) - oldest).total_seconds() / 60
    return age_min < max_age_min


def _get_stale_targets(
    all_markets: list[Waypoint],
    market_db: MarketDatabase,
    max_age_min: float,
) -> list[Waypoint]:
    """Return markets that are stale or never scanned."""
    cached_wps = set(market_db.get_all_markets())
    stale_wps = set(market_db.get_stale_markets(max_age_hours=max_age_min / 60))
    never_scanned = {wp.symbol for wp in all_markets} - cached_wps
    target_syms = stale_wps | never_scanned
    return [wp for wp in all_markets if wp.symbol in target_syms]


async def _scan_worker(
    client: SpaceTradersClient,
    ship_symbol: str,
    market_db: MarketDatabase,
    waypoints: list[Waypoint],
    markets: list[Waypoint],
    continuous: bool,
    max_age_min: float,
    shutdown: asyncio.Event,
    multi_probe: bool = False,
) -> None:
    """Core scan loop for a single probe.

    When multi_probe=True, re-checks market freshness before each stop
    to avoid duplicating work that another probe already did.
    """
    name = ship_name(ship_symbol)
    log: logging.LoggerAdapter | logging.Logger
    if multi_probe:
        log = ShipLogger(logger, {"ship": name})
    else:
        log = logger

    ship = await fleet.get_ship(client, ship_symbol)
    system = ship.nav.system_symbol

    log.info("=" * 60)
    log.info("PROBE SCANNER [%s]: %s in system %s", name, ship_symbol, system)
    if continuous:
        log.info("MODE: continuous (refresh data older than %.0f min)", max_age_min)
    if multi_probe:
        log.info("MODE: multi-probe (coordinating via shared market_db)")
    log.info("=" * 60)

    if ship.nav.status.value == "IN_TRANSIT":
        ship = await wait_for_probe_arrival(client, ship_symbol, log=log)

    cycle = 0
    while not shutdown.is_set():
        cycle += 1

        # Determine targets
        if cycle == 1:
            targets = markets
        else:
            targets = _get_stale_targets(markets, market_db, max_age_min)
            if not targets:
                log.info(
                    "All %d markets fresh (< %.0f min old). Sleeping 5 min...",
                    len(markets), max_age_min,
                )
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=300)
                except asyncio.TimeoutError:
                    pass
                continue

        # Plan route from current position
        ship = await fleet.get_ship(client, ship_symbol)
        if ship.nav.status.value == "IN_TRANSIT":
            ship = await wait_for_probe_arrival(client, ship_symbol, log=log)

        ship_wp = next(
            (wp for wp in waypoints if wp.symbol == ship.nav.waypoint_symbol),
            None,
        )
        if not ship_wp:
            log.error("Can't find ship waypoint %s", ship.nav.waypoint_symbol)
            break

        route = plan_scan_route(ship_wp, targets)
        total_dist = sum(
            waypoint_distance(route[i], route[i + 1])
            for i in range(len(route) - 1)
        )

        log.info("")
        if continuous:
            log.info(
                "--- CYCLE %d: %d/%d markets to scan, ~%.0f distance ---",
                cycle, len(route), len(markets), total_dist,
            )
        else:
            log.info(
                "Scan route: %d stops, ~%.0f total distance",
                len(route), total_dist,
            )
            for i, wp in enumerate(route, 1):
                dist = waypoint_distance(ship_wp if i == 1 else route[i - 2], wp)
                log.info("  %d. %s (%s) -- %.0f dist", i, wp.symbol, wp.type.value, dist)

        # Visit each marketplace
        scanned = 0
        skipped = 0
        for i, waypoint in enumerate(route, 1):
            if shutdown.is_set():
                log.info("Shutdown signal received.")
                break

            # Multi-probe dedup: skip if another probe already scanned this market
            if multi_probe and cycle > 1 and _is_market_fresh(waypoint.symbol, market_db, max_age_min):
                log.info("  SKIP %s (freshly scanned by another probe)", waypoint.symbol)
                skipped += 1
                continue

            log.info("")
            log.info("### STOP %d/%d: %s ###", i, len(route), waypoint.symbol)

            ship = await fleet.get_ship(client, ship_symbol)
            success = await scan_marketplace(client, ship, waypoint, market_db, log=log)
            if success:
                scanned += 1

        # Cycle summary
        log.info("")
        log.info("=" * 60)
        cached_count = len(market_db.get_all_markets())
        skip_msg = f" | {skipped} skipped (fresh)" if skipped else ""
        if continuous:
            log.info(
                "CYCLE %d COMPLETE: %d/%d scanned%s | %d markets in cache",
                cycle, scanned, len(route), skip_msg, cached_count,
            )
        else:
            log.info("SCAN COMPLETE: %d/%d marketplaces cached%s", scanned, len(route), skip_msg)
            log.info("Total markets in cache: %d", cached_count)
        log.info("=" * 60)

        if not continuous:
            break


# -- Public entry points --


async def run_scan(
    ship_symbol: str,
    continuous: bool = False,
    max_age_min: float = 90.0,
) -> None:
    """Run a single probe scanner (backward compatible)."""
    settings = load_settings()
    market_db = MarketDatabase(db_path=settings.data_dir / "markets.db")
    fleet_db = FleetDB(db_path=settings.data_dir / "fleet.db")

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    fleet_db.release_dead()
    if not fleet_db.assign(ship_symbol, "scan"):
        logger.error("Ship %s is already assigned to another mission!", ship_symbol)
        fleet_db.close()
        market_db.close()
        return

    try:
        async with SpaceTradersClient(settings) as client:
            waypoints = await navigation.list_waypoints(client, "X1-XV5")
            markets = find_marketplace_waypoints(waypoints)
            logger.info("Found %d marketplaces in X1-XV5", len(markets))

            if not markets:
                logger.error("No marketplaces found!")
                return

            await _scan_worker(
                client, ship_symbol, market_db, waypoints, markets,
                continuous, max_age_min, shutdown, multi_probe=False,
            )

    except KeyboardInterrupt:
        logger.info("Scanner interrupted by user.")
    except Exception:
        logger.exception("SCANNER CRASHED -- unexpected error")
    finally:
        fleet_db.release(ship_symbol)
        fleet_db.close()
        market_db.close()


async def run_fleet_scan(
    ship_symbols: list[str],
    continuous: bool = False,
    max_age_min: float = 90.0,
) -> None:
    """Run multiple probes concurrently. They coordinate via shared market_db."""
    settings = load_settings()
    market_db = MarketDatabase(db_path=settings.data_dir / "markets.db")
    fleet_db = FleetDB(db_path=settings.data_dir / "fleet.db")

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    fleet_db.release_dead()
    assigned: list[str] = []
    for sym in ship_symbols:
        if fleet_db.assign(sym, "scan"):
            assigned.append(sym)
        else:
            logger.warning("Probe %s unavailable — skipping", sym)

    if not assigned:
        logger.error("No probes could be assigned!")
        fleet_db.close()
        market_db.close()
        return

    try:
        async with SpaceTradersClient(settings) as client:
            waypoints = await navigation.list_waypoints(client, "X1-XV5")
            markets = find_marketplace_waypoints(waypoints)
            logger.info("Found %d marketplaces in X1-XV5", len(markets))
            logger.info("Launching %d probes: %s", len(assigned), ", ".join(assigned))

            if not markets:
                logger.error("No marketplaces found!")
                return

            # Launch one worker per probe
            tasks = []
            for ship_sym in assigned:
                task = asyncio.create_task(
                    _scan_worker(
                        client, ship_sym, market_db, waypoints, markets,
                        continuous, max_age_min, shutdown, multi_probe=True,
                    ),
                    name=f"probe-{ship_sym}",
                )
                tasks.append(task)

            # Wait for all workers (or first crash)
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

            # If a worker crashed, log it and cancel the rest
            for t in done:
                if t.exception():
                    logger.error("Probe %s crashed: %s", t.get_name(), t.exception())

            for t in pending:
                t.cancel()
            if pending:
                await asyncio.wait(pending)

    except KeyboardInterrupt:
        logger.info("Fleet scanner interrupted by user.")
    except Exception:
        logger.exception("FLEET SCANNER CRASHED -- unexpected error")
    finally:
        for sym in assigned:
            fleet_db.release(sym)
        fleet_db.close()
        market_db.close()


# -- Logging setup --


def setup_logging(
    ship_symbols: str | list[str],
    log_dir: Path | None = None,
) -> None:
    """Configure logging for the scanner."""
    if log_dir is None:
        log_dir = Path("data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    # Determine log file name
    if isinstance(ship_symbols, list) and len(ship_symbols) > 1:
        log_file = log_dir / "scanner_fleet.log"
    else:
        name = ship_symbols if isinstance(ship_symbols, str) else ship_symbols[0]
        log_file = log_dir / f"scanner_{name.lower()}.log"

    formatter = logging.Formatter(
        "[%(asctime)s] %(message)s", datefmt="%H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, mode="a")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger("spacetraders")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    logger.info("Logging to %s", log_file)


# -- CLI --


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe market scanner")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ship", help="Single probe ship symbol")
    group.add_argument(
        "--ships", nargs="+",
        help="Multiple probe ship symbols (run concurrently)",
    )
    parser.add_argument(
        "--continuous", action="store_true",
        help="Run continuously, re-scanning stale markets each cycle",
    )
    parser.add_argument(
        "--max-age", type=float, default=90.0,
        help="Re-scan markets with data older than this (minutes, default: 90)",
    )
    args = parser.parse_args()

    settings = load_settings()

    # Auto-pick from pool if no ships specified
    if not args.ship and not args.ships:
        fleet_db = FleetDB(db_path=settings.data_dir / "fleet.db")
        fleet_db.release_dead()
        available = fleet_db.available("probe")
        fleet_db.close()
        if not available:
            print("No probes available in the pool!")
            sys.exit(1)
        if len(available) > 1:
            args.ships = available
            print(f"Auto-picked {len(available)} probes: {', '.join(available)}")
        else:
            args.ship = available[0]
            print(f"Auto-picked {ship_name(available[0])} ({available[0]})")

    if args.ships and len(args.ships) > 1:
        setup_logging(args.ships, log_dir=settings.data_dir / "logs")
        asyncio.run(run_fleet_scan(args.ships, continuous=args.continuous, max_age_min=args.max_age))
    else:
        ship = args.ship or args.ships[0]
        setup_logging(ship, log_dir=settings.data_dir / "logs")
        asyncio.run(run_scan(ship, continuous=args.continuous, max_age_min=args.max_age))


if __name__ == "__main__":
    main()
