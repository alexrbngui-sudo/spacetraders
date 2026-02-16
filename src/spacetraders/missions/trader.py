"""Autonomous trade route runner — finds and executes the most profitable route.

Uses cached market prices from market_db to pick the best buy→sell route,
then loops: fly to source, buy, fly to destination, sell, refuel, repeat.

Refreshes prices at each market visit so routes adapt to shifting supply/demand.

Usage:
    python -m spacetraders.missions.trader --ship UTMOSTLY-3 --loops 5
    python -m spacetraders.missions.trader --ship UTMOSTLY-3 --scout   # discover prices first

Requires: market_db with cached prices (run --scout first, or use probe_scanner).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from spacetraders.api import agent as agent_api, fleet, navigation
from spacetraders.client import ApiError, SpaceTradersClient
from spacetraders.config import load_settings
from spacetraders.data.fleet_db import FleetDB
from spacetraders.data.market_db import MarketDatabase, MarketPriceRecord
from spacetraders.data.operations_db import OperationsDB
from spacetraders.missions.probe_scanner import find_marketplace_waypoints
from spacetraders.missions.router import build_fuel_waypoints, plan_multihop
from spacetraders.missions.runner import (
    navigate_multihop,
    navigate_ship,
    setup_logging,
    try_refuel,
    wait_for_arrival,
)
from spacetraders.fleet_registry import ship_name
from spacetraders.models import MarketTradeGood, Ship, Waypoint, system_symbol_from_waypoint

logger = logging.getLogger("spacetraders.missions")

FUEL_PRICE = 72  # credits per unit, consistent across markets
FAILED_ROUTE_TTL = 1800  # seconds (30 min) before a failed route becomes eligible again
BACKOFF_SCHEDULE = [300, 600, 1200, 1800]  # seconds: 5, 10, 20, 30 min

# Supply → base multiplier for safe sell volume (units = trade_volume × multiplier).
# Conservative: LIMITED × 3.0 = 18 units with vol=6, cliff hits ~20-24 empirically.
_SUPPLY_MULTIPLIER: dict[str, float] = {
    "SCARCE": 2.0,
    "LIMITED": 3.0,
    "MODERATE": 4.0,
    "HIGH": 5.0,
    "ABUNDANT": 6.0,
}


def safe_sell_volume(
    dest_supply: str,
    dest_activity: str | None,
    trade_volume: int,
    cargo_capacity: int = 40,
) -> int:
    """Estimate how many units a destination market can absorb without crashing.

    Based on supply level and trade volume. STRONG activity adds +1.0 to
    the multiplier (faster market recovery = more absorption).
    """
    multiplier = _SUPPLY_MULTIPLIER.get(dest_supply, 3.0)
    if dest_activity == "STRONG":
        multiplier += 1.0
    return min(int(trade_volume * multiplier), cargo_capacity)


@dataclass(frozen=True)
class TradeRoute:
    """A scored trade route: buy good at source, sell at destination."""

    good: str
    source: str          # waypoint to buy at
    destination: str     # waypoint to sell at
    buy_price: int       # purchase_price at source
    sell_price: int      # sell_price at destination
    trade_volume: int    # max units per transaction
    profit_per_unit: int
    fuel_cost_credits: int  # round-trip fuel in credits
    deadhead_credits: int   # fuel cost to reach source from current position
    net_profit: int      # estimated net for volume-capped cargo after ALL fuel
    trip_seconds: int = 0              # estimated time: deadhead + source→dest
    profit_per_minute: float = 0.0     # net_profit / (trip_seconds / 60)
    dest_supply: str = "MODERATE"       # destination supply level
    dest_trade_volume: int = 60         # destination trade volume


def load_waypoint_coords(waypoints: list[Waypoint]) -> dict[str, tuple[int, int]]:
    """Build waypoint symbol → (x, y) lookup."""
    return {wp.symbol: (wp.x, wp.y) for wp in waypoints}


def _distance(
    coords: dict[str, tuple[int, int]], a: str, b: str,
) -> float | None:
    """Euclidean distance between two waypoints, or None if unknown."""
    if a == b:
        return 0.0
    if a not in coords or b not in coords:
        return None
    ax, ay = coords[a]
    bx, by = coords[b]
    return math.sqrt((bx - ax) ** 2 + (by - ay) ** 2)


def cruise_time(dist: float, speed: int) -> int:
    """CRUISE travel time in seconds: round(15 + distance × 25 / speed)."""
    return round(15 + dist * 25 / speed)


# Overhead per trip: dock + buy batches + dock + sell batches + refuel.
# ~30s is conservative based on observed API round-trip times.
TRADE_OVERHEAD_SECONDS = 30


def estimate_fuel_round_trip(
    coords: dict[str, tuple[int, int]],
    source: str,
    destination: str,
) -> int:
    """Estimate round-trip fuel cost in units (CRUISE mode: fuel = ceil(distance))."""
    dist = _distance(coords, source, destination)
    if dist is None:
        return 9999
    one_way = math.ceil(dist)
    return one_way * 2


def estimate_fuel_one_way(
    coords: dict[str, tuple[int, int]],
    origin: str,
    destination: str,
) -> int:
    """Fuel for a one-way CRUISE trip (ceil(distance)). Returns 0 if already there."""
    dist = _distance(coords, origin, destination)
    if dist is None:
        return 9999
    if dist == 0.0:
        return 0
    return max(1, math.ceil(dist))


def find_best_routes(
    market_db: MarketDatabase,
    coords: dict[str, tuple[int, int]],
    ship_location: str,
    cargo_capacity: int = 40,
    fuel_capacity: int = 300,
    excluded_routes: list[tuple[str, str, str]] | None = None,
    credits: int | None = None,
    speed: int = 36,
    system_symbol: str | None = None,
    fuel_waypoints: set[str] | None = None,
) -> list[TradeRoute]:
    """Scan all cached markets to find the most profitable routes.

    Compares every EXPORT good at every market against every IMPORT at every
    other market. Accounts for fuel cost, deadhead, AND travel time.
    Routes are ranked by profit per minute, not raw net profit.

    excluded_routes: list of (good, source, dest) tuples claimed by other ships.
    credits: if set, skip routes where one batch costs more than available credits.
    speed: ship engine speed for travel time estimates.
    system_symbol: if set, only consider markets in this system.
    """
    excluded = set(excluded_routes) if excluded_routes else set()
    all_markets = market_db.get_all_markets(system_symbol=system_symbol)
    prices_by_wp: dict[str, list[MarketPriceRecord]] = {}
    for wp in all_markets:
        prices_by_wp[wp] = market_db.get_prices(wp)

    exports: dict[str, list[tuple[str, MarketPriceRecord]]] = {}
    imports: dict[str, list[tuple[str, MarketPriceRecord]]] = {}

    for wp, records in prices_by_wp.items():
        for r in records:
            if r.type == "EXPORT":
                exports.setdefault(r.trade_symbol, []).append((wp, r))
            elif r.type == "IMPORT":
                imports.setdefault(r.trade_symbol, []).append((wp, r))

    routes: list[TradeRoute] = []
    for good, export_list in exports.items():
        import_list = imports.get(good, [])
        for src_wp, src_rec in export_list:
            for dst_wp, dst_rec in import_list:
                if src_wp == dst_wp:
                    continue

                if (good, src_wp, dst_wp) in excluded:
                    continue

                profit_per_unit = dst_rec.sell_price - src_rec.purchase_price
                if profit_per_unit <= 0:
                    continue

                # Skip if we can't afford even one batch
                if credits is not None and src_rec.purchase_price * src_rec.trade_volume > credits:
                    batch_cost = src_rec.purchase_price * src_rec.trade_volume
                    logger.debug(
                        "Skipping %s at %s: batch cost %d > %d credits",
                        good, src_wp, batch_cost, credits,
                    )
                    continue

                deadhead_fuel = estimate_fuel_one_way(coords, ship_location, src_wp)
                leg_fuel = estimate_fuel_one_way(coords, src_wp, dst_wp)

                # Compute travel times (may be overridden by multi-hop)
                deadhead_dist = _distance(coords, ship_location, src_wp) or 0.0
                leg_dist = _distance(coords, src_wp, dst_wp) or 0.0
                deadhead_secs = cruise_time(deadhead_dist, speed) if deadhead_dist > 0 else 0
                leg_secs = cruise_time(leg_dist, speed)

                # Multi-hop for legs beyond direct fuel range
                if deadhead_fuel > fuel_capacity:
                    if not fuel_waypoints:
                        continue
                    dh_plan = plan_multihop(
                        coords, fuel_waypoints, ship_location, src_wp,
                        fuel_capacity, speed,
                    )
                    if not dh_plan.feasible:
                        continue
                    deadhead_fuel = dh_plan.total_fuel
                    deadhead_secs = dh_plan.total_seconds

                if leg_fuel > fuel_capacity:
                    if not fuel_waypoints:
                        continue
                    leg_plan = plan_multihop(
                        coords, fuel_waypoints, src_wp, dst_wp,
                        fuel_capacity, speed,
                    )
                    if not leg_plan.feasible:
                        continue
                    leg_fuel = leg_plan.total_fuel
                    leg_secs = leg_plan.total_seconds

                route_fuel = leg_fuel * 2  # approximate round-trip for scoring
                route_fuel_credits = route_fuel * FUEL_PRICE
                deadhead_credits = deadhead_fuel * FUEL_PRICE
                safe_units = safe_sell_volume(
                    dst_rec.supply, dst_rec.activity,
                    dst_rec.trade_volume, cargo_capacity,
                )
                gross = profit_per_unit * safe_units
                net = gross - route_fuel_credits - deadhead_credits

                if net <= 0:
                    continue

                # Time estimate: deadhead + leg + overhead
                trip_secs = deadhead_secs + leg_secs + TRADE_OVERHEAD_SECONDS
                ppm = (net / trip_secs) * 60 if trip_secs > 0 else 0.0

                routes.append(TradeRoute(
                    good=good,
                    source=src_wp,
                    destination=dst_wp,
                    buy_price=src_rec.purchase_price,
                    sell_price=dst_rec.sell_price,
                    trade_volume=src_rec.trade_volume,
                    profit_per_unit=profit_per_unit,
                    fuel_cost_credits=route_fuel_credits,
                    deadhead_credits=deadhead_credits,
                    net_profit=net,
                    trip_seconds=trip_secs,
                    profit_per_minute=ppm,
                    dest_supply=dst_rec.supply,
                    dest_trade_volume=dst_rec.trade_volume,
                ))

    routes.sort(key=lambda r: r.profit_per_minute, reverse=True)
    return routes


async def refresh_market(
    client: SpaceTradersClient,
    ship_symbol: str,
    waypoint: str,
    market_db: MarketDatabase,
) -> list[MarketTradeGood]:
    """Fetch live market data at current waypoint and update cache. Returns trade goods."""
    system = system_symbol_from_waypoint(waypoint)
    market = await navigation.get_market(client, system, waypoint)
    if market.trade_goods:
        market_db.update_market(waypoint, market.trade_goods, system_symbol=system)
        logger.info("Refreshed prices at %s (%d goods)", waypoint, len(market.trade_goods))
    return market.trade_goods


async def buy_cargo(
    client: SpaceTradersClient,
    ship_symbol: str,
    good: str,
    target_units: int,
    trade_volume: int,
    *,
    ops_db: OperationsDB | None = None,
    waypoint: str = "",
    mission: str = "trade",
) -> tuple[int, int]:
    """Buy good in batches. Returns (units_bought, total_cost)."""
    bought = 0
    cost = 0
    while bought < target_units:
        batch = min(trade_volume, target_units - bought)
        try:
            result = await fleet.purchase_cargo(client, ship_symbol, good, batch)
            tx = result.get("transaction", {})
            units = tx.get("units", batch)
            price = tx.get("totalPrice", 0)
            ppu = tx.get("pricePerUnit", 0)
            bought += units
            cost += price
            balance = result.get("agent", {}).get("credits", "?")
            logger.info(
                "  Bought %d %s @ %d/unit (%d/%d). Balance: %s",
                units, good, ppu, bought, target_units,
                f"{balance:,}" if isinstance(balance, int) else balance,
            )
            if ops_db:
                ops_db.record_trade(
                    ship_symbol, "BUY", good, units, ppu, price,
                    waypoint or tx.get("waypointSymbol", ""),
                    balance if isinstance(balance, int) else None,
                    mission,
                )
        except ApiError as e:
            logger.warning("  Buy failed (%d): %s — bought %d so far", e.code, e, bought)
            break
    return bought, cost


async def sell_cargo(
    client: SpaceTradersClient,
    ship_symbol: str,
    good: str,
    units: int,
    trade_volume: int,
    *,
    ops_db: OperationsDB | None = None,
    waypoint: str = "",
    mission: str = "trade",
) -> tuple[int, int]:
    """Sell good in batches. Returns (units_sold, total_revenue)."""
    sold = 0
    revenue = 0
    remaining = units
    while remaining > 0:
        batch = min(trade_volume, remaining)
        try:
            result = await fleet.sell_cargo(client, ship_symbol, good, batch)
            tx = result.get("transaction", {})
            u = tx.get("units", batch)
            rev = tx.get("totalPrice", 0)
            ppu = tx.get("pricePerUnit", 0)
            sold += u
            revenue += rev
            remaining -= u
            balance = result.get("agent", {}).get("credits", "?")
            logger.info(
                "  Sold %d %s @ %d/unit (%d remaining). Balance: %s",
                u, good, ppu, remaining,
                f"{balance:,}" if isinstance(balance, int) else balance,
            )
            if ops_db:
                ops_db.record_trade(
                    ship_symbol, "SELL", good, u, ppu, rev,
                    waypoint or tx.get("waypointSymbol", ""),
                    balance if isinstance(balance, int) else None,
                    mission,
                )
        except ApiError as e:
            logger.warning("  Sell failed (%d): %s — sold %d so far", e.code, e, sold)
            break
    return sold, revenue


async def sell_existing_cargo(
    client: SpaceTradersClient,
    ship: Ship,
    ship_symbol: str,
    market_db: MarketDatabase,
    coords: dict[str, tuple[int, int]],
    speed: int,
) -> Ship:
    """Sell any existing cargo at the best available market before trading.

    Groups cargo items by their best sell destination, picks the destination
    that maximizes total revenue, flies there, and sells everything possible.
    Repeats until cargo is empty or no sell destinations remain.
    Returns the updated ship.
    """
    total_sold_units = 0
    total_revenue = 0

    while True:
        ship = await fleet.get_ship(client, ship_symbol)
        if ship.cargo.units == 0:
            break

        items = [(i.symbol, i.units) for i in ship.cargo.inventory]
        logger.info(
            "Existing cargo to sell: %s",
            ", ".join(f"{u}x {s}" for s, u in items),
        )

        # For each item, find the best sell market from cache (scoped to current system)
        # Score each market by total estimated revenue across all items
        system = ship.nav.system_symbol
        market_scores: dict[str, int] = {}
        for symbol, units in items:
            best = market_db.find_best_sell(symbol, system_symbol=system)
            if best:
                market_scores.setdefault(best.waypoint_symbol, 0)
                market_scores[best.waypoint_symbol] += best.sell_price * units

        if not market_scores:
            logger.warning("No cached sell destinations for cargo. Jettisoning.")
            for symbol, units in items:
                await fleet.jettison_cargo(client, ship_symbol, symbol, units)
                logger.info("  Jettisoned %dx %s", units, symbol)
            ship = await fleet.get_ship(client, ship_symbol)
            break

        # Pick market with highest total revenue, weighted by travel time
        best_wp = None
        best_rate = 0.0
        for wp, revenue in market_scores.items():
            dist = _distance(coords, ship.nav.waypoint_symbol, wp) or 0.0
            trip_secs = cruise_time(dist, speed) + TRADE_OVERHEAD_SECONDS if dist > 0 else TRADE_OVERHEAD_SECONDS
            rate = revenue / trip_secs
            if rate > best_rate:
                best_rate = rate
                best_wp = wp

        if not best_wp:
            break

        logger.info("Selling cargo at %s (est revenue/sec: %.0f)", best_wp, best_rate)

        # Fly to market and sell
        if ship.nav.status.value == "IN_TRANSIT":
            while ship.nav.status.value == "IN_TRANSIT":
                ship = await wait_for_arrival(client, ship_symbol)
        if ship.nav.waypoint_symbol != best_wp:
            ship = await navigate_ship(client, ship, best_wp)
            while ship.nav.status.value == "IN_TRANSIT":
                ship = await wait_for_arrival(client, ship_symbol)
        if ship.nav.status.value != "DOCKED":
            await fleet.dock(client, ship_symbol)
        await refresh_market(client, ship_symbol, best_wp, market_db)
        ship = await try_refuel(client, ship)

        # Sell everything we can at this market — use cached trade_volume
        wp_prices = market_db.get_prices(best_wp)
        vol_lookup = {p.trade_symbol: p.trade_volume for p in wp_prices}
        for symbol, units in items:
            trade_vol = vol_lookup.get(symbol, 20)
            try:
                sold, rev = await sell_cargo(
                    client, ship_symbol, symbol, units, trade_vol,
                )
                total_sold_units += sold
                total_revenue += rev
            except ApiError as e:
                logger.info("  Can't sell %s here (%d), will try elsewhere", symbol, e.code)

    if total_sold_units > 0:
        logger.info(
            "Cargo cleanup complete: sold %d units for %d credits total",
            total_sold_units, total_revenue,
        )

    ship = await fleet.get_ship(client, ship_symbol)
    return ship


async def run_scout(
    client: SpaceTradersClient,
    ship: Ship,
    ship_symbol: str,
    market_db: MarketDatabase,
    stops: list[str],
) -> Ship:
    """Fly through a list of waypoints, docking at each to discover prices."""
    for wp in stops:
        ship = await navigate_ship(client, ship, wp)
        if ship.nav.status.value != "DOCKED":
            await fleet.dock(client, ship_symbol)
        goods = await refresh_market(client, ship_symbol, wp, market_db)
        if goods:
            for g in sorted(goods, key=lambda x: x.symbol):
                logger.info(
                    "  %-25s %-8s buy=%6d sell=%6d vol=%3d supply=%s",
                    g.symbol, g.type, g.purchase_price, g.sell_price,
                    g.trade_volume, g.supply,
                )
        # Refuel if fuel available
        has_fuel = any(g.symbol == "FUEL" for g in goods)
        if has_fuel:
            ship = await try_refuel(client, ship)
    return ship


async def run_trade(
    ship_symbol: str,
    loops: int = 1,
    scout: bool = False,
    continuous: bool = False,
) -> None:
    """Main entry point: find best route from cached data, execute trade loops.

    In continuous mode, repeats indefinitely: runs `loops` trades per cycle,
    logs a summary, then starts the next cycle. Stops on SIGINT/SIGTERM.
    """
    settings = load_settings()
    market_db = MarketDatabase(db_path=settings.data_dir / "markets.db")
    ops_db = OperationsDB(db_path=settings.data_dir / "operations.db")
    fleet_db = FleetDB(db_path=settings.data_dir / "fleet.db")

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    fleet_db.release_dead()
    if not fleet_db.assign(ship_symbol, "trade"):
        logger.error("Ship %s is already assigned to another mission!", ship_symbol)
        fleet_db.close()
        ops_db.close()
        market_db.close()
        return

    try:
        async with SpaceTradersClient(settings) as client:
            ag = await agent_api.get_agent(client)
            ship = await fleet.get_ship(client, ship_symbol)
            ship = await wait_for_arrival(client, ship_symbol)
            system = ship.nav.system_symbol
            waypoints = await navigation.list_waypoints(client, system)
            coords = load_waypoint_coords(waypoints)
            fuel_waypoints = build_fuel_waypoints(waypoints)

            name = ship_name(ship_symbol)
            logger.info("=" * 60)
            logger.info("TRADER [%s]: %s at %s | %d credits | fuel %d/%d",
                        name, ship_symbol, ship.nav.waypoint_symbol, ag.credits,
                        ship.fuel.current, ship.fuel.capacity)
            if continuous:
                logger.info("MODE: continuous (%d loops per cycle)", loops)
            logger.info("=" * 60)

            session_start_credits = ag.credits
            ops_db.snapshot_agent(ag.credits, ag.ship_count)

            # --- Scout mode: discover prices at key markets ---
            if scout:
                marketplace_wps = find_marketplace_waypoints(waypoints)
                scout_stops = [wp.symbol for wp in marketplace_wps]
                logger.info("SCOUTING %d markets in %s...", len(scout_stops), system)
                ship = await run_scout(client, ship, ship_symbol, market_db, scout_stops)
                logger.info("Scouting complete.")

            cycle = 0
            total_loops = 0
            failed_routes: dict[tuple[str, str, str], float] = {}  # key → monotonic time of failure
            dry_streak = 0  # consecutive cycles with 0 successful trades
            negative_streak = 0  # consecutive cycles with negative profit

            while not shutdown.is_set():
                cycle += 1
                cycle_start_credits = (await agent_api.get_agent(client)).credits
                cycle_successes = 0

                # Prune expired failures (routes get a second chance after TTL)
                now = time.monotonic()
                failed_routes = {k: v for k, v in failed_routes.items() if now - v < FAILED_ROUTE_TTL}
                if failed_routes:
                    logger.info("Failed route memory: %d routes blacklisted (TTL %d min)",
                                len(failed_routes), FAILED_ROUTE_TTL // 60)

                # --- Wait for in-transit ship (e.g. after crash/restart) ---
                ship = await fleet.get_ship(client, ship_symbol)
                if ship.nav.status.value == "IN_TRANSIT":
                    logger.info("Ship is in transit — waiting for arrival...")
                    while ship.nav.status.value == "IN_TRANSIT":
                        ship = await wait_for_arrival(client, ship_symbol)

                # --- Sell any existing cargo before trading ---
                if ship.cargo.units > 0:
                    ship = await sell_existing_cargo(
                        client, ship, ship_symbol, market_db, coords, ship.engine.speed,
                    )

                # --- Find best route ---
                ship = await fleet.get_ship(client, ship_symbol)
                ag = await agent_api.get_agent(client)
                claimed = market_db.get_claimed_routes(exclude_ship=ship_symbol)
                if claimed:
                    logger.info("Routes claimed by other ships: %s", claimed)
                all_excluded = list(claimed) + list(failed_routes) if claimed else list(failed_routes)
                ship_speed = ship.engine.speed
                routes = find_best_routes(
                    market_db, coords, ship.nav.waypoint_symbol,
                    ship.cargo.capacity, ship.fuel.capacity,
                    excluded_routes=all_excluded or None,
                    credits=ag.credits,
                    speed=ship_speed,
                    system_symbol=system,
                    fuel_waypoints=fuel_waypoints,
                )
                if not routes:
                    if continuous:
                        logger.warning("No profitable routes. Sleeping 5 min...")
                        try:
                            await asyncio.wait_for(shutdown.wait(), timeout=300)
                        except asyncio.TimeoutError:
                            pass
                        continue
                    else:
                        logger.error("No profitable routes found. Run with --scout to discover prices.")
                        break

                logger.info("")
                logger.info("Top routes (by profit/min, from %s):", ship.nav.waypoint_symbol)
                for r in routes[:5]:
                    deadhead = f" +dh={r.deadhead_credits}" if r.deadhead_credits > 0 else ""
                    trip_min = r.trip_seconds / 60
                    logger.info(
                        "  %-20s %s → %s  net=%+d  %.1fmin  %d/min%s",
                        r.good, r.source[-3:], r.destination[-3:],
                        r.net_profit, trip_min, round(r.profit_per_minute), deadhead,
                    )

                # --- Trade loops within this cycle ---
                prev_route_key: tuple[str, str, str] | None = None
                cycle_trip_log: list[tuple[str, int]] = []  # (good, profit) per trip

                for loop_num in range(1, loops + 1):
                    if shutdown.is_set():
                        break

                    # Re-rank routes each loop (prices shift + new position)
                    if loop_num > 1:
                        ship = await fleet.get_ship(client, ship_symbol)
                        ag = await agent_api.get_agent(client)
                        claimed = market_db.get_claimed_routes(exclude_ship=ship_symbol)
                        all_excluded = list(claimed) + list(failed_routes) if claimed else list(failed_routes)
                        routes = find_best_routes(
                            market_db, coords, ship.nav.waypoint_symbol,
                            ship.cargo.capacity, ship.fuel.capacity,
                            excluded_routes=all_excluded or None,
                            credits=ag.credits,
                            speed=ship_speed,
                            system_symbol=system,
                            fuel_waypoints=fuel_waypoints,
                        )
                        if not routes:
                            logger.warning("No profitable routes left this cycle (failed: %d).", len(failed_routes))
                            break

                    best = routes[0]
                    route_key = (best.good, best.source, best.destination)
                    market_db.claim_route(ship_symbol, best.good, best.source, best.destination)
                    total_loops += 1

                    # Log route change
                    if prev_route_key and route_key != prev_route_key:
                        logger.info(
                            "Route changed: %s %s→%s  ⇒  %s %s→%s",
                            prev_route_key[0], prev_route_key[1][-3:], prev_route_key[2][-3:],
                            best.good, best.source[-3:], best.destination[-3:],
                        )
                    prev_route_key = route_key

                    logger.info("")
                    trip_min = best.trip_seconds / 60
                    logger.info(
                        "### LOOP %d/%d (cycle %d) — %s: %s → %s  est net=%+d  buy=%d sell=%d  %.1fmin ###",
                        loop_num, loops, cycle, best.good,
                        best.source[-3:], best.destination[-3:], best.net_profit,
                        best.buy_price, best.sell_price, trip_min,
                    )

                    # 1. Fly to source (multi-hop if needed)
                    ship = await fleet.get_ship(client, ship_symbol)
                    dh_fuel = estimate_fuel_one_way(coords, ship.nav.waypoint_symbol, best.source)
                    if dh_fuel > ship.fuel.capacity and fuel_waypoints:
                        dh_plan = plan_multihop(
                            coords, fuel_waypoints, ship.nav.waypoint_symbol,
                            best.source, ship.fuel.capacity, ship_speed,
                        )
                        if dh_plan.feasible and dh_plan.num_stops > 0:
                            logger.info("Multi-hop to source (%d stops)", dh_plan.num_stops)
                            ship = await navigate_multihop(client, ship, dh_plan)
                        else:
                            ship = await navigate_ship(client, ship, best.source)
                    else:
                        ship = await navigate_ship(client, ship, best.source)
                    if ship.nav.status.value != "DOCKED":
                        await fleet.dock(client, ship_symbol)

                    # Refresh source prices and refuel before buying
                    await refresh_market(client, ship_symbol, best.source, market_db)
                    ship = await try_refuel(client, ship)

                    # 2. Buy (volume-capped to avoid crashing destination market)
                    space = ship.cargo.capacity - ship.cargo.units
                    if space == 0:
                        logger.warning(
                            "Cargo full (%d/%d) mid-cycle. Selling first. Cargo: %s",
                            ship.cargo.units, ship.cargo.capacity,
                            ", ".join(f"{i.units}x {i.symbol}" for i in ship.cargo.inventory),
                        )
                        ship = await sell_existing_cargo(
                            client, ship, ship_symbol, market_db, coords, ship_speed,
                        )
                        space = ship.cargo.capacity - ship.cargo.units
                        if space == 0:
                            logger.error("Still full after selling. Breaking.")
                            break

                    dest_prices = market_db.get_prices(best.destination)
                    dest_good = next(
                        (p for p in dest_prices if p.trade_symbol == best.good), None,
                    )
                    if dest_good:
                        cap = safe_sell_volume(
                            dest_good.supply, dest_good.activity,
                            dest_good.trade_volume, space,
                        )
                        if cap < space:
                            logger.info(
                                "Volume cap: %s at %s is %s supply — buying %d not %d",
                                best.good, best.destination, dest_good.supply, cap, space,
                            )
                            space = cap

                    units_bought, total_cost = await buy_cargo(
                        client, ship_symbol, best.good, space, best.trade_volume,
                        ops_db=ops_db, waypoint=best.source, mission="trade",
                    )
                    if units_bought == 0:
                        failed_routes[(best.good, best.source, best.destination)] = time.monotonic()
                        logger.warning(
                            "Couldn't buy any %s at %s. Route blacklisted for %d min (%d total failed).",
                            best.good, best.source, FAILED_ROUTE_TTL // 60, len(failed_routes),
                        )
                        continue

                    # Log cargo state after buy
                    ship = await fleet.get_ship(client, ship_symbol)
                    logger.info(
                        "Cargo after buy: %d/%d — %s",
                        ship.cargo.units, ship.cargo.capacity,
                        ", ".join(f"{i.units}x {i.symbol}" for i in ship.cargo.inventory),
                    )

                    # 3. Fly to destination (multi-hop if needed)
                    ship = await fleet.get_ship(client, ship_symbol)
                    leg_fuel_now = estimate_fuel_one_way(coords, ship.nav.waypoint_symbol, best.destination)
                    if leg_fuel_now > ship.fuel.capacity and fuel_waypoints:
                        leg_plan = plan_multihop(
                            coords, fuel_waypoints, ship.nav.waypoint_symbol,
                            best.destination, ship.fuel.capacity, ship_speed,
                        )
                        if leg_plan.feasible and leg_plan.num_stops > 0:
                            logger.info("Multi-hop to destination (%d stops)", leg_plan.num_stops)
                            ship = await navigate_multihop(client, ship, leg_plan)
                        else:
                            ship = await navigate_ship(client, ship, best.destination)
                    else:
                        ship = await navigate_ship(client, ship, best.destination)
                    if ship.nav.status.value != "DOCKED":
                        await fleet.dock(client, ship_symbol)

                    # Refresh destination prices
                    dest_goods = await refresh_market(client, ship_symbol, best.destination, market_db)

                    # 4. Sell — use destination's trade_volume (may differ from source)
                    dest_vol = best.trade_volume
                    for dg in dest_goods:
                        if dg.symbol == best.good:
                            dest_vol = dg.trade_volume
                            break
                    units_sold, total_revenue = await sell_cargo(
                        client, ship_symbol, best.good, units_bought, dest_vol,
                        ops_db=ops_db, waypoint=best.destination, mission="trade",
                    )
                    cycle_successes += 1

                    trip_profit = total_revenue - total_cost
                    cycle_trip_log.append((best.good, trip_profit))
                    logger.info(
                        "Trip P&L: bought %d for %d, sold %d for %d → %+d credits (estimated %+d)",
                        units_bought, total_cost, units_sold, total_revenue,
                        trip_profit, best.net_profit,
                    )

                    # 5. Refuel
                    ship = await fleet.get_ship(client, ship_symbol)
                    ship = await try_refuel(client, ship)

                    ag = await agent_api.get_agent(client)
                    logger.info(
                        "Balance: %d (%+d session) | Fuel: %d/%d",
                        ag.credits, ag.credits - session_start_credits,
                        ship.fuel.current, ship.fuel.capacity,
                    )

                # --- Cycle summary ---
                ag = await agent_api.get_agent(client)
                cycle_profit = ag.credits - cycle_start_credits
                logger.info("")
                logger.info("=" * 60)
                logger.info(
                    "CYCLE %d COMPLETE — %+d credits this cycle | %+d session total | %d successful trades",
                    cycle, cycle_profit, ag.credits - session_start_credits, cycle_successes,
                )
                if cycle_trip_log:
                    recap = ", ".join(f"{g} {p:+d}" for g, p in cycle_trip_log)
                    logger.info("  Trades: %s", recap)
                logger.info("  Balance: %d credits | Total loops: %d", ag.credits, total_loops)
                logger.info("=" * 60)

                if not continuous:
                    break

                # --- Negative cycle auto-park ---
                if cycle_profit < 0 and cycle_successes > 0:
                    negative_streak += 1
                    if negative_streak >= 2:
                        logger.warning(
                            "Negative streak %d — parking for 30 min to let markets recover.",
                            negative_streak,
                        )
                        try:
                            await asyncio.wait_for(shutdown.wait(), timeout=1800)
                        except asyncio.TimeoutError:
                            pass
                elif cycle_profit >= 0:
                    negative_streak = 0

                # --- Dry cycle backoff ---
                if cycle_successes == 0:
                    dry_streak += 1
                    backoff = BACKOFF_SCHEDULE[min(dry_streak - 1, len(BACKOFF_SCHEDULE) - 1)]
                    logger.warning(
                        "Dry cycle %d — all markets empty. Sleeping %d min.",
                        dry_streak, backoff // 60,
                    )
                    try:
                        await asyncio.wait_for(shutdown.wait(), timeout=backoff)
                    except asyncio.TimeoutError:
                        pass
                else:
                    dry_streak = 0

            # --- Session summary ---
            ag = await agent_api.get_agent(client)
            logger.info("")
            logger.info("=" * 60)
            logger.info("TRADER SHUTDOWN — %d cycles, %d total loops", cycle, total_loops)
            logger.info("  Start:  %d credits", session_start_credits)
            logger.info("  End:    %d credits", ag.credits)
            logger.info("  Profit: %+d credits", ag.credits - session_start_credits)
            logger.info("=" * 60)

    except KeyboardInterrupt:
        logger.info("Trade interrupted.")
    except Exception:
        logger.exception("TRADER CRASHED")
    finally:
        fleet_db.release(ship_symbol)
        fleet_db.close()
        market_db.release_route(ship_symbol)
        market_db.close()
        ops_db.close()


def _auto_pick_trader(fleet_db: FleetDB) -> str | None:
    """Pick the best available ship for trading (largest cargo first)."""
    available = fleet_db.available("ship")
    return available[0] if available else None


def main() -> None:
    parser = argparse.ArgumentParser(description="SpaceTraders autonomous trader")
    parser.add_argument(
        "--ship", default=None,
        help="Ship symbol (omit to auto-pick from pool)",
    )
    parser.add_argument("--loops", type=int, default=3, help="Trade loops per cycle")
    parser.add_argument("--scout", action="store_true", help="Scout markets before trading")
    parser.add_argument(
        "--continuous", action="store_true",
        help="Run indefinitely — repeat cycles until stopped",
    )
    args = parser.parse_args()

    settings = load_settings()

    ship = args.ship
    if not ship:
        fleet_db = FleetDB(db_path=settings.data_dir / "fleet.db")
        fleet_db.release_dead()
        ship = _auto_pick_trader(fleet_db)
        fleet_db.close()
        if not ship:
            print("No ships available for trading!")
            sys.exit(1)
        print(f"Auto-picked {ship_name(ship)} ({ship}) for trading")

    setup_logging(ship, log_dir=settings.data_dir / "logs")
    asyncio.run(run_trade(ship, args.loops, args.scout, args.continuous))


if __name__ == "__main__":
    main()
