"""Deploy ships as permanent market/shipyard sentinels.

Waits for each ship to finish any current transit, then navigates
to its assigned post and docks. Once docked, the ship sits there
permanently — providing live market data and shipyard access.

Usage:
    python -m spacetraders.missions.deploy_sentinels
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from spacetraders.client import ApiError, SpaceTradersClient
from spacetraders.config import load_settings
from spacetraders.api import agent as agent_api, fleet
from spacetraders.data.fleet_db import FleetDB
from spacetraders.fleet_registry import ship_name
from spacetraders.missions.runner import navigate_ship, setup_logging, sleep_with_heartbeat

logger = logging.getLogger("spacetraders.sentinels")

# Ship → target waypoint assignments
ASSIGNMENTS: dict[str, str] = {
    "UTMOSTLY-D": "X1-XV5-A1",    # Surveyor → A1 (faction HQ, contract negotiation)
    "UTMOSTLY-6": "X1-XV5-B7",    # Drone 2 → B7 (best market, 18 commodities)
    "UTMOSTLY-8": "X1-XV5-H59",   # Drone 4 → H59 (shipyard: drones/surveyors)
    "UTMOSTLY-5": "X1-XV5-E52",   # Drone 1 → E52 (FERTILIZERS contract delivery)
    "UTMOSTLY-7": "X1-XV5-C45",   # Drone 3 → C45 (distant shipyard: siphon drones)
}


async def wait_for_arrival(client: SpaceTradersClient, ship_symbol: str) -> None:
    """Wait for a ship to finish any current transit."""
    name = ship_name(ship_symbol)
    ship = await fleet.get_ship(client, ship_symbol)

    while ship.nav.status.value == "IN_TRANSIT":
        now = datetime.now(timezone.utc)
        arrival = ship.nav.route.arrival
        remaining = (arrival - now).total_seconds()

        if remaining > 0:
            dest = ship.nav.route.destination.symbol
            logger.info("[%s] In transit → %s, %.0f min remaining", name, dest, remaining / 60)
            await sleep_with_heartbeat(remaining + 2, f"{name} transit → {dest}")

        ship = await fleet.get_ship(client, ship_symbol)

        polls = 0
        while ship.nav.status.value == "IN_TRANSIT" and polls < 5:
            polls += 1
            await asyncio.sleep(10)
            ship = await fleet.get_ship(client, ship_symbol)


async def deploy_one(client: SpaceTradersClient, ship_symbol: str, target: str) -> None:
    """Deploy a single ship to its sentinel post."""
    name = ship_name(ship_symbol)

    # Wait for any current transit
    await wait_for_arrival(client, ship_symbol)
    ship = await fleet.get_ship(client, ship_symbol)

    current = ship.nav.waypoint_symbol
    if current == target:
        logger.info("[%s] Already at %s", name, target)
    else:
        # Navigate — use DRIFT since drones have limited fuel and these are one-way trips
        from spacetraders.missions.router import distance, fuel_cost, usable_fuel
        coords_current = (ship.nav.route.destination.x, ship.nav.route.destination.y)

        dist_info = ""
        try:
            from spacetraders.api import navigation
            system = ship.nav.system_symbol
            # Just navigate — let navigate_ship pick the mode
            ship = await navigate_ship(client, ship, target, "DRIFT")
            await wait_for_arrival(client, ship_symbol)
            ship = await fleet.get_ship(client, ship_symbol)
        except ApiError as e:
            logger.error("[%s] Navigate failed: %s (code %d)", name, e, e.code)
            return

    # Dock at the post
    if ship.nav.status.value != "DOCKED":
        try:
            await fleet.dock(client, ship_symbol)
        except ApiError:
            # Might need to orbit first if in weird state
            await fleet.orbit(client, ship_symbol)
            await fleet.dock(client, ship_symbol)

    logger.info("[%s] STATIONED at %s ✓", name, target)


async def main_async() -> None:
    """Deploy all sentinel ships."""
    settings = load_settings()
    fleet_db = FleetDB(db_path=settings.data_dir / "fleet.db")

    try:
        async with SpaceTradersClient(settings) as client:
            ag = await agent_api.get_agent(client)
            logger.info("=" * 50)
            logger.info("DEPLOYING SENTINELS")
            logger.info("Agent: %s | Credits: %s", ag.symbol, f"{ag.credits:,}")
            logger.info("=" * 50)

            for ship_sym, target in ASSIGNMENTS.items():
                logger.info("  %s (%s) → %s", ship_name(ship_sym), ship_sym, target)

            # Mark all sentinel ships as assigned
            fleet_db.release_dead()
            for ship_sym in ASSIGNMENTS:
                fleet_db.assign(ship_sym, "sentinel")

            logger.info("")

            # Deploy all concurrently
            tasks = [
                asyncio.create_task(
                    deploy_one(client, ship_sym, target),
                    name=f"deploy-{ship_name(ship_sym)}",
                )
                for ship_sym, target in ASSIGNMENTS.items()
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for task_result, (ship_sym, target) in zip(results, ASSIGNMENTS.items()):
                if isinstance(task_result, Exception):
                    logger.error(
                        "[%s] Deploy failed: %s", ship_name(ship_sym), task_result,
                    )

            logger.info("")
            logger.info("=" * 50)
            logger.info("DEPLOYMENT COMPLETE")

            # Final status
            for ship_sym, target in ASSIGNMENTS.items():
                try:
                    s = await fleet.get_ship(client, ship_sym)
                    logger.info(
                        "  %s: %s at %s (fuel %d/%d)",
                        ship_name(ship_sym), s.nav.status.value,
                        s.nav.waypoint_symbol, s.fuel.current, s.fuel.capacity,
                    )
                except ApiError:
                    logger.info("  %s: unreachable", ship_name(ship_sym))

            logger.info("=" * 50)
    finally:
        # Sentinel assignments persist — don't release them
        fleet_db.close()


def main() -> None:
    settings = load_settings()
    setup_logging("sentinels", log_dir=settings.data_dir / "logs")
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
