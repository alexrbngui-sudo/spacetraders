"""Jump gate construction supply runner.

Hauls FAB_MATS and ADVANCED_CIRCUITRY to the jump gate at I62 until
construction is complete. Automatically picks whichever material is
cheaper at the time. Pauses buying if credits drop below a safety floor.

Usage:
    python -m spacetraders.missions.gate_builder
    python -m spacetraders.missions.gate_builder --ship UTMOSTLY-C --floor 300000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spacetraders.api import agent as agent_api, fleet, navigation
from spacetraders.client import ApiError, SpaceTradersClient
from spacetraders.config import load_settings
from spacetraders.data.fleet_db import FleetDB
from spacetraders.data.market_db import MarketDatabase
from spacetraders.fleet_registry import ship_name
from spacetraders.missions.drone_swarm import await_transit
from spacetraders.missions.runner import (
    setup_logging,
    try_refuel,
)

logger = logging.getLogger("spacetraders.missions")

SYSTEM = "X1-XV5"
SPEED = 15  # Ion I engine speed
GATE_WAYPOINT = "X1-XV5-I62"
CAPITAL_CHECK_INTERVAL = 60  # seconds to wait when below floor

# Materials and their sources (EXPORT markets)
MATERIALS: dict[str, str] = {
    "FAB_MATS": "X1-XV5-F56",
    "ADVANCED_CIRCUITRY": "X1-XV5-D48",
}

OnEventCallback = Callable[[str, str, dict[str, Any]], None]


async def gate_navigate(
    client: SpaceTradersClient, ship_symbol: str, waypoint: str,
) -> Ship:
    """Navigate and wait for actual arrival (no 600s cap)."""
    from spacetraders.models import Ship as ShipModel
    ship = await fleet.get_ship(client, ship_symbol)
    if ship.nav.waypoint_symbol == waypoint:
        logger.info("Already at %s", waypoint)
        return ship
    if ship.nav.status.value == "DOCKED":
        await fleet.orbit(client, ship_symbol)
    nav_result = await fleet.navigate(client, ship_symbol, waypoint)
    fuel_used = nav_result.get("fuel", {}).get("consumed", {}).get("amount", "?")
    logger.info("Navigating %s → %s (%s fuel used)", ship.nav.waypoint_symbol, waypoint, fuel_used)
    ship = await await_transit(client, ship_symbol)
    return ship


@dataclass
class MaterialNeed:
    """How much of a material the gate still needs."""

    trade_symbol: str
    required: int
    fulfilled: int
    source: str

    @property
    def remaining(self) -> int:
        return self.required - self.fulfilled


async def check_construction(client: SpaceTradersClient) -> tuple[bool, list[MaterialNeed]]:
    """Check gate construction status. Returns (is_complete, needs)."""
    body = await client.get(
        f"/systems/{SYSTEM}/waypoints/{GATE_WAYPOINT}/construction"
    )
    data = body["data"]

    needs: list[MaterialNeed] = []
    for mat in data.get("materials", []):
        symbol = mat["tradeSymbol"]
        if symbol in MATERIALS and mat["required"] > mat["fulfilled"]:
            needs.append(MaterialNeed(
                trade_symbol=symbol,
                required=mat["required"],
                fulfilled=mat["fulfilled"],
                source=MATERIALS[symbol],
            ))

    return data.get("isComplete", False), needs


async def supply_construction(
    client: SpaceTradersClient,
    ship_symbol: str,
    trade_symbol: str,
    units: int,
) -> dict:
    """Deliver materials to the construction site."""
    body = await client.post(
        f"/systems/{SYSTEM}/waypoints/{GATE_WAYPOINT}/construction/supply",
        json={
            "shipSymbol": ship_symbol,
            "tradeSymbol": trade_symbol,
            "units": units,
        },
    )
    return body["data"]


async def buy_cargo(
    client: SpaceTradersClient,
    ship_symbol: str,
    good: str,
    target_units: int,
    trade_volume: int,
    capital_floor: int,
) -> tuple[int, int]:
    """Buy good in batches, respecting capital floor. Returns (units_bought, total_cost)."""
    bought = 0
    cost = 0
    while bought < target_units:
        # Check balance before each batch
        ag = await agent_api.get_agent(client)
        batch = min(trade_volume, target_units - bought)
        batch_est_cost = batch * (cost // bought if bought > 0 else 5000)
        if ag.credits - batch_est_cost < capital_floor:
            logger.info(
                "  Balance %s would drop below %s floor after buy. Stopping.",
                f"{ag.credits:,}", f"{capital_floor:,}",
            )
            break

        try:
            result = await fleet.purchase_cargo(client, ship_symbol, good, batch)
            tx = result.get("transaction", {})
            units = tx.get("units", batch)
            price = tx.get("totalPrice", 0)
            bought += units
            cost += price
            balance = result.get("agent", {}).get("credits", "?")
            logger.info(
                "  Bought %d %s @ %d/unit (%d/%d). Balance: %s",
                units, good, tx.get("pricePerUnit", 0), bought, target_units,
                f"{balance:,}" if isinstance(balance, int) else balance,
            )
        except ApiError as e:
            logger.warning("  Buy failed (%d): %s — bought %d so far", e.code, e, bought)
            break
    return bought, cost


async def get_buy_price(
    client: SpaceTradersClient,
    market_db: MarketDatabase,
    waypoint: str,
    trade_symbol: str,
) -> int | None:
    """Get buy price from market_db cache (kept fresh by probe scanners)."""
    prices = market_db.get_prices(waypoint)
    for p in prices:
        if p.trade_symbol == trade_symbol:
            return p.purchase_price
    return None


# ---------------------------------------------------------------------------
# Extracted inner loop — callable from both standalone and fleet adapter
# ---------------------------------------------------------------------------


async def gate_build_loop(
    client: SpaceTradersClient,
    ship_symbol: str,
    market_db: MarketDatabase,
    shutdown: asyncio.Event,
    capital_floor: int = 300_000,
    *,
    on_event: OnEventCallback | None = None,
) -> None:
    """Inner loop: haul materials to the jump gate until complete or shutdown.

    Args:
        on_event: Optional callback(event_type, ship_symbol, data) for fleet
                  commander integration.  Called at delivery and completion.
    """
    name = ship_name(ship_symbol)
    total_delivered: dict[str, int] = {"FAB_MATS": 0, "ADVANCED_CIRCUITRY": 0}
    total_spent = 0
    trips = 0

    while not shutdown.is_set():
        # --- Deliver any cargo on board (handles restarts mid-trip) ---
        ship = await fleet.get_ship(client, ship_symbol)
        if ship.cargo.units > 0 and ship.nav.waypoint_symbol == GATE_WAYPOINT:
            if ship.nav.status.value != "DOCKED":
                await fleet.dock(client, ship_symbol)
            for item in ship.cargo.inventory:
                if item.symbol in MATERIALS:
                    try:
                        result = await supply_construction(
                            client, ship_symbol, item.symbol, item.units,
                        )
                        construction = result.get("construction", {})
                        for m in construction.get("materials", []):
                            if m["tradeSymbol"] == item.symbol:
                                logger.info(
                                    "[%s] Delivered %d %s (restart recovery)! Progress: %d/%d",
                                    name, item.units, item.symbol, m["fulfilled"], m["required"],
                                )
                        total_delivered[item.symbol] += item.units
                        if on_event:
                            on_event("gate_delivery", ship_symbol, {
                                "material": item.symbol,
                                "units": item.units,
                            })
                        if construction.get("isComplete"):
                            logger.info("[%s] JUMP GATE CONSTRUCTION COMPLETE!", name)
                            if on_event:
                                on_event("gate_complete", ship_symbol, {})
                            return
                    except ApiError as e:
                        logger.error("[%s] Supply failed (%d): %s", name, e.code, e)

        # --- Ensure refueled before any navigation ---
        ship = await fleet.get_ship(client, ship_symbol)
        if ship.nav.status.value != "DOCKED":
            await fleet.dock(client, ship_symbol)
        ship = await try_refuel(client, ship)

        # --- Check construction progress ---
        is_complete, needs = await check_construction(client)
        if is_complete or not needs:
            logger.info("[%s] JUMP GATE CONSTRUCTION COMPLETE!", name)
            if on_event:
                on_event("gate_complete", ship_symbol, {})
            return

        for n in needs:
            logger.info(
                "[%s]   %s: %d/%d delivered (%d remaining)",
                name, n.trade_symbol, n.fulfilled, n.required, n.remaining,
            )

        # --- Check capital ---
        ag = await agent_api.get_agent(client)
        while ag.credits < capital_floor and not shutdown.is_set():
            logger.info(
                "[%s] Balance %s below %s floor. Waiting %ds...",
                name, f"{ag.credits:,}", f"{capital_floor:,}", CAPITAL_CHECK_INTERVAL,
            )
            if on_event:
                on_event("capital_low", ship_symbol, {"credits": ag.credits})
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=CAPITAL_CHECK_INTERVAL)
            except asyncio.TimeoutError:
                pass
            ag = await agent_api.get_agent(client)

        if shutdown.is_set():
            break

        # --- Pick cheapest material that's still needed ---
        best_need: MaterialNeed | None = None
        best_price: int | None = None

        for n in needs:
            price = await get_buy_price(client, market_db, n.source, n.trade_symbol)
            if price is not None:
                logger.info("[%s]   %s at %s: %d/unit", name, n.trade_symbol, n.source, price)
                if best_price is None or price < best_price:
                    best_price = price
                    best_need = n

        if best_need is None or best_price is None:
            logger.warning("[%s] Could not get prices for any needed material. Retrying in 60s.", name)
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            continue

        trips += 1
        target = best_need
        cargo_cap = ship.cargo.capacity
        load_units = min(cargo_cap, target.remaining)

        logger.info("")
        logger.info(
            "[%s] ### TRIP %d: %s — %d units from %s → %s (@ %d/unit, est %s) ###",
            name, trips, target.trade_symbol, load_units, target.source,
            GATE_WAYPOINT, best_price,
            f"{load_units * best_price:,}",
        )

        # --- Navigate to source ---
        ship = await fleet.get_ship(client, ship_symbol)
        ship = await gate_navigate(client, ship_symbol, target.source)
        if ship.nav.status.value != "DOCKED":
            await fleet.dock(client, ship_symbol)
        ship = await try_refuel(client, ship)

        # --- Get live trade volume and buy ---
        market = await navigation.get_market(client, SYSTEM, target.source)
        trade_vol = 20
        if market.trade_goods:
            for g in market.trade_goods:
                if g.symbol == target.trade_symbol:
                    trade_vol = g.trade_volume
                    best_price = g.purchase_price  # update to live price
                    break

        # Re-check capital with live price
        ag = await agent_api.get_agent(client)
        affordable = max(0, (ag.credits - capital_floor) // best_price) if best_price > 0 else 0
        load_units = min(load_units, affordable)
        if load_units <= 0:
            logger.info("[%s] Can't afford any units above capital floor. Waiting...", name)
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=CAPITAL_CHECK_INTERVAL)
            except asyncio.TimeoutError:
                pass
            continue

        units_bought, buy_cost = await buy_cargo(
            client, ship_symbol, target.trade_symbol,
            load_units, trade_vol, capital_floor,
        )

        if units_bought == 0:
            logger.warning("[%s] Couldn't buy any %s. Retrying next loop.", name, target.trade_symbol)
            continue

        total_spent += buy_cost

        # --- Navigate to gate ---
        ship = await gate_navigate(client, ship_symbol, GATE_WAYPOINT)
        if ship.nav.status.value != "DOCKED":
            await fleet.dock(client, ship_symbol)

        # --- Supply construction ---
        try:
            result = await supply_construction(
                client, ship_symbol, target.trade_symbol, units_bought,
            )
            construction = result.get("construction", {})
            materials = construction.get("materials", [])
            for m in materials:
                if m["tradeSymbol"] == target.trade_symbol:
                    logger.info(
                        "[%s] Delivered %d %s! Progress: %d/%d",
                        name, units_bought, target.trade_symbol,
                        m["fulfilled"], m["required"],
                    )
            total_delivered[target.trade_symbol] += units_bought

            if on_event:
                remaining = 0
                for m in materials:
                    if m["tradeSymbol"] == target.trade_symbol:
                        remaining = m["required"] - m["fulfilled"]
                on_event("gate_delivery", ship_symbol, {
                    "material": target.trade_symbol,
                    "units": units_bought,
                    "remaining": remaining,
                })

            if construction.get("isComplete"):
                logger.info("[%s] JUMP GATE CONSTRUCTION COMPLETE!", name)
                if on_event:
                    on_event("gate_complete", ship_symbol, {})
                return
        except ApiError as e:
            logger.error("[%s] Supply failed (%d): %s", name, e.code, e)

        # --- Refuel at gate ---
        ship = await try_refuel(client, ship)

        # --- Trip summary ---
        ag = await agent_api.get_agent(client)
        logger.info(
            "[%s] Trip %d complete. Spent: %s | Balance: %s | Total delivered: %s",
            name, trips, f"{buy_cost:,}", f"{ag.credits:,}",
            ", ".join(f"{v} {k}" for k, v in total_delivered.items() if v > 0),
        )

    logger.info("[%s] GATE_BUILD mission stopped — %d trips, %s spent", name, trips, f"{total_spent:,}")


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


async def run_gate_builder(
    ship_symbol: str,
    capital_floor: int = 300_000,
) -> None:
    """Standalone entry: setup resources, run gate_build_loop, cleanup."""
    settings = load_settings()
    market_db = MarketDatabase(db_path=settings.data_dir / "markets.db")
    fleet_db = FleetDB(db_path=settings.data_dir / "fleet.db")

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    fleet_db.release_dead()
    if not fleet_db.assign(ship_symbol, "gate"):
        logger.error("Ship %s is already assigned to another mission!", ship_symbol)
        fleet_db.close()
        market_db.close()
        return

    try:
        async with SpaceTradersClient(settings) as client:
            ag = await agent_api.get_agent(client)
            ship = await fleet.get_ship(client, ship_symbol)
            ship = await await_transit(client, ship_symbol)

            name = ship_name(ship_symbol)
            logger.info("=" * 60)
            logger.info("GATE BUILDER [%s]: %s at %s", name, ship_symbol, ship.nav.waypoint_symbol)
            logger.info("Credits: %s | Fuel: %d/%d | Cargo: %d/%d",
                        f"{ag.credits:,}", ship.fuel.current, ship.fuel.capacity,
                        ship.cargo.units, ship.cargo.capacity)
            logger.info("Capital floor: %s credits", f"{capital_floor:,}")
            logger.info("=" * 60)

            await gate_build_loop(
                client, ship_symbol, market_db, shutdown, capital_floor,
            )

            # --- Session summary ---
            logger.info("")
            logger.info("=" * 60)
            is_complete, needs = await check_construction(client)
            if is_complete:
                logger.info("  STATUS: COMPLETE!")
            else:
                for n in needs:
                    logger.info("  %s: %d/%d (%d remaining)", n.trade_symbol, n.fulfilled, n.required, n.remaining)
            ag = await agent_api.get_agent(client)
            logger.info("  Final balance: %s credits", f"{ag.credits:,}")
            logger.info("=" * 60)

    except KeyboardInterrupt:
        logger.info("Gate builder interrupted.")
    except Exception:
        logger.exception("GATE BUILDER CRASHED")
    finally:
        fleet_db.release(ship_symbol)
        fleet_db.close()
        market_db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Jump gate construction supply runner")
    parser.add_argument(
        "--ship", default=None,
        help="Ship symbol (omit to auto-pick largest cargo ship from pool)",
    )
    parser.add_argument("--floor", type=int, default=300_000, help="Minimum credit balance to maintain")
    args = parser.parse_args()

    settings = load_settings()

    ship = args.ship
    if not ship:
        fleet_db = FleetDB(db_path=settings.data_dir / "fleet.db")
        fleet_db.release_dead()
        available = fleet_db.available("ship")
        fleet_db.close()
        if not available:
            print("No ships available for gate building!")
            sys.exit(1)
        ship = available[0]  # already sorted by cargo desc
        print(f"Auto-picked {ship_name(ship)} ({ship}) for gate building")

    setup_logging(ship, log_dir=settings.data_dir / "logs")
    asyncio.run(run_gate_builder(ship, args.floor))


if __name__ == "__main__":
    main()
