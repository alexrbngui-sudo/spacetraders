"""Self-sufficient mining drone swarm with optional surveyor.

Each drone independently: mine → fly to market → sell + refuel → fly back → repeat.
No shuttle needed. The optional surveyor rotates between drones creating surveys
to improve extraction yields.

Usage:
    python -m spacetraders.missions.drone_swarm
    python -m spacetraders.missions.drone_swarm --drones UTMOSTLY-5 UTMOSTLY-6
    python -m spacetraders.missions.drone_swarm --surveyor UTMOSTLY-D
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import signal
from dataclasses import dataclass, field
from datetime import datetime, timezone

from spacetraders.client import ApiError, SpaceTradersClient
from spacetraders.config import load_settings
from spacetraders.api import agent as agent_api, fleet, mining as mining_api, navigation
from spacetraders.data.asteroid_db import AsteroidDatabase
from spacetraders.data.market_db import MarketDatabase
from spacetraders.data.operations_db import OperationsDB
from spacetraders.fleet_registry import ship_name
from spacetraders.missions.mining import ensure_orbit, wait_for_cooldown
from spacetraders.missions.router import (
    best_flight_mode,
    distance,
    fuel_cost,
    usable_fuel,
)
from spacetraders.missions.runner import (
    navigate_ship,
    setup_logging,
    sleep_with_heartbeat,
    try_refuel,
)
from spacetraders.missions.scanner import deposit_score, is_minable_asteroid
from spacetraders.models import Ship, Survey, Waypoint

logger = logging.getLogger("spacetraders.swarm")


# --- Transit helpers ---


async def await_transit(client: SpaceTradersClient, ship_symbol: str) -> Ship:
    """Wait for a ship to finish transit, no matter how long it takes."""
    name = ship_name(ship_symbol)
    ship = await fleet.get_ship(client, ship_symbol)

    while ship.nav.status.value == "IN_TRANSIT":
        now = datetime.now(timezone.utc)
        arrival = ship.nav.route.arrival
        remaining = (arrival - now).total_seconds()

        if remaining > 0:
            wait = remaining + 2
            dest = ship.nav.route.destination.symbol
            logger.info(
                "[%s] In transit → %s, %.0fs remaining (%.1f min)",
                name, dest, remaining, remaining / 60,
            )
            await sleep_with_heartbeat(wait, f"{name} transit → {dest}")

        ship = await fleet.get_ship(client, ship_symbol)

        polls = 0
        while ship.nav.status.value == "IN_TRANSIT" and polls < 5:
            polls += 1
            logger.info("[%s] Still in transit, polling %d/5...", name, polls)
            await asyncio.sleep(10)
            ship = await fleet.get_ship(client, ship_symbol)

    return ship


async def swarm_navigate(
    client: SpaceTradersClient,
    ship: Ship,
    destination: str,
    mode: str | None = None,
) -> Ship:
    """Navigate and fully wait for arrival — safe for long drifts."""
    if ship.nav.waypoint_symbol == destination:
        return ship

    ship = await navigate_ship(client, ship, destination, mode)

    if ship.nav.status.value == "IN_TRANSIT":
        ship = await await_transit(client, ship.symbol)
        logger.info(
            "[%s] Arrived at %s. Fuel: %d/%d",
            ship_name(ship.symbol), destination,
            ship.fuel.current, ship.fuel.capacity,
        )

    return ship


# --- Constants ---

DRY_THRESHOLD = 20
DEFAULT_DRONES = ["UTMOSTLY-5", "UTMOSTLY-6", "UTMOSTLY-7", "UTMOSTLY-8"]

# Max one-way CRUISE fuel for asteroid eligibility.
# Drone has 80 fuel — need to reach market from asteroid.
# Drones refuel at market, so only one-way fuel matters.
MAX_DRONE_ONEWAY_FUEL = 70  # leaves 10 reserve


# --- Coordinator state ---


@dataclass
class SwarmState:
    """Shared coordination state for the drone swarm."""

    # ship_symbol → asteroid_symbol
    assignments: dict[str, str] = field(default_factory=dict)
    # asteroid_symbol → ship_symbol
    occupied: dict[str, str] = field(default_factory=dict)

    asteroid_db: AsteroidDatabase = field(repr=False, default=None)  # type: ignore[assignment]
    market_db: MarketDatabase = field(repr=False, default=None)  # type: ignore[assignment]
    ops_db: OperationsDB | None = field(repr=False, default=None)

    # Precomputed from waypoints
    coords: dict[str, tuple[int, int]] = field(default_factory=dict)
    waypoints: list[Waypoint] = field(default_factory=list)
    asteroids: list[Waypoint] = field(default_factory=list)
    markets: list[Waypoint] = field(default_factory=list)

    shutdown: asyncio.Event = field(default_factory=asyncio.Event)

    # Survey cache: asteroid_symbol → best survey (updated by surveyor loop)
    surveys: dict[str, Survey] = field(default_factory=dict)

    # Cumulative income tracking
    total_sold_units: int = 0
    total_credits_earned: int = 0

    def get_survey(self, asteroid_symbol: str) -> Survey | None:
        """Get cached survey for an asteroid, if not expired."""
        survey = self.surveys.get(asteroid_symbol)
        if survey is None:
            return None
        if survey.expiration <= datetime.now(timezone.utc):
            del self.surveys[asteroid_symbol]
            return None
        return survey

    def assign_asteroid(
        self, ship_symbol: str, current_x: int, current_y: int,
    ) -> Waypoint | None:
        """Assign the best unoccupied, non-blacklisted asteroid to a drone.

        Prefers asteroids close to a market (short sell trips).
        """
        candidates: list[tuple[float, Waypoint]] = []
        for wp in self.asteroids:
            if wp.symbol in self.occupied:
                continue
            if self.asteroid_db.is_blacklisted(wp.symbol, "ANY"):
                continue
            # Must be within one-way CRUISE range of at least one market
            nearest_market_dist = min(
                (distance(wp.x, wp.y, m.x, m.y) for m in self.markets),
                default=9999,
            )
            if fuel_cost(nearest_market_dist, "CRUISE") > MAX_DRONE_ONEWAY_FUEL:
                continue
            dist = distance(current_x, current_y, wp.x, wp.y)
            score = deposit_score(wp) - dist / 100.0
            # Bonus for being very close to a market
            if nearest_market_dist < 20:
                score += 3.0
            elif nearest_market_dist < 50:
                score += 2.0
            elif nearest_market_dist < 100:
                score += 1.0
            candidates.append((score, wp))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)
        best_wp = candidates[0][1]
        self.assignments[ship_symbol] = best_wp.symbol
        self.occupied[best_wp.symbol] = ship_symbol
        return best_wp

    def release_asteroid(self, ship_symbol: str) -> None:
        """Release a drone's asteroid assignment."""
        asteroid = self.assignments.pop(ship_symbol, None)
        if asteroid:
            self.occupied.pop(asteroid, None)


def _wp_distance(wp_a: str, wp_b: str, state: SwarmState) -> float:
    """Distance between two waypoint symbols."""
    ax, ay = state.coords.get(wp_a, (0, 0))
    bx, by = state.coords.get(wp_b, (0, 0))
    return distance(ax, ay, bx, by)


def _nearest_market(wp_symbol: str, state: SwarmState) -> Waypoint | None:
    """Find the nearest market to a waypoint."""
    x, y = state.coords.get(wp_symbol, (0, 0))
    best: Waypoint | None = None
    best_dist = float("inf")
    for m in state.markets:
        d = distance(x, y, m.x, m.y)
        if d < best_dist:
            best_dist = d
            best = m
    return best


def _best_sell_market(
    ship: Ship, state: SwarmState,
) -> Waypoint | None:
    """Pick the best reachable market based on revenue per minute (not just price).

    A nearby market at slightly lower prices beats a distant market at
    higher prices when you account for travel time.
    """
    name = ship_name(ship.symbol)
    hx, hy = state.coords.get(ship.nav.waypoint_symbol, (0, 0))
    available = usable_fuel(ship)
    speed = ship.engine.speed

    cargo_items: dict[str, int] = {}
    for item in ship.cargo.inventory:
        cargo_items[item.symbol] = cargo_items.get(item.symbol, 0) + item.units
    if not cargo_items:
        return None

    best_market: Waypoint | None = None
    best_score = -1.0  # revenue per minute

    for market_wp in state.markets:
        dist = distance(hx, hy, market_wp.x, market_wp.y)
        cruise_cost = fuel_cost(dist, "CRUISE")

        # Only consider CRUISEable markets — DRIFTing to sell is never worth it
        if cruise_cost > ship.fuel.current:
            continue

        prices = state.market_db.get_prices(market_wp.symbol)
        price_map = {p.trade_symbol: p.sell_price for p in prices}

        revenue = sum(price_map.get(sym, 0) * units for sym, units in cargo_items.items())
        if revenue <= 0:
            continue

        # Estimate round-trip time in minutes (to market and back)
        travel_secs = round(15 + dist * 25 / max(speed, 1)) * 2  # CRUISE both ways
        sell_secs = 30  # dock + sell + refuel overhead
        total_mins = max((travel_secs + sell_secs) / 60, 0.5)

        score = revenue / total_mins
        if score > best_score:
            best_score = score
            best_market = market_wp

    if best_market:
        dist = distance(hx, hy, best_market.x, best_market.y)
        logger.info(
            "[%s] Best market: %s (%.0f dist, %.0f cr/min)",
            name, best_market.symbol, dist, best_score,
        )
    else:
        # No price data — nearest CRUISEable market
        reachable = []
        for m in state.markets:
            dist = distance(hx, hy, m.x, m.y)
            if fuel_cost(dist, "CRUISE") <= ship.fuel.current:
                reachable.append((dist, m))
        if reachable:
            reachable.sort(key=lambda x: x[0])
            best_market = reachable[0][1]
            logger.info("[%s] No price data — nearest market %s", name, best_market.symbol)

    return best_market


# --- Self-sufficient drone loop ---


async def drone_mine_loop(
    client: SpaceTradersClient,
    ship_symbol: str,
    state: SwarmState,
) -> None:
    """Self-sufficient mining loop: mine → sell at market → refuel → return → repeat."""
    name = ship_name(ship_symbol)
    logger.info("[%s] Starting mining loop", name)

    ship = await fleet.get_ship(client, ship_symbol)
    if ship.nav.status.value == "IN_TRANSIT":
        ship = await await_transit(client, ship_symbol)
    system = ship.nav.system_symbol

    # Get or assign asteroid
    asteroid_wp = None
    current_assignment = state.assignments.get(ship_symbol)
    if current_assignment:
        for wp in state.asteroids:
            if wp.symbol == current_assignment:
                asteroid_wp = wp
                break

    if not asteroid_wp:
        sx, sy = state.coords.get(ship.nav.waypoint_symbol, (0, 0))
        asteroid_wp = state.assign_asteroid(ship_symbol, sx, sy)
        if not asteroid_wp:
            logger.error("[%s] No available asteroids! Exiting.", name)
            return

    # Deploy to asteroid
    if ship.nav.waypoint_symbol != asteroid_wp.symbol:
        dist = distance(
            *state.coords.get(ship.nav.waypoint_symbol, (0, 0)),
            asteroid_wp.x, asteroid_wp.y,
        )
        mode = best_flight_mode(ship, dist)
        logger.info(
            "[%s] Deploying to %s (%.0f dist, %s)", name, asteroid_wp.symbol, dist, mode,
        )
        ship = await swarm_navigate(client, ship, asteroid_wp.symbol, mode)

    logger.info("[%s] Mining at %s", name, asteroid_wp.symbol)
    consecutive_misses = 0
    total_extractions = 0

    while not state.shutdown.is_set():
        ship = await fleet.get_ship(client, ship_symbol)
        if ship.nav.status.value == "IN_TRANSIT":
            ship = await await_transit(client, ship_symbol)

        ship = await ensure_orbit(client, ship)
        await wait_for_cooldown(client, ship_symbol)

        # Cargo full → sell trip
        ship = await fleet.get_ship(client, ship_symbol)
        if ship.cargo.units >= ship.cargo.capacity:
            logger.info(
                "[%s] Cargo full (%d/%d), heading to market",
                name, ship.cargo.units, ship.cargo.capacity,
            )
            await _drone_sell_trip(client, ship, system, asteroid_wp, state)
            ship = await fleet.get_ship(client, ship_symbol)
            continue

        # Extract — use cached survey if available
        survey = state.get_survey(asteroid_wp.symbol)
        try:
            extraction, cooldown = await mining_api.extract(client, ship_symbol, survey)
        except ApiError as e:
            if e.code == 4000:  # Cooldown active
                remaining = e.data.get("cooldown", {}).get("remainingSeconds", 30)
                logger.info("[%s] Cooldown: %ds", name, remaining)
                await asyncio.sleep(remaining + 1)
                continue
            if e.code in (4221, 4224):  # Survey expired/invalid
                state.surveys.pop(asteroid_wp.symbol, None)
                logger.info("[%s] Survey expired, extracting without", name)
                continue
            logger.error("[%s] Extract error (%d): %s", name, e.code, e)
            await asyncio.sleep(10)
            continue

        total_extractions += 1
        logger.info(
            "[%s] Extracted %dx %s (cargo: %d/%d)",
            name, extraction.yield_.units, extraction.yield_.symbol,
            ship.cargo.units + extraction.yield_.units, ship.cargo.capacity,
        )

        state.asteroid_db.record_extraction(asteroid_wp.symbol, "ANY", True)
        if state.ops_db and extraction.yield_.units > 0:
            state.ops_db.record_extraction(
                ship_symbol, asteroid_wp.symbol,
                extraction.yield_.symbol, extraction.yield_.units,
            )

        if extraction.yield_.units == 0:
            consecutive_misses += 1
        else:
            consecutive_misses = 0

        if consecutive_misses >= DRY_THRESHOLD:
            logger.warning(
                "[%s] Asteroid %s appears dry (%d misses). Reassigning.",
                name, asteroid_wp.symbol, consecutive_misses,
            )
            state.asteroid_db.blacklist(
                asteroid_wp.symbol, "ANY",
                f"Dry after {total_extractions} extractions",
            )
            state.release_asteroid(ship_symbol)

            ship = await fleet.get_ship(client, ship_symbol)
            sx, sy = state.coords.get(ship.nav.waypoint_symbol, (0, 0))
            asteroid_wp = state.assign_asteroid(ship_symbol, sx, sy)
            if not asteroid_wp:
                logger.error("[%s] No more asteroids available!", name)
                return

            dist = distance(sx, sy, asteroid_wp.x, asteroid_wp.y)
            mode = best_flight_mode(ship, dist)
            logger.info("[%s] Reassigned to %s (%.0f dist)", name, asteroid_wp.symbol, dist)
            ship = await swarm_navigate(client, ship, asteroid_wp.symbol, mode)
            consecutive_misses = 0
            total_extractions = 0

        await asyncio.sleep(cooldown.remaining_seconds + 1)

    logger.info("[%s] Shutdown — exiting mine loop", name)


async def _drone_sell_trip(
    client: SpaceTradersClient,
    ship: Ship,
    system: str,
    home_asteroid: Waypoint,
    state: SwarmState,
) -> None:
    """Fly to best market, sell all cargo, refuel, return to asteroid."""
    name = ship_name(ship.symbol)

    # Pick market
    market_wp = _best_sell_market(ship, state)
    if not market_wp:
        market_wp = _nearest_market(home_asteroid.symbol, state)
    if not market_wp:
        logger.warning("[%s] No reachable market! Keeping cargo.", name)
        return

    # Navigate to market (always CRUISE — drifting to sell is never worth it)
    if ship.nav.waypoint_symbol != market_wp.symbol:
        dist = _wp_distance(ship.nav.waypoint_symbol, market_wp.symbol, state)
        logger.info(
            "[%s] → %s to sell (%.0f dist, CRUISE, fuel %d/%d)",
            name, market_wp.symbol, dist,
            ship.fuel.current, ship.fuel.capacity,
        )
        ship = await swarm_navigate(client, ship, market_wp.symbol, "CRUISE")

    # Dock and sell everything
    if ship.nav.status.value != "DOCKED":
        await fleet.dock(client, ship.symbol)

    cargo = await fleet.get_cargo(client, ship.symbol)
    total_sold = 0
    total_credits = 0

    for item in cargo.inventory:
        try:
            result = await fleet.sell_cargo(client, ship.symbol, item.symbol, item.units)
            price = result.get("transaction", {}).get("totalPrice", 0)
            total_sold += item.units
            total_credits += price
            balance = result.get("agent", {}).get("credits", "?")
            logger.info(
                "[%s] Sold %dx %s for %d cr (balance: %s)",
                name, item.units, item.symbol, price,
                f"{balance:,}" if isinstance(balance, int) else balance,
            )
            if state.ops_db:
                ppu = result.get("transaction", {}).get("pricePerUnit", 0)
                state.ops_db.record_trade(
                    ship.symbol, "SELL", item.symbol, item.units, ppu, price,
                    market_wp.symbol,
                    balance if isinstance(balance, int) else None,
                    "mining",
                )
        except ApiError as e:
            logger.warning("[%s] Can't sell %s (%d): %s", name, item.symbol, e.code, e)

    if total_sold > 0:
        state.total_sold_units += total_sold
        state.total_credits_earned += total_credits
        logger.info(
            "[%s] Sold %d units for %d cr | Session total: %d cr",
            name, total_sold, total_credits, state.total_credits_earned,
        )

    # Cache market data
    try:
        market = await navigation.get_market(client, system, market_wp.symbol)
        if market.trade_goods:
            state.market_db.update_market(market_wp.symbol, market.trade_goods)
    except ApiError:
        pass

    # Refuel
    ship = await fleet.get_ship(client, ship.symbol)
    if ship.fuel.current < ship.fuel.capacity:
        ship = await try_refuel(client, ship)
        logger.info("[%s] Refueled to %d/%d", name, ship.fuel.current, ship.fuel.capacity)

    # Return to asteroid
    await fleet.orbit(client, ship.symbol)
    dist = _wp_distance(market_wp.symbol, home_asteroid.symbol, state)
    mode = best_flight_mode(ship, dist)
    logger.info(
        "[%s] → %s to mine (%.0f dist, %s)",
        name, home_asteroid.symbol, dist, mode,
    )
    ship = await swarm_navigate(client, ship, home_asteroid.symbol, mode)


# --- Surveyor loop ---


async def surveyor_loop(
    client: SpaceTradersClient,
    surveyor_symbol: str,
    drone_symbols: list[str],
    state: SwarmState,
) -> None:
    """Surveyor rotates between drone asteroids, creating surveys to boost yields."""
    name = ship_name(surveyor_symbol)
    logger.info("[%s] Starting surveyor loop", name)

    ship = await fleet.get_ship(client, surveyor_symbol)
    if ship.nav.status.value == "IN_TRANSIT":
        ship = await await_transit(client, surveyor_symbol)

    while not state.shutdown.is_set():
        # Refuel if low
        ship = await fleet.get_ship(client, surveyor_symbol)
        if ship.fuel.current < 20 and ship.fuel.capacity > 0:
            nearest = _nearest_market(ship.nav.waypoint_symbol, state)
            if nearest:
                dist = _wp_distance(ship.nav.waypoint_symbol, nearest.symbol, state)
                mode = "DRIFT" if fuel_cost(dist, "CRUISE") > usable_fuel(ship) else "CRUISE"
                logger.info(
                    "[%s] Low fuel (%d/%d), refueling at %s",
                    name, ship.fuel.current, ship.fuel.capacity, nearest.symbol,
                )
                ship = await swarm_navigate(client, ship, nearest.symbol, mode)
                ship = await try_refuel(client, ship)

        # Visit each drone's asteroid and create surveys
        for drone_sym in drone_symbols:
            if state.shutdown.is_set():
                break

            asteroid_sym = state.assignments.get(drone_sym)
            if not asteroid_sym:
                continue

            # Skip if fresh survey exists
            if state.get_survey(asteroid_sym) is not None:
                continue

            ship = await fleet.get_ship(client, surveyor_symbol)

            if ship.nav.waypoint_symbol != asteroid_sym:
                dist = _wp_distance(ship.nav.waypoint_symbol, asteroid_sym, state)
                cruise_cost = fuel_cost(dist, "CRUISE")
                mode = "DRIFT" if cruise_cost > usable_fuel(ship) else best_flight_mode(ship, dist)
                logger.info(
                    "[%s] → %s for %s (%.0f dist, %s, fuel %d/%d)",
                    name, asteroid_sym, ship_name(drone_sym), dist, mode,
                    ship.fuel.current, ship.fuel.capacity,
                )
                ship = await swarm_navigate(client, ship, asteroid_sym, mode)

            ship = await ensure_orbit(client, ship)
            await wait_for_cooldown(client, surveyor_symbol)

            try:
                surveys, cooldown = await mining_api.create_survey(client, surveyor_symbol)
                if surveys:
                    best = max(surveys, key=lambda s: len(s.deposits))
                    state.surveys[asteroid_sym] = best
                    deposit_names = [d.symbol for d in best.deposits]
                    logger.info(
                        "[%s] Survey at %s: %s (expires %s)",
                        name, asteroid_sym,
                        ", ".join(deposit_names),
                        best.expiration.strftime("%H:%M:%S"),
                    )
                await asyncio.sleep(cooldown.remaining_seconds + 1)
            except ApiError as e:
                if e.code == 4000:
                    remaining = e.data.get("cooldown", {}).get("remainingSeconds", 30)
                    await asyncio.sleep(remaining + 1)
                else:
                    logger.error("[%s] Survey error (%d): %s", name, e.code, e)
                    await asyncio.sleep(10)

        # Wait before next round
        try:
            await asyncio.wait_for(state.shutdown.wait(), timeout=60)
        except TimeoutError:
            pass

    logger.info("[%s] Shutdown — exiting surveyor loop", name)


# --- Main orchestrator ---


async def run_swarm(
    drone_symbols: list[str],
    surveyor_symbol: str | None = None,
) -> None:
    """Run the self-sufficient mining drone swarm."""
    settings = load_settings()
    asteroid_db = AsteroidDatabase(db_path=settings.data_dir / "asteroids.db")
    market_db = MarketDatabase(db_path=settings.data_dir / "markets.db")
    ops_db = OperationsDB(db_path=settings.data_dir / "operations.db")

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    try:
        async with SpaceTradersClient(settings) as client:
            ag = await agent_api.get_agent(client)
            logger.info("=" * 60)
            logger.info("DRONE SWARM STARTING")
            logger.info("Agent: %s | Credits: %s", ag.symbol, f"{ag.credits:,}")
            logger.info("Drones: %s", ", ".join(ship_name(d) for d in drone_symbols))
            logger.info("Mode: self-sufficient (each drone sells at market)")
            if surveyor_symbol:
                logger.info("Surveyor: %s (%s)", ship_name(surveyor_symbol), surveyor_symbol)
            logger.info("=" * 60)

            sample_ship = await fleet.get_ship(client, drone_symbols[0])
            system = sample_ship.nav.system_symbol
            waypoints = await navigation.list_waypoints(client, system)

            state = SwarmState(
                asteroid_db=asteroid_db,
                market_db=market_db,
                ops_db=ops_db,
                shutdown=shutdown,
                waypoints=waypoints,
                coords={wp.symbol: (wp.x, wp.y) for wp in waypoints},
                asteroids=[wp for wp in waypoints if is_minable_asteroid(wp)],
                markets=[
                    wp for wp in waypoints
                    if any(t.symbol == "MARKETPLACE" for t in wp.traits)
                ],
            )

            logger.info(
                "System %s: %d waypoints, %d minable asteroids, %d markets",
                system, len(waypoints), len(state.asteroids), len(state.markets),
            )

            # Assign asteroids
            for drone_sym in drone_symbols:
                ship = await fleet.get_ship(client, drone_sym)
                sx, sy = state.coords.get(ship.nav.waypoint_symbol, (0, 0))
                wp = state.assign_asteroid(drone_sym, sx, sy)
                if wp:
                    market = _nearest_market(wp.symbol, state)
                    market_dist = _wp_distance(wp.symbol, market.symbol, state) if market else 9999
                    logger.info(
                        "Assigned %s → %s (deposits: %.1f, market %.0f dist)",
                        ship_name(drone_sym), wp.symbol, deposit_score(wp), market_dist,
                    )
                else:
                    logger.warning("No asteroid available for %s", ship_name(drone_sym))

            # Launch tasks
            tasks: list[asyncio.Task] = []

            for drone_sym in drone_symbols:
                task = asyncio.create_task(
                    drone_mine_loop(client, drone_sym, state),
                    name=f"drone-{ship_name(drone_sym)}",
                )
                tasks.append(task)

            if surveyor_symbol:
                task = asyncio.create_task(
                    surveyor_loop(client, surveyor_symbol, drone_symbols, state),
                    name=f"surveyor-{ship_name(surveyor_symbol)}",
                )
                tasks.append(task)

            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

            for t in done:
                if t.exception():
                    logger.error("Task %s crashed: %s", t.get_name(), t.exception())

            if pending:
                shutdown.set()
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

            # Final status
            ag = await agent_api.get_agent(client)
            logger.info("")
            logger.info("=" * 60)
            logger.info("DRONE SWARM STOPPED")
            logger.info("Credits: %s", f"{ag.credits:,}")
            logger.info(
                "Session: sold %d units for %s credits",
                state.total_sold_units, f"{state.total_credits_earned:,}",
            )
            for drone_sym in drone_symbols:
                try:
                    s = await fleet.get_ship(client, drone_sym)
                    logger.info(
                        "  %s: fuel %d/%d, cargo %d/%d at %s",
                        ship_name(drone_sym),
                        s.fuel.current, s.fuel.capacity,
                        s.cargo.units, s.cargo.capacity,
                        s.nav.waypoint_symbol,
                    )
                except ApiError:
                    logger.info("  %s: unreachable", ship_name(drone_sym))
            logger.info("=" * 60)

    except KeyboardInterrupt:
        logger.info("Swarm interrupted by user.")
    except Exception:
        logger.exception("SWARM CRASHED — unexpected error")
    finally:
        asteroid_db.close()
        market_db.close()
        ops_db.close()


# --- CLI ---


def main() -> None:
    parser = argparse.ArgumentParser(description="SpaceTraders drone mining swarm")
    parser.add_argument(
        "--drones", nargs="+", default=DEFAULT_DRONES,
        help="Drone ship symbols (default: UTMOSTLY-5 through 8)",
    )
    parser.add_argument(
        "--surveyor", default=None,
        help="Surveyor ship symbol — rotates between drones creating surveys",
    )
    args = parser.parse_args()

    settings = load_settings()
    setup_logging("swarm", log_dir=settings.data_dir / "logs")
    asyncio.run(run_swarm(
        drone_symbols=args.drones,
        surveyor_symbol=args.surveyor,
    ))


if __name__ == "__main__":
    main()
