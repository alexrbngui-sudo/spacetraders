"""One-shot operational commands: contract collection, negotiation, ship purchases.

Usage:
    python -m spacetraders.missions.ops fulfill          # Collect fulfilled contract payment
    python -m spacetraders.missions.ops negotiate SHIP   # Negotiate new contract (ship must be at HQ)
    python -m spacetraders.missions.ops buy-ship TYPE WP # Buy a ship (e.g. SHIP_SURVEYOR X1-XV5-H59)
    python -m spacetraders.missions.ops status           # Quick fleet + contract overview
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from spacetraders.client import ApiError, SpaceTradersClient
from spacetraders.config import load_settings
from spacetraders.api import agent as agent_api, contracts as contracts_api, fleet
from spacetraders.data.fleet_db import FleetDB
from spacetraders.fleet_registry import FLEET, ship_name
from spacetraders.models import Contract

logger = logging.getLogger("spacetraders.ops")


def _format_contract(c: Contract) -> str:
    """Format a contract for display."""
    lines = [
        f"  Contract: {c.id}",
        f"  Type: {c.type} | Faction: {c.faction_symbol}",
        f"  Accepted: {c.accepted} | Fulfilled: {c.fulfilled}",
    ]
    if c.terms:
        pay = c.terms.payment
        lines.append(f"  Payment: {pay.on_accepted:,} on accept + {pay.on_fulfilled:,} on fulfill")
        for d in c.terms.deliver or []:
            lines.append(
                f"  Deliver: {d.units_fulfilled}/{d.units_required} {d.trade_symbol} → {d.destination_symbol}"
            )
        if c.terms.deadline:
            lines.append(f"  Deadline: {c.terms.deadline}")
    return "\n".join(lines)


async def cmd_fulfill(client: SpaceTradersClient) -> None:
    """Find and fulfill any completed-but-uncollected contracts."""
    contracts = await contracts_api.list_contracts(client)
    fulfilled_any = False

    for c in contracts:
        if c.accepted and not c.fulfilled:
            # Check if all deliveries are complete
            all_done = all(
                d.units_fulfilled >= d.units_required
                for d in (c.terms.deliver or [])
            )
            if all_done:
                logger.info("Fulfilling contract %s...", c.id)
                try:
                    result = await contracts_api.fulfill_contract(client, c.id)
                    payment = c.terms.payment.on_fulfilled if c.terms else 0
                    logger.info(
                        "Contract fulfilled! Payment: +%s credits",
                        f"{payment:,}",
                    )
                    fulfilled_any = True
                except ApiError as e:
                    logger.error("Failed to fulfill %s: %s (code %d)", c.id, e, e.code)
            else:
                logger.info("Contract %s not yet complete:", c.id)
                for d in c.terms.deliver or []:
                    logger.info(
                        "  %s: %d/%d",
                        d.trade_symbol, d.units_fulfilled, d.units_required,
                    )

    if not fulfilled_any:
        logger.info("No contracts ready to fulfill.")

    ag = await agent_api.get_agent(client)
    logger.info("Current credits: %s", f"{ag.credits:,}")


async def cmd_negotiate(client: SpaceTradersClient, ship_symbol: str) -> None:
    """Negotiate a new contract with a ship docked at faction HQ."""
    ship = await fleet.get_ship(client, ship_symbol)

    # Ensure docked
    if ship.nav.status.value != "DOCKED":
        logger.info("Docking %s...", ship_name(ship_symbol))
        await fleet.dock(client, ship_symbol)

    logger.info(
        "Negotiating contract with %s at %s...",
        ship_name(ship_symbol), ship.nav.waypoint_symbol,
    )

    try:
        contract = await contracts_api.negotiate_contract(client, ship_symbol)
        logger.info("New contract negotiated!")
        logger.info(_format_contract(contract))
    except ApiError as e:
        if e.code == 4214:
            logger.error("Ship must be at a faction headquarters to negotiate.")
        else:
            logger.error("Negotiate failed: %s (code %d)", e, e.code)


async def cmd_buy_ship(
    client: SpaceTradersClient, ship_type: str, waypoint_symbol: str,
) -> None:
    """Buy a ship at a shipyard."""
    ag = await agent_api.get_agent(client)
    logger.info("Current credits: %s", f"{ag.credits:,}")

    logger.info("Purchasing %s at %s...", ship_type, waypoint_symbol)
    try:
        result = await fleet.purchase_ship(client, ship_type, waypoint_symbol)
        ship_data = result.get("ship", {})
        transaction = result.get("transaction", {})
        new_symbol = ship_data.get("symbol", "?")
        price = transaction.get("price", 0)
        agent_data = result.get("agent", {})
        new_credits = agent_data.get("credits", "?")

        logger.info("Purchased %s for %s credits!", new_symbol, f"{price:,}")
        logger.info("New balance: %s credits", f"{new_credits:,}" if isinstance(new_credits, int) else new_credits)
        logger.info(
            "Add to fleet_registry.py:\n"
            '    "%s": ShipRecord(\n'
            '        symbol="%s",\n'
            '        name="Surveyor",\n'
            '        role="Surveyor",\n'
            "        cargo=0, fuel=0,\n"
            '        notes="Purchased %s at %s.",\n'
            "    ),",
            new_symbol, new_symbol,
            "2026-02-16", waypoint_symbol,
        )
    except ApiError as e:
        if e.code == 4601:
            logger.error("Insufficient credits to purchase ship.")
        elif e.code == 4602:
            logger.error("Ship type not available at this shipyard.")
        else:
            logger.error("Purchase failed: %s (code %d)", e, e.code)


async def cmd_status(client: SpaceTradersClient) -> None:
    """Quick fleet and contract status overview."""
    settings = load_settings()
    fleet_db = FleetDB(db_path=settings.data_dir / "fleet.db")

    ag = await agent_api.get_agent(client)
    logger.info("=" * 50)
    logger.info("Agent: %s | Credits: %s", ag.symbol, f"{ag.credits:,}")
    logger.info("=" * 50)

    # Contracts
    contracts = await contracts_api.list_contracts(client)
    active = [c for c in contracts if c.accepted and not c.fulfilled]
    if active:
        logger.info("\nActive contracts:")
        for c in active:
            logger.info(_format_contract(c))
    else:
        logger.info("\nNo active contracts.")

    # Fleet — query known ships individually to avoid list_ships pagination bug
    fleet_db.release_dead()
    all_assignments = fleet_db.assignments()

    logger.info("\nFleet:")
    for symbol, rec in FLEET.items():
        assignment_info = ""
        if symbol in all_assignments:
            mission, pid = all_assignments[symbol]
            assignment_info = f" | {mission} (pid {pid})"
        elif rec.category == "disabled":
            assignment_info = " | disabled"
        else:
            assignment_info = " | idle"

        try:
            s = await fleet.get_ship(client, symbol)
            status = s.nav.status.value
            location = s.nav.waypoint_symbol
            fuel_str = f"fuel {s.fuel.current}/{s.fuel.capacity}" if s.fuel.capacity > 0 else "solar"
            cargo_str = f"cargo {s.cargo.units}/{s.cargo.capacity}" if s.cargo.capacity > 0 else ""

            parts = [fuel_str]
            if cargo_str:
                parts.append(cargo_str)
            detail = " ".join(parts)

            logger.info(
                "  %-12s (%s): %-10s at %-14s | %s%s",
                rec.name, symbol, status, location, detail, assignment_info,
            )
        except ApiError as e:
            if e.code == 3000:
                logger.info(
                    "  %-12s (%s): UNREACHABLE%s",
                    rec.name, symbol, assignment_info,
                )
            else:
                logger.info(
                    "  %-12s (%s): ERROR %d%s",
                    rec.name, symbol, e.code, assignment_info,
                )

    logger.info("=" * 50)
    fleet_db.close()


async def run(args: argparse.Namespace) -> None:
    """Execute the requested operation."""
    settings = load_settings()
    async with SpaceTradersClient(settings) as client:
        if args.command == "fulfill":
            await cmd_fulfill(client)
        elif args.command == "negotiate":
            await cmd_negotiate(client, args.ship)
        elif args.command == "buy-ship":
            await cmd_buy_ship(client, args.ship_type, args.waypoint)
        elif args.command == "status":
            await cmd_status(client)


def main() -> None:
    parser = argparse.ArgumentParser(description="SpaceTraders operational commands")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("fulfill", help="Collect payment for fulfilled contracts")

    neg = sub.add_parser("negotiate", help="Negotiate a new contract")
    neg.add_argument("ship", help="Ship symbol (must be docked at faction HQ)")

    buy = sub.add_parser("buy-ship", help="Purchase a ship at a shipyard")
    buy.add_argument("ship_type", help="Ship type (e.g. SHIP_SURVEYOR)")
    buy.add_argument("waypoint", help="Shipyard waypoint (e.g. X1-XV5-H59)")

    sub.add_parser("status", help="Fleet and contract overview")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
