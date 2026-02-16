"""Mission runner — full automated mine→deliver→refuel→repeat loop.

Usage:
    python -m spacetraders.missions.runner [--ship UTMOSTLY-1] [--resource COPPER_ORE]

Runs from the project root (where .env lives). Logs to stdout and file.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from spacetraders.client import ApiError, SpaceTradersClient
from spacetraders.config import load_settings
from spacetraders.api import agent as agent_api, contracts as contracts_api, fleet, navigation
from spacetraders.missions.mining import mine_asteroid
from spacetraders.missions.router import (
    RoutePlan,
    best_flight_mode,
    distance,
    fuel_cost,
    plan_round_trip,
    travel_time,
    usable_fuel,
    waypoint_distance,
)
from spacetraders.data.asteroid_db import AsteroidDatabase
from spacetraders.data.market_db import MarketDatabase
from spacetraders.missions.scanner import rank_asteroids
from spacetraders.fleet_registry import ship_name
from spacetraders.models import Contract, Ship, Waypoint

logger = logging.getLogger("spacetraders.missions")

# Sanity cap for in-transit waits — reject clearly bogus arrival times
MAX_TRANSIT_WAIT = 3600  # 1 hour; real trips max ~17 min
# Max poll attempts after initial sleep (10s each = 2 min)
TRANSIT_POLL_ATTEMPTS = 12
# Fuel percentage below which we consider the ship critically low
FUEL_CRITICAL_PCT = 0.15
# Seconds to wait for operator intervention when fuel is critical
FUEL_CRITICAL_PAUSE = 300


EST = ZoneInfo("America/New_York")


async def sleep_with_heartbeat(
    seconds: float, context: str, *, interval: float = 60,
) -> None:
    """Sleep for `seconds`, logging a heartbeat every `interval`."""
    elapsed = 0.0
    while elapsed < seconds:
        chunk = min(interval, seconds - elapsed)
        await asyncio.sleep(chunk)
        elapsed += chunk
        if elapsed < seconds:
            remaining = seconds - elapsed
            eta = datetime.now(EST) + timedelta(seconds=remaining)
            eta_str = eta.strftime("%-I:%M %p EST")
            logger.info(
                "  [heartbeat] %s — %.0f/%.0fs (ETA: %s)",
                context, elapsed, seconds, eta_str,
            )


def setup_logging(ship_symbol: str, log_dir: Path | None = None) -> None:
    """Configure logging to both stdout and a ship-specific log file."""
    if log_dir is None:
        log_dir = Path("data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"mission_{ship_symbol.lower()}.log"

    formatter = logging.Formatter(
        "[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
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

    # Silence noisy HTTP request logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    logger.info("Logging to %s", log_file)


async def wait_for_arrival(client: SpaceTradersClient, ship_symbol: str) -> Ship:
    """If ship is in transit, wait for arrival with safety clamp and polling."""
    ship = await fleet.get_ship(client, ship_symbol)
    if ship.nav.status.value == "IN_TRANSIT":
        now = datetime.now(timezone.utc)
        arrival = ship.nav.route.arrival
        raw_wait = (arrival - now).total_seconds()
        wait_secs = min(max(raw_wait + 2, 0), MAX_TRANSIT_WAIT)
        if raw_wait + 2 > MAX_TRANSIT_WAIT:
            logger.warning(
                "Transit wait %.0fs exceeds max, clamping to %ds",
                raw_wait, MAX_TRANSIT_WAIT,
            )
        if wait_secs > 0:
            logger.info(
                "In transit %s → %s, waiting %.0fs (%.1f min)",
                ship.nav.route.origin.symbol,
                ship.nav.route.destination.symbol,
                wait_secs,
                wait_secs / 60,
            )
            await sleep_with_heartbeat(wait_secs, f"transit → {ship.nav.route.destination.symbol}")
        ship = await fleet.get_ship(client, ship_symbol)

        # Poll if still in transit after initial wait
        polls = 0
        while ship.nav.status.value == "IN_TRANSIT" and polls < TRANSIT_POLL_ATTEMPTS:
            polls += 1
            logger.info("Still in transit, polling %d/%d...", polls, TRANSIT_POLL_ATTEMPTS)
            await asyncio.sleep(10)
            ship = await fleet.get_ship(client, ship_symbol)

        if ship.nav.status.value == "IN_TRANSIT":
            logger.error("Ship still IN_TRANSIT after max wait + polling. Returning as-is.")
    return ship


async def navigate_ship(
    client: SpaceTradersClient,
    ship: Ship,
    destination: str,
    mode: str | None = None,
) -> Ship:
    """Navigate ship to destination, handling flight mode and transit wait."""
    if ship.nav.waypoint_symbol == destination:
        logger.info("Already at %s", destination)
        return ship

    # Ensure orbit
    if ship.nav.status.value == "DOCKED":
        await fleet.orbit(client, ship.symbol)
        ship = await fleet.get_ship(client, ship.symbol)

    # Set flight mode if specified
    if mode and ship.nav.flight_mode.value != mode:
        await fleet.set_flight_mode(client, ship.symbol, mode)
        logger.info("Flight mode set to %s", mode)

    # Navigate
    nav_data = await fleet.navigate(client, ship.symbol, destination)
    ship = await fleet.get_ship(client, ship.symbol)

    fuel_used = nav_data.get("fuel", {}).get("consumed", {}).get("amount", "?")
    logger.info(
        "Navigating %s → %s (%s mode, %s fuel used)",
        ship.nav.route.origin.symbol,
        destination,
        ship.nav.flight_mode.value,
        fuel_used,
    )

    # Wait for arrival
    ship = await wait_for_arrival(client, ship.symbol)
    logger.info("Arrived at %s. Fuel: %d/%d", destination, ship.fuel.current, ship.fuel.capacity)
    return ship


async def navigate_multihop(
    client: SpaceTradersClient,
    ship: Ship,
    plan: RoutePlan,
) -> Ship:
    """Navigate through a multi-hop route, refueling at intermediate stops."""
    if not plan.feasible or not plan.segments:
        return ship

    for i, segment in enumerate(plan.segments):
        is_last = i == len(plan.segments) - 1
        ship = await navigate_ship(client, ship, segment.destination, segment.flight_mode)
        # Long legs may exceed MAX_TRANSIT_WAIT — keep waiting if needed
        while ship.nav.status.value == "IN_TRANSIT":
            ship = await wait_for_arrival(client, ship.symbol)
        if not is_last:
            logger.info(
                "Refueling at stop %d/%d: %s",
                i + 1, plan.num_stops, segment.destination,
            )
            if ship.nav.status.value != "DOCKED":
                await fleet.dock(client, ship.symbol)
            ship = await try_refuel(client, ship)
    return ship


async def dock_and_deliver(
    client: SpaceTradersClient,
    ship: Ship,
    contract: Contract,
    target_resource: str,
) -> tuple[Ship, int]:
    """Dock, deliver contract goods, return updated ship and units delivered."""
    # Dock
    if ship.nav.status.value != "DOCKED":
        await fleet.dock(client, ship.symbol)
        logger.info("Docked at %s", ship.nav.waypoint_symbol)

    # How much target do we have?
    units = sum(i.units for i in ship.cargo.inventory if i.symbol == target_resource)
    if units == 0:
        logger.info("No %s to deliver", target_resource)
        return await fleet.get_ship(client, ship.symbol), 0

    # Find the delivery requirement
    delivery = None
    for d in contract.terms.deliver:
        if d.trade_symbol == target_resource:
            delivery = d
            break

    if not delivery:
        logger.warning("Contract doesn't need %s", target_resource)
        return await fleet.get_ship(client, ship.symbol), 0

    # Only deliver what's still needed
    remaining = delivery.units_required - delivery.units_fulfilled
    to_deliver = min(units, remaining)

    if to_deliver <= 0:
        logger.info("Contract already fulfilled for %s", target_resource)
        return await fleet.get_ship(client, ship.symbol), 0

    result = await contracts_api.deliver_contract(
        client, contract.id, ship.symbol, target_resource, to_deliver,
    )
    logger.info(
        "Delivered %d/%d %s (contract: %d/%d total)",
        to_deliver, units, target_resource,
        delivery.units_fulfilled + to_deliver, delivery.units_required,
    )

    ship = await fleet.get_ship(client, ship.symbol)
    return ship, to_deliver


async def try_refuel(client: SpaceTradersClient, ship: Ship) -> Ship:
    """Try to refuel at current location. Log cost if successful."""
    if ship.fuel.current >= ship.fuel.capacity:
        logger.info("Fuel already full: %d/%d", ship.fuel.current, ship.fuel.capacity)
        return ship

    if ship.nav.status.value != "DOCKED":
        await fleet.dock(client, ship.symbol)

    fuel_before = ship.fuel.current
    try:
        agent_before = await agent_api.get_agent(client)
        credits_before = agent_before.credits

        refuel_data = await fleet.refuel(client, ship.symbol)
        ship = await fleet.get_ship(client, ship.symbol)

        agent_after = await agent_api.get_agent(client)
        cost = credits_before - agent_after.credits

        if ship.fuel.current <= fuel_before:
            logger.warning(
                "Refuel may have failed silently: fuel unchanged at %d/%d",
                ship.fuel.current, ship.fuel.capacity,
            )
        else:
            logger.info(
                "Refueled: %d → %d/%d fuel. Cost: %d credits. Balance: %d",
                fuel_before, ship.fuel.current, ship.fuel.capacity,
                cost, agent_after.credits,
            )
        return ship
    except ApiError as e:
        logger.warning(
            "Refuel failed (%d): %s. Fuel: %d/%d",
            e.code, e, fuel_before, ship.fuel.capacity,
        )
        return await fleet.get_ship(client, ship.symbol)


async def try_fulfill_contract(
    client: SpaceTradersClient,
    contract: Contract,
) -> Contract:
    """Check if contract is fully delivered and fulfill it."""
    # Refresh contract
    contract = await contracts_api.get_contract(client, contract.id)

    all_delivered = all(
        d.units_fulfilled >= d.units_required
        for d in contract.terms.deliver
    )

    if all_delivered and not contract.fulfilled:
        try:
            contract = await contracts_api.fulfill_contract(client, contract.id)
            logger.info(
                "CONTRACT FULFILLED! Payment: %d credits",
                contract.terms.payment.on_fulfilled,
            )
        except ApiError as e:
            logger.error("Fulfill failed (%d): %s", e.code, e)

    return contract


async def try_negotiate_next(
    client: SpaceTradersClient,
    ship_symbol: str,
    target_resource: str,
    wp_lookup: dict[str, Waypoint],
) -> Contract | None:
    """Try to negotiate and accept a new contract for the same resource.

    Ship must be at a faction waypoint (HQ, outpost, etc.) to negotiate.
    Navigates to the nearest faction waypoint if needed.
    """
    # Find faction waypoints we can negotiate at
    faction_wps = [
        wp for wp in wp_lookup.values()
        if wp.faction is not None
        and wp.type.value in {"PLANET", "ORBITAL_STATION"}
    ]

    if not faction_wps:
        logger.info("No faction waypoints found for negotiation")
        return None

    ship = await fleet.get_ship(client, ship_symbol)

    # Check if we're already at a faction waypoint
    current_wp = wp_lookup.get(ship.nav.waypoint_symbol)
    at_faction = current_wp and current_wp in faction_wps

    if not at_faction:
        # Navigate to nearest faction waypoint
        if not current_wp:
            logger.warning("Can't determine position for contract negotiation")
            return None

        nearest = min(
            faction_wps,
            key=lambda wp: waypoint_distance(current_wp, wp),
        )
        dist = waypoint_distance(current_wp, nearest)
        mode = best_flight_mode(ship, dist)
        logger.info(
            "Navigating to %s for contract negotiation (%.0f dist, %s)",
            nearest.symbol, dist, mode,
        )
        ship = await navigate_ship(client, ship, nearest.symbol, mode)

    # Dock (required for negotiation)
    if ship.nav.status.value != "DOCKED":
        await fleet.dock(client, ship.symbol)

    # Negotiate
    try:
        new_contract = await contracts_api.negotiate_contract(client, ship_symbol)
        logger.info(
            "NEGOTIATED new contract: %s (%s)",
            new_contract.id, new_contract.type.value,
        )

        # Check if it involves our target resource
        has_target = any(
            d.trade_symbol == target_resource
            for d in new_contract.terms.deliver
        )

        if not has_target:
            logger.info(
                "New contract doesn't need %s — leaving unaccepted for manual review",
                target_resource,
            )
            for d in new_contract.terms.deliver:
                logger.info("  Needs: %d %s → %s", d.units_required, d.trade_symbol, d.destination_symbol)
            return None

        # Accept it
        new_contract = await contracts_api.accept_contract(client, new_contract.id)
        logger.info(
            "ACCEPTED contract %s: deliver %s. Advance payment: %d credits",
            new_contract.id, target_resource,
            new_contract.terms.payment.on_accepted,
        )
        return new_contract

    except ApiError as e:
        logger.warning("Contract negotiation failed (%d): %s", e.code, e)
        return None


def find_delivery_waypoint(contract: Contract, resource: str) -> str | None:
    """Find the delivery waypoint for a resource in a contract."""
    for d in contract.terms.deliver:
        if d.trade_symbol == resource:
            return d.destination_symbol
    return None


def find_refuel_waypoint(
    waypoints: list[Waypoint],
    near_x: int,
    near_y: int,
) -> Waypoint | None:
    """Find nearest waypoint with marketplace (likely has fuel).

    Fuel is available at any marketplace. Prioritize known fuel stations,
    then any marketplace.
    """
    market_wps = []
    for wp in waypoints:
        trait_symbols = {t.symbol for t in wp.traits}
        if "MARKETPLACE" in trait_symbols:
            dist = distance(near_x, near_y, wp.x, wp.y)
            # Prioritize fuel stations
            is_fuel = wp.type.value == "FUEL_STATION"
            market_wps.append((is_fuel, dist, wp))

    if not market_wps:
        return None

    # Sort: fuel stations first, then by distance
    market_wps.sort(key=lambda x: (not x[0], x[1]))
    return market_wps[0][2]


async def run_mission(
    ship_symbol: str,
    target_resource: str,
    max_trips: int = 0,
    timeout_minutes: int = 0,
) -> None:
    """Run the full mining mission loop.

    Args:
        ship_symbol: Ship to use (e.g. "UTMOSTLY-1")
        target_resource: Resource to mine (e.g. "COPPER_ORE")
        max_trips: Max mine→deliver trips. 0 = until contract done.
        timeout_minutes: Wall-clock limit in minutes. 0 = no limit.
    """
    mission_start = time.monotonic()
    settings = load_settings()
    asteroid_db = AsteroidDatabase(db_path=settings.data_dir / "asteroids.db")
    market_db = MarketDatabase(db_path=settings.data_dir / "markets.db")

    # Graceful shutdown event
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    try:
        async with SpaceTradersClient(settings) as client:
            # Get initial state
            ag = await agent_api.get_agent(client)
            name = ship_name(ship_symbol)
            logger.info("=" * 60)
            logger.info("MISSION START [%s]: Mine & deliver %s", name, target_resource)
            logger.info("Ship: %s (%s) | Credits: %d", name, ship_symbol, ag.credits)
            if timeout_minutes > 0:
                logger.info("Timeout: %d minutes", timeout_minutes)
            logger.info("=" * 60)

            # Get contract
            all_contracts = await contracts_api.list_contracts(client)
            contract = None
            for c in all_contracts:
                if c.accepted and not c.fulfilled:
                    for d in c.terms.deliver:
                        if d.trade_symbol == target_resource:
                            contract = c
                            break

            if not contract:
                logger.error("No active contract found for %s", target_resource)
                return

            delivery_wp_symbol = find_delivery_waypoint(contract, target_resource)
            if not delivery_wp_symbol:
                logger.error("No delivery waypoint for %s", target_resource)
                return

            # Contract status
            for d in contract.terms.deliver:
                if d.trade_symbol == target_resource:
                    remaining = d.units_required - d.units_fulfilled
                    logger.info(
                        "Contract: %d/%d %s delivered. Need %d more.",
                        d.units_fulfilled, d.units_required, target_resource, remaining,
                    )

            # Get ship and system data
            ship = await fleet.get_ship(client, ship_symbol)
            system = ship.nav.system_symbol
            waypoints = await navigation.list_waypoints(client, system)

            # Build waypoint lookup
            wp_lookup: dict[str, Waypoint] = {wp.symbol: wp for wp in waypoints}

            # Find delivery and refuel waypoints
            delivery_wp = wp_lookup.get(delivery_wp_symbol)
            if not delivery_wp:
                logger.error("Delivery waypoint %s not found in system", delivery_wp_symbol)
                return

            # Find nearest refuel point to delivery
            refuel_wp = find_refuel_waypoint(waypoints, delivery_wp.x, delivery_wp.y)
            if refuel_wp:
                logger.info("Refuel point: %s (%.0f from delivery)", refuel_wp.symbol,
                           waypoint_distance(delivery_wp, refuel_wp))
            else:
                logger.warning("No refuel point found! Operating without refuel capability.")

            # Wait for arrival if in transit
            ship = await wait_for_arrival(client, ship_symbol)

            trip = 0
            total_junk_revenue = 0
            while True:
                # Check shutdown signal
                if shutdown.is_set():
                    logger.info("Shutdown signal received. Stopping gracefully.")
                    break

                # Check wall-clock timeout
                if timeout_minutes > 0:
                    elapsed_min = (time.monotonic() - mission_start) / 60
                    if elapsed_min >= timeout_minutes:
                        logger.info("Timeout reached (%.0f/%d min). Stopping.", elapsed_min, timeout_minutes)
                        break

                trip += 1
                if max_trips > 0 and trip > max_trips:
                    logger.info("Max trips (%d) reached. Stopping.", max_trips)
                    break

                # Refresh contract
                contract = await contracts_api.get_contract(client, contract.id)
                for d in contract.terms.deliver:
                    if d.trade_symbol == target_resource:
                        remaining = d.units_required - d.units_fulfilled
                        if remaining <= 0:
                            logger.info("Contract fully delivered!")
                            break
                else:
                    remaining = 0

                if remaining <= 0 or contract.fulfilled:
                    contract = await try_fulfill_contract(client, contract)
                    logger.info("Mission complete! Contract fulfilled.")

                    # Try to negotiate next contract
                    next_contract = await try_negotiate_next(
                        client, ship_symbol, target_resource, wp_lookup,
                    )
                    if next_contract:
                        contract = next_contract
                        delivery_wp_symbol_new = find_delivery_waypoint(contract, target_resource)
                        if delivery_wp_symbol_new:
                            delivery_wp_symbol = delivery_wp_symbol_new
                            delivery_wp = wp_lookup.get(delivery_wp_symbol)
                            logger.info("Continuing with new contract!")
                            continue
                    break

                ship = await fleet.get_ship(client, ship_symbol)
                logger.info("")
                logger.info(
                    "### TRIP %d — need %d more %s | Fuel: %d/%d | Cargo: %d/%d ###",
                    trip, remaining, target_resource,
                    ship.fuel.current, ship.fuel.capacity,
                    ship.cargo.units, ship.cargo.capacity,
                )

                # Phase 1: Pick asteroid
                if shutdown.is_set():
                    break
                ship_wp = wp_lookup.get(ship.nav.waypoint_symbol)

                # If we're already at an asteroid, check if it's viable
                current_is_asteroid = ship_wp and ship_wp.type.value in {
                    "ASTEROID", "ASTEROID_FIELD", "ENGINEERED_ASTEROID", "ASTEROID_BASE"
                }
                current_viable = (
                    current_is_asteroid
                    and not asteroid_db.is_blacklisted(ship.nav.waypoint_symbol, target_resource)
                )

                if current_viable:
                    target_asteroid = ship_wp
                    logger.info("Already at viable asteroid %s", target_asteroid.symbol)
                else:
                    # Find and rank asteroids
                    return_wp = refuel_wp or delivery_wp
                    if ship_wp:
                        candidates = rank_asteroids(
                            waypoints, ship, ship_wp.x, ship_wp.y, return_wp, asteroid_db,
                            resource=target_resource,
                        )
                    else:
                        logger.error("Can't determine ship position for asteroid ranking")
                        break

                    if not candidates:
                        logger.error("No viable asteroids found! All blacklisted or unreachable.")
                        break

                    # Pick the best reachable asteroid
                    target_asteroid = None
                    for c in candidates:
                        if c.reachable_cruise or c.reachable_drift:
                            target_asteroid = c.waypoint
                            mode = "CRUISE" if c.reachable_cruise else "DRIFT"
                            logger.info(
                                "Selected asteroid: %s (score: %.1f, dist: %.0f, mode: %s)",
                                c.waypoint.symbol, c.rank_score,
                                c.distance_from_ship, mode,
                            )
                            break

                    if not target_asteroid:
                        logger.error("No reachable asteroids! Fuel: %d/%d", ship.fuel.current, ship.fuel.capacity)
                        break

                    # Navigate to asteroid
                    dist_to_asteroid = waypoint_distance(ship_wp, target_asteroid)
                    mode = best_flight_mode(ship, dist_to_asteroid)
                    ship = await navigate_ship(client, ship, target_asteroid.symbol, mode)

                # Phase 2: Mine
                if shutdown.is_set():
                    break
                cargo_target = min(ship.cargo.capacity, remaining)
                asteroid_has_market = target_asteroid is not None and any(
                    t.symbol == "MARKETPLACE" for t in target_asteroid.traits
                )
                mining_result = await mine_asteroid(
                    client, ship_symbol, target_resource, cargo_target, asteroid_db,
                    has_marketplace=asteroid_has_market,
                    market_db=market_db,
                    system_symbol=system,
                    waypoint_symbol=target_asteroid.symbol if target_asteroid else "",
                )
                total_junk_revenue += mining_result.credits_earned
                logger.info(mining_result.summary())

                # Phase 3: Deliver
                if shutdown.is_set():
                    break
                ship = await fleet.get_ship(client, ship_symbol)
                target_count = sum(
                    i.units for i in ship.cargo.inventory if i.symbol == target_resource
                )

                if target_count > 0:
                    # Navigate to delivery point
                    dist_to_delivery = 0.0
                    if ship.nav.waypoint_symbol != delivery_wp_symbol:
                        ship_wp = wp_lookup.get(ship.nav.waypoint_symbol)
                        if ship_wp and delivery_wp:
                            dist_to_delivery = waypoint_distance(ship_wp, delivery_wp)
                        mode = best_flight_mode(ship, dist_to_delivery)
                        ship = await navigate_ship(client, ship, delivery_wp_symbol, mode)

                    # Deliver
                    contract = await contracts_api.get_contract(client, contract.id)
                    ship, delivered = await dock_and_deliver(
                        client, ship, contract, target_resource,
                    )

                    # Cache market data at delivery waypoint
                    if delivery_wp and any(t.symbol == "MARKETPLACE" for t in delivery_wp.traits):
                        try:
                            market = await navigation.get_market(client, system, delivery_wp_symbol)
                            if market.trade_goods:
                                market_db.update_market(delivery_wp_symbol, market.trade_goods)
                        except ApiError:
                            pass
                else:
                    logger.info("No %s mined this trip. Skipping delivery.", target_resource)

                # Phase 4: Refuel
                if shutdown.is_set():
                    break
                if refuel_wp:
                    if ship.nav.waypoint_symbol != refuel_wp.symbol:
                        # If delivery point has marketplace, try refueling there first
                        delivery_has_market = delivery_wp and any(
                            t.symbol == "MARKETPLACE" for t in delivery_wp.traits
                        )
                        if delivery_has_market and ship.nav.waypoint_symbol == delivery_wp_symbol:
                            ship = await try_refuel(client, ship)
                        else:
                            dist_to_refuel = 0.0
                            ship_wp = wp_lookup.get(ship.nav.waypoint_symbol)
                            if ship_wp:
                                dist_to_refuel = waypoint_distance(ship_wp, refuel_wp)
                            mode = best_flight_mode(ship, dist_to_refuel)
                            ship = await navigate_ship(client, ship, refuel_wp.symbol, mode)
                            ship = await try_refuel(client, ship)
                    else:
                        ship = await try_refuel(client, ship)
                    # Cache market data at refuel waypoint (if different from delivery)
                    if refuel_wp.symbol != delivery_wp_symbol:
                        try:
                            market = await navigation.get_market(client, system, refuel_wp.symbol)
                            if market.trade_goods:
                                market_db.update_market(refuel_wp.symbol, market.trade_goods)
                        except ApiError:
                            pass
                else:
                    logger.warning("No refuel point — operating on remaining fuel: %d/%d",
                                 ship.fuel.current, ship.fuel.capacity)

                # Fuel-critical check after refuel phase
                ship = await fleet.get_ship(client, ship_symbol)
                if ship.fuel.capacity > 0:
                    fuel_pct = ship.fuel.current / ship.fuel.capacity
                    if fuel_pct < FUEL_CRITICAL_PCT:
                        logger.error(
                            "FUEL CRITICAL: %d/%d (%.0f%%). Pausing %ds for operator intervention...",
                            ship.fuel.current, ship.fuel.capacity,
                            fuel_pct * 100, FUEL_CRITICAL_PAUSE,
                        )
                        await sleep_with_heartbeat(
                            FUEL_CRITICAL_PAUSE, "fuel critical — waiting for intervention",
                        )
                        ship = await fleet.get_ship(client, ship_symbol)
                        fuel_pct = ship.fuel.current / ship.fuel.capacity if ship.fuel.capacity > 0 else 0
                        if fuel_pct < FUEL_CRITICAL_PCT:
                            logger.error("Fuel still critical after pause. Aborting mission.")
                            break

            # Final status
            elapsed_total = (time.monotonic() - mission_start) / 60
            ag = await agent_api.get_agent(client)
            ship = await fleet.get_ship(client, ship_symbol)
            logger.info("")
            logger.info("=" * 60)
            logger.info("MISSION ENDED (%.1f min runtime)", elapsed_total)
            logger.info("  Credits: %d", ag.credits)
            logger.info("  Fuel: %d/%d", ship.fuel.current, ship.fuel.capacity)
            logger.info("  Cargo: %d/%d", ship.cargo.units, ship.cargo.capacity)
            contract = await contracts_api.get_contract(client, contract.id)
            logger.info("  Contract fulfilled: %s", contract.fulfilled)
            for d in contract.terms.deliver:
                logger.info("  %s: %d/%d", d.trade_symbol, d.units_fulfilled, d.units_required)
            if total_junk_revenue > 0:
                logger.info("  Junk revenue: %d credits (from selling mining byproducts)", total_junk_revenue)
            logger.info("=" * 60)

    except KeyboardInterrupt:
        logger.info("Mission interrupted by user.")
    except Exception:
        logger.exception("MISSION CRASHED — unexpected error")
    finally:
        asteroid_db.close()
        market_db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="SpaceTraders mining mission runner")
    parser.add_argument("--ship", default="UTMOSTLY-1", help="Ship symbol")
    parser.add_argument("--resource", default="COPPER_ORE", help="Target resource")
    parser.add_argument("--trips", type=int, default=0, help="Max trips (0 = until contract done)")
    parser.add_argument("--timeout", type=int, default=0, help="Wall-clock limit in minutes (0 = no limit)")
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(args.ship, log_dir=settings.data_dir / "logs")
    asyncio.run(run_mission(args.ship, args.resource, args.trips, args.timeout))


if __name__ == "__main__":
    main()
