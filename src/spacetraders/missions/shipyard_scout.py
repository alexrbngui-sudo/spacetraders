"""Shipyard scout — sends a probe to catalog all shipyards in the system.

Visits every SHIPYARD waypoint, docks, queries available ships, and writes
a summary report. Useful after game resets or when planning fleet expansion.

Usage:
    python -m spacetraders.missions.shipyard_scout --ship UTMOSTLY-4
    python -m spacetraders.missions.shipyard_scout --ship UTMOSTLY-4 --output data/shipyards.md

Output: Markdown report with ship types, prices, supply levels per shipyard.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from spacetraders.api import fleet, navigation
from spacetraders.client import ApiError, SpaceTradersClient
from spacetraders.config import load_settings
from spacetraders.fleet_registry import ship_name
from spacetraders.missions.probe_scanner import (
    find_marketplace_waypoints,
    plan_scan_route,
    wait_for_probe_arrival,
)
from spacetraders.missions.router import waypoint_distance
from spacetraders.models import Shipyard, ShipyardShip, Waypoint

logger = logging.getLogger("spacetraders.missions")

SYSTEM = "X1-XV5"


def find_shipyard_waypoints(waypoints: list[Waypoint]) -> list[Waypoint]:
    """Filter waypoints that have a SHIPYARD trait."""
    return [
        wp for wp in waypoints
        if any(t.symbol == "SHIPYARD" for t in wp.traits)
    ]


def format_report(
    results: list[tuple[Waypoint, Shipyard]],
    scan_time: str,
) -> str:
    """Format shipyard scan results as a Markdown report."""
    lines = [
        "# Shipyard Catalog — X1-XV5",
        "",
        f"Scanned: {scan_time}",
        "",
    ]

    if not results:
        lines.append("No shipyards found or accessible.")
        return "\n".join(lines)

    # Summary table
    lines.append("## Summary")
    lines.append("")
    lines.append("| Shipyard | Type | Ships Available |")
    lines.append("|----------|------|-----------------|")
    for wp, yard in results:
        wp_type = wp.type.value if wp.type else "?"
        ship_count = len(yard.ships) if yard.ships else len(yard.ship_types)
        lines.append(f"| {wp.symbol} | {wp_type} | {ship_count} |")
    lines.append("")

    # Detail per shipyard
    for wp, yard in results:
        lines.append(f"## {wp.symbol}")
        lines.append("")
        coords = f"({wp.x}, {wp.y})"
        wp_type = wp.type.value if wp.type else "?"
        lines.append(f"**Type:** {wp_type} | **Coords:** {coords}")
        if yard.modifications_fee:
            lines.append(f"**Modification fee:** {yard.modifications_fee:,} credits")
        lines.append("")

        if yard.ships:
            lines.append("| Ship | Type | Price | Supply | Activity |")
            lines.append("|------|------|------:|--------|----------|")
            for ship in sorted(yard.ships, key=lambda s: s.purchase_price):
                activity = ship.activity or "—"
                lines.append(
                    f"| {ship.name} | {ship.type or '?'} "
                    f"| {ship.purchase_price:,} | {ship.supply} | {activity} |"
                )
        elif yard.ship_types:
            lines.append("*Ship details require a ship docked at this shipyard.*")
            lines.append("")
            lines.append("**Types available:** " + ", ".join(
                st.type for st in yard.ship_types
            ))
        else:
            lines.append("*No ships listed.*")

        lines.append("")

    return "\n".join(lines)


async def run_shipyard_scout(
    ship_symbol: str,
    output_path: Path | None = None,
) -> None:
    """Send a probe to every shipyard, catalog available ships."""
    settings = load_settings()

    if output_path is None:
        output_path = settings.data_dir / "shipyards.md"

    name = ship_name(ship_symbol)

    try:
        async with SpaceTradersClient(settings) as client:
            waypoints = await navigation.list_waypoints(client, SYSTEM)
            shipyards = find_shipyard_waypoints(waypoints)

            logger.info("=" * 60)
            logger.info("SHIPYARD SCOUT [%s]: %s in system %s", name, ship_symbol, SYSTEM)
            logger.info("Found %d shipyards to visit", len(shipyards))
            logger.info("=" * 60)

            if not shipyards:
                logger.error("No shipyards found in %s!", SYSTEM)
                return

            # Get current ship state
            ship = await fleet.get_ship(client, ship_symbol)
            if ship.nav.status.value == "IN_TRANSIT":
                ship = await wait_for_probe_arrival(client, ship_symbol)

            # Plan route from current position
            ship_wp = next(
                (wp for wp in waypoints if wp.symbol == ship.nav.waypoint_symbol),
                None,
            )
            if not ship_wp:
                logger.error("Can't find ship waypoint %s", ship.nav.waypoint_symbol)
                return

            route = plan_scan_route(ship_wp, shipyards)
            total_dist = sum(
                waypoint_distance(route[i], route[i + 1])
                for i in range(len(route) - 1)
            )
            logger.info("Route: %d stops, ~%.0f total distance", len(route), total_dist)

            # Visit each shipyard
            results: list[tuple[Waypoint, Shipyard]] = []
            for i, waypoint in enumerate(route, 1):
                logger.info("")
                logger.info("### SHIPYARD %d/%d: %s ###", i, len(route), waypoint.symbol)

                ship = await fleet.get_ship(client, ship_symbol)

                # Wait if in transit
                if ship.nav.status.value == "IN_TRANSIT":
                    ship = await wait_for_probe_arrival(client, ship_symbol)

                # Navigate if needed
                if ship.nav.waypoint_symbol != waypoint.symbol:
                    if ship.nav.status.value == "DOCKED":
                        await fleet.orbit(client, ship_symbol)

                    if ship.nav.flight_mode.value != "DRIFT":
                        await fleet.set_flight_mode(client, ship_symbol, "DRIFT")

                    await fleet.navigate(client, ship_symbol, waypoint.symbol)
                    ship = await wait_for_probe_arrival(client, ship_symbol)

                    if ship.nav.waypoint_symbol != waypoint.symbol:
                        logger.warning("  Failed to arrive at %s", waypoint.symbol)
                        continue

                # Dock and query shipyard
                if ship.nav.status.value != "DOCKED":
                    await fleet.dock(client, ship_symbol)

                try:
                    yard = await navigation.get_shipyard(client, SYSTEM, waypoint.symbol)
                    results.append((waypoint, yard))

                    if yard.ships:
                        logger.info("  %d ships available:", len(yard.ships))
                        for s in sorted(yard.ships, key=lambda s: s.purchase_price):
                            logger.info(
                                "    %-30s %-20s %8s credits  supply=%s",
                                s.name, s.type or "?",
                                f"{s.purchase_price:,}", s.supply,
                            )
                    elif yard.ship_types:
                        logger.info(
                            "  Types listed (no details — need docked ship): %s",
                            ", ".join(st.type for st in yard.ship_types),
                        )
                    else:
                        logger.info("  No ships listed.")

                except ApiError as e:
                    logger.warning(
                        "  Shipyard query failed at %s (%d): %s",
                        waypoint.symbol, e.code, e,
                    )

            # Write report
            scan_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            report = format_report(results, scan_time)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(report)

            logger.info("")
            logger.info("=" * 60)
            logger.info(
                "SCOUT COMPLETE: %d/%d shipyards cataloged",
                len(results), len(shipyards),
            )
            logger.info("Report written to %s", output_path)
            logger.info("=" * 60)

    except KeyboardInterrupt:
        logger.info("Scout interrupted.")
    except Exception:
        logger.exception("SHIPYARD SCOUT CRASHED")


def setup_logging(ship_symbol: str, log_dir: Path | None = None) -> None:
    """Configure logging for the scout."""
    if log_dir is None:
        log_dir = Path("data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"shipyard_scout_{ship_symbol.lower()}.log"
    formatter = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Shipyard scout — catalog all shipyards")
    parser.add_argument("--ship", default="UTMOSTLY-4", help="Probe to use")
    parser.add_argument("--output", type=Path, default=None, help="Output report path")
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(args.ship, log_dir=settings.data_dir / "logs")
    asyncio.run(run_shipyard_scout(args.ship, args.output))


if __name__ == "__main__":
    main()
