"""Mission adapters — bridge existing mission scripts to the fleet commander.

Importing this module registers all mission coroutines with the registry.
Each adapter wraps an existing mission's inner loop, reusing all the
proven logic while plugging into FleetState for coordination.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from spacetraders.client import SpaceTradersClient
from spacetraders.fleet.events import EventType, FleetEvent
from spacetraders.fleet.missions import MissionType, register_mission
from spacetraders.fleet.state import FleetState
from spacetraders.fleet.system_intel import load_system_intel
from spacetraders.fleet_registry import ship_name

logger = logging.getLogger(__name__)


def _make_emitter(
    state: FleetState,
) -> Any:
    """Create an on_event callback that pushes FleetEvents onto the state queue.

    Returns a function matching the OnEventCallback signature:
        (event_type_str, ship_symbol, data) -> None
    """
    def emit(event_type_str: str, ship_symbol: str, data: dict[str, Any]) -> None:
        try:
            event_type = EventType(event_type_str)
        except ValueError:
            logger.warning("Unknown event type: %s", event_type_str)
            return
        state.emit(FleetEvent(
            type=event_type,
            ship_symbol=ship_symbol,
            data=data,
        ))
    return emit


# ---------------------------------------------------------------------------
# TRADE adapter
# ---------------------------------------------------------------------------


async def trade_mission(
    client: SpaceTradersClient,
    ship_symbol: str,
    state: FleetState,
    *,
    loops_per_cycle: int = 3,
    **kwargs: Any,
) -> None:
    """Fleet commander adapter for the autonomous trader.

    Wraps trader.run_trade's inner loop, using FleetState for shutdown
    signaling and route claim coordination. The core buy/sell/navigate
    logic is reused from the existing trader module.
    """
    from spacetraders.api import agent as agent_api, fleet as fleet_api, navigation
    from spacetraders.missions.router import build_fuel_waypoints, plan_multihop
    from spacetraders.missions.runner import navigate_multihop, navigate_ship, try_refuel, wait_for_arrival
    from spacetraders.missions.trader import (
        buy_cargo,
        estimate_fuel_one_way,
        find_best_routes,
        load_waypoint_coords,
        refresh_market,
        safe_sell_volume,
        sell_cargo,
        sell_existing_cargo,
        FAILED_ROUTE_TTL,
        BACKOFF_SCHEDULE,
    )
    import time

    name = ship_name(ship_symbol)
    log = logger

    ship = await fleet_api.get_ship(client, ship_symbol)
    ship = await wait_for_arrival(client, ship_symbol)
    system = ship.nav.system_symbol

    # Ensure system intel is loaded
    sys_state = await load_system_intel(client, system, state)
    coords = sys_state.coords
    fuel_waypoints = build_fuel_waypoints(sys_state.waypoints)

    ag = await agent_api.get_agent(client)
    log.info(
        "[%s] TRADE mission started at %s | %s credits | fuel %d/%d",
        name, ship.nav.waypoint_symbol, f"{ag.credits:,}",
        ship.fuel.current, ship.fuel.capacity,
    )

    session_start_credits = ag.credits
    if state.ops_db:
        state.ops_db.snapshot_agent(ag.credits, ag.ship_count)
    cycle = 0
    failed_routes: dict[tuple[str, str, str], float] = {}
    dry_streak = 0

    while not state.shutdown.is_set():
        cycle += 1
        cycle_start_credits = (await agent_api.get_agent(client)).credits
        cycle_successes = 0

        # Prune expired failures
        now = time.monotonic()
        failed_routes = {
            k: v for k, v in failed_routes.items()
            if now - v < FAILED_ROUTE_TTL
        }

        # Sell existing cargo
        ship = await fleet_api.get_ship(client, ship_symbol)
        if ship.cargo.units > 0:
            ship = await sell_existing_cargo(
                client, ship, ship_symbol, state.market_db, coords, ship.engine.speed,
            )

        # Find best route
        ship = await fleet_api.get_ship(client, ship_symbol)
        ag = await agent_api.get_agent(client)

        # Use in-memory claims from FleetState
        excluded = state.get_excluded_routes(system, ship_symbol)
        all_excluded = excluded + list(failed_routes.keys())
        ship_speed = ship.engine.speed

        routes = find_best_routes(
            state.market_db, coords, ship.nav.waypoint_symbol,
            ship.cargo.capacity, ship.fuel.capacity,
            excluded_routes=all_excluded or None,
            credits=ag.credits,
            speed=ship_speed,
            system_symbol=system,
            fuel_waypoints=fuel_waypoints,
        )

        if not routes:
            dry_streak += 1
            backoff = BACKOFF_SCHEDULE[min(dry_streak - 1, len(BACKOFF_SCHEDULE) - 1)]
            log.warning(
                "[%s] No profitable routes (dry streak %d). Sleeping %d min...",
                name, dry_streak, backoff // 60,
            )
            state.emit(FleetEvent(
                type=EventType.TRADE_DRY,
                ship_symbol=ship_symbol,
                data={"dry_streak": dry_streak},
            ))
            try:
                await asyncio.wait_for(state.shutdown.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            continue

        log.info("[%s] Top routes from %s:", name, ship.nav.waypoint_symbol)
        for r in routes[:3]:
            log.info(
                "[%s]   %-18s %s→%s net=%+d %d/min",
                name, r.good, r.source[-3:], r.destination[-3:],
                r.net_profit, round(r.profit_per_minute),
            )

        # Trade loops within cycle
        for loop_num in range(1, loops_per_cycle + 1):
            if state.shutdown.is_set():
                break

            if loop_num > 1:
                ship = await fleet_api.get_ship(client, ship_symbol)
                ag = await agent_api.get_agent(client)
                excluded = state.get_excluded_routes(system, ship_symbol)
                all_excluded = excluded + list(failed_routes.keys())
                routes = find_best_routes(
                    state.market_db, coords, ship.nav.waypoint_symbol,
                    ship.cargo.capacity, ship.fuel.capacity,
                    excluded_routes=all_excluded or None,
                    credits=ag.credits,
                    speed=ship_speed,
                    system_symbol=system,
                    fuel_waypoints=fuel_waypoints,
                )
                if not routes:
                    break

            best = routes[0]
            state.claim_route(system, ship_symbol, best.good, best.source, best.destination)

            log.info(
                "[%s] Loop %d/%d — %s: %s→%s est net=%+d",
                name, loop_num, loops_per_cycle, best.good,
                best.source[-3:], best.destination[-3:], best.net_profit,
            )

            # Fly to source (multi-hop if needed)
            ship = await fleet_api.get_ship(client, ship_symbol)
            dh_fuel = estimate_fuel_one_way(coords, ship.nav.waypoint_symbol, best.source)
            if dh_fuel > ship.fuel.capacity and fuel_waypoints:
                dh_plan = plan_multihop(
                    coords, fuel_waypoints, ship.nav.waypoint_symbol,
                    best.source, ship.fuel.capacity, ship_speed,
                )
                if dh_plan.feasible and dh_plan.num_stops > 0:
                    log.info("[%s] Multi-hop to source (%d stops)", name, dh_plan.num_stops)
                    ship = await navigate_multihop(client, ship, dh_plan)
                else:
                    ship = await navigate_ship(client, ship, best.source)
            else:
                ship = await navigate_ship(client, ship, best.source)
            if ship.nav.status.value != "DOCKED":
                await fleet_api.dock(client, ship_symbol)
            await refresh_market(client, ship_symbol, best.source, state.market_db)
            ship = await try_refuel(client, ship)

            space = ship.cargo.capacity - ship.cargo.units
            if space == 0:
                ship = await sell_existing_cargo(
                    client, ship, ship_symbol, state.market_db, coords, ship_speed,
                )
                space = ship.cargo.capacity - ship.cargo.units
                if space == 0:
                    break

            # Volume cap
            dest_prices = state.market_db.get_prices(best.destination)
            dest_good = next(
                (p for p in dest_prices if p.trade_symbol == best.good), None,
            )
            if dest_good:
                cap = safe_sell_volume(
                    dest_good.supply, dest_good.activity,
                    dest_good.trade_volume, space,
                )
                if cap < space:
                    space = cap

            units_bought, total_cost = await buy_cargo(
                client, ship_symbol, best.good, space, best.trade_volume,
                ops_db=state.ops_db, waypoint=best.source, mission="trade",
            )
            if units_bought == 0:
                failed_routes[(best.good, best.source, best.destination)] = time.monotonic()
                log.warning("[%s] Buy failed — route blacklisted", name)
                continue

            # Fly to destination (multi-hop if needed)
            ship = await fleet_api.get_ship(client, ship_symbol)
            leg_fuel_now = estimate_fuel_one_way(coords, ship.nav.waypoint_symbol, best.destination)
            if leg_fuel_now > ship.fuel.capacity and fuel_waypoints:
                leg_plan = plan_multihop(
                    coords, fuel_waypoints, ship.nav.waypoint_symbol,
                    best.destination, ship.fuel.capacity, ship_speed,
                )
                if leg_plan.feasible and leg_plan.num_stops > 0:
                    log.info("[%s] Multi-hop to destination (%d stops)", name, leg_plan.num_stops)
                    ship = await navigate_multihop(client, ship, leg_plan)
                else:
                    ship = await navigate_ship(client, ship, best.destination)
            else:
                ship = await navigate_ship(client, ship, best.destination)
            if ship.nav.status.value != "DOCKED":
                await fleet_api.dock(client, ship_symbol)
            dest_goods = await refresh_market(
                client, ship_symbol, best.destination, state.market_db,
            )

            dest_vol = best.trade_volume
            for dg in dest_goods:
                if dg.symbol == best.good:
                    dest_vol = dg.trade_volume
                    break

            units_sold, total_revenue = await sell_cargo(
                client, ship_symbol, best.good, units_bought, dest_vol,
                ops_db=state.ops_db, waypoint=best.destination, mission="trade",
            )
            cycle_successes += 1

            trip_profit = total_revenue - total_cost
            log.info(
                "[%s] P&L: %+d credits (bought %d for %d, sold %d for %d)",
                name, trip_profit, units_bought, total_cost, units_sold, total_revenue,
            )

            # Emit trade completed event
            ag_now = await agent_api.get_agent(client)
            state.emit(FleetEvent(
                type=EventType.TRADE_COMPLETED,
                ship_symbol=ship_symbol,
                data={
                    "good": best.good,
                    "profit": trip_profit,
                    "credits": ag_now.credits,
                },
            ))

            ship = await fleet_api.get_ship(client, ship_symbol)
            ship = await try_refuel(client, ship)

        # Cycle summary
        ag = await agent_api.get_agent(client)
        cycle_profit = ag.credits - cycle_start_credits
        log.info(
            "[%s] Cycle %d: %+d credits (%d trades) | Balance: %s",
            name, cycle, cycle_profit, cycle_successes, f"{ag.credits:,}",
        )

        if cycle_successes == 0:
            dry_streak += 1
            backoff = BACKOFF_SCHEDULE[min(dry_streak - 1, len(BACKOFF_SCHEDULE) - 1)]
            log.warning("[%s] Dry cycle — sleeping %d min", name, backoff // 60)
            state.emit(FleetEvent(
                type=EventType.TRADE_DRY,
                ship_symbol=ship_symbol,
                data={"dry_streak": dry_streak},
            ))
            try:
                await asyncio.wait_for(state.shutdown.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
        else:
            dry_streak = 0

    # Cleanup
    state.release_route(system, ship_symbol)
    log.info("[%s] TRADE mission stopped", name)


# ---------------------------------------------------------------------------
# SCAN adapter
# ---------------------------------------------------------------------------


async def scan_mission(
    client: SpaceTradersClient,
    ship_symbol: str,
    state: FleetState,
    *,
    max_age_min: float = 90.0,
    **kwargs: Any,
) -> None:
    """Fleet commander adapter for the probe scanner.

    Wraps _scan_worker's inner loop, reusing all the navigation and
    market caching logic from probe_scanner.
    """
    from spacetraders.api import fleet as fleet_api
    from spacetraders.missions.probe_scanner import (
        _scan_worker,
        find_marketplace_waypoints,
    )

    name = ship_name(ship_symbol)

    ship = await fleet_api.get_ship(client, ship_symbol)
    system = ship.nav.system_symbol

    # Ensure system intel is loaded
    sys_state = await load_system_intel(client, system, state)
    markets = find_marketplace_waypoints(sys_state.waypoints)

    logger.info(
        "[%s] SCAN mission started in %s (%d markets)",
        name, system, len(markets),
    )

    if not markets:
        logger.warning("[%s] No marketplaces found in %s!", name, system)
        return

    # Reuse the existing scan worker — it already handles continuous mode,
    # freshness checks, and graceful shutdown via the event
    await _scan_worker(
        client,
        ship_symbol,
        state.market_db,
        sys_state.waypoints,
        markets,
        continuous=True,
        max_age_min=max_age_min,
        shutdown=state.shutdown,
        multi_probe=True,
    )

    # Emit scan complete when the worker exits (shutdown or done)
    state.emit(FleetEvent(
        type=EventType.SCAN_COMPLETE,
        ship_symbol=ship_symbol,
    ))

    logger.info("[%s] SCAN mission stopped", name)


# ---------------------------------------------------------------------------
# CONTRACT adapter
# ---------------------------------------------------------------------------


async def contract_mission(
    client: SpaceTradersClient,
    ship_symbol: str,
    state: FleetState,
    **kwargs: Any,
) -> None:
    """Fleet commander adapter for the contract runner.

    Uses a shared ContractState from FleetState so multiple ships can
    coordinate on the same contract.
    """
    from spacetraders.api import navigation
    from spacetraders.missions.contractor import (
        ContractState,
        NavContext,
        _find_active_contract,
        _ship_loop,
    )
    from spacetraders.api import agent as agent_api, fleet as fleet_api
    from spacetraders.missions.router import build_fuel_waypoints

    name = ship_name(ship_symbol)
    hq = "X1-XV5-A1"

    ship = await fleet_api.get_ship(client, ship_symbol)
    system = ship.nav.system_symbol

    # Ensure system intel
    sys_state = await load_system_intel(client, system, state)
    coords = sys_state.coords
    fuel_wps = build_fuel_waypoints(sys_state.waypoints)
    nav = NavContext(coords=coords, fuel_waypoints=fuel_wps)

    # Share a single ContractState across all contract ships
    if state.contract_state is None:
        ag = await agent_api.get_agent(client)
        cs = ContractState(start_credits=ag.credits)
        cs.contract = await _find_active_contract(client)
        state.contract_state = cs

    logger.info("[%s] CONTRACT mission started in %s", name, system)

    emitter = _make_emitter(state)

    await _ship_loop(
        client, ship_symbol, state.contract_state, state.market_db,
        system, hq, state.shutdown, nav,
        on_event=emitter,
    )

    logger.info("[%s] CONTRACT mission stopped", name)


# ---------------------------------------------------------------------------
# GATE_BUILD adapter
# ---------------------------------------------------------------------------


async def gate_build_mission(
    client: SpaceTradersClient,
    ship_symbol: str,
    state: FleetState,
    *,
    capital_floor: int = 300_000,
    **kwargs: Any,
) -> None:
    """Fleet commander adapter for the gate builder."""
    from spacetraders.missions.gate_builder import gate_build_loop

    name = ship_name(ship_symbol)
    logger.info("[%s] GATE_BUILD mission started (floor: %s)", name, f"{capital_floor:,}")

    emitter = _make_emitter(state)

    await gate_build_loop(
        client, ship_symbol, state.market_db, state.shutdown,
        capital_floor=capital_floor,
        on_event=emitter,
    )

    logger.info("[%s] GATE_BUILD mission stopped", name)


# ---------------------------------------------------------------------------
# Register all adapters
# ---------------------------------------------------------------------------

register_mission(MissionType.TRADE, trade_mission)
register_mission(MissionType.SCAN, scan_mission)
register_mission(MissionType.CONTRACT, contract_mission)
register_mission(MissionType.GATE_BUILD, gate_build_mission)
