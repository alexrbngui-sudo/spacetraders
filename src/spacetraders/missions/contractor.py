"""Autonomous contract runner — negotiate, buy, deliver, fulfill, repeat.

Continuously runs procurement contracts: negotiates at faction HQ,
buys goods at the cheapest market, delivers to the contract destination,
and collects payment.  Supports multiple ships on the same contract.

The game only allows ONE active contract at a time.  Multiple ships
coordinate via shared state: they buy and deliver in parallel, and
only one ship negotiates the next contract (asyncio.Lock).

Usage:
    python -m spacetraders.missions.contractor --ship UTMOSTLY-9
    python -m spacetraders.missions.contractor --ship UTMOSTLY-9 --ship UTMOSTLY-3
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spacetraders.api import agent as agent_api, contracts as contracts_api, fleet, navigation
from spacetraders.client import ApiError, SpaceTradersClient
from spacetraders.config import load_settings
from spacetraders.data.fleet_db import FleetDB
from spacetraders.data.market_db import MarketDatabase
from spacetraders.fleet_registry import FLEET, ship_name
from spacetraders.missions.router import build_fuel_waypoints, plan_multihop
from spacetraders.missions.runner import (
    navigate_multihop,
    navigate_ship,
    setup_logging,
    sleep_with_heartbeat,
    try_refuel,
    wait_for_arrival,
)
from spacetraders.models import Contract, Ship, Waypoint

logger = logging.getLogger("spacetraders.missions")

DEFAULT_HQ = "X1-XV5-A1"


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

@dataclass
class ContractState:
    """Shared state across all contractor ships."""

    contract: Contract | None = None
    negotiate_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    contracts_completed: int = 0
    total_revenue: int = 0
    total_cost: int = 0
    start_credits: int = 0

    @property
    def net_profit(self) -> int:
        return self.total_revenue - self.total_cost


@dataclass
class NavContext:
    """Shared navigation data for fuel-aware routing."""

    coords: dict[str, tuple[int, int]]
    fuel_waypoints: set[str]


def _fuel_needed(coords: dict[str, tuple[int, int]], origin: str, dest: str) -> int:
    """Estimate one-way CRUISE fuel: ceil(distance). Returns 9999 if unknown."""
    if origin == dest:
        return 0
    if origin not in coords or dest not in coords:
        return 9999
    ox, oy = coords[origin]
    dx, dy = coords[dest]
    dist = math.sqrt((dx - ox) ** 2 + (dy - oy) ** 2)
    return max(1, math.ceil(dist))


async def _smart_navigate(
    client: SpaceTradersClient,
    ship: Ship,
    destination: str,
    nav: NavContext,
    mode: str = "CRUISE",
) -> Ship:
    """Navigate to destination, using multi-hop refueling if out of direct range."""
    if ship.nav.waypoint_symbol == destination:
        return ship

    fuel_needed = _fuel_needed(nav.coords, ship.nav.waypoint_symbol, destination)

    if fuel_needed > ship.fuel.capacity and nav.fuel_waypoints:
        plan = plan_multihop(
            nav.coords, nav.fuel_waypoints, ship.nav.waypoint_symbol,
            destination, ship.fuel.capacity, ship.engine.speed, mode,
        )
        if plan.feasible and plan.num_stops > 0:
            name = ship_name(ship.symbol)
            logger.info(
                "[%s] Multi-hop to %s (%d stops, %d fuel)",
                name, destination, plan.num_stops, plan.total_fuel,
            )
            return await navigate_multihop(client, ship, plan)

    return await navigate_ship(client, ship, destination, mode)


# ---------------------------------------------------------------------------
# Contract helpers
# ---------------------------------------------------------------------------

def _remaining_deliveries(contract: Contract) -> dict[str, tuple[int, str]]:
    """Return {trade_symbol: (units_remaining, destination)} for unfinished items."""
    result: dict[str, tuple[int, str]] = {}
    for d in contract.terms.deliver or []:
        remaining = d.units_required - d.units_fulfilled
        if remaining > 0:
            result[d.trade_symbol] = (remaining, d.destination_symbol)
    return result


async def _find_active_contract(client: SpaceTradersClient) -> Contract | None:
    """Find an accepted, unfulfilled procurement contract."""
    contracts = await contracts_api.list_contracts(client)
    for c in contracts:
        if c.accepted and not c.fulfilled and c.type.value == "PROCUREMENT":
            return c
    return None


async def _evaluate_profitability(
    contract: Contract,
    market_db: MarketDatabase,
    system: str,
) -> tuple[bool, int, str]:
    """Check if buying goods to fulfill a contract is profitable.

    Returns (profitable, estimated_profit, explanation).
    """
    total_cost = 0
    details: list[str] = []

    for d in contract.terms.deliver or []:
        remaining = d.units_required - d.units_fulfilled
        if remaining <= 0:
            continue

        best = market_db.find_best_buy(d.trade_symbol, system_symbol=system)
        if not best:
            return False, 0, f"No market sells {d.trade_symbol}"

        cost = best.purchase_price * remaining
        total_cost += cost
        details.append(
            f"{remaining}× {d.trade_symbol} @ {best.purchase_price}/unit from {best.waypoint_symbol}"
        )

    total_payment = contract.terms.payment.on_accepted + contract.terms.payment.on_fulfilled
    profit = total_payment - total_cost

    explanation = (
        f"Payment: {total_payment:,} — Cost: {total_cost:,} — "
        f"Profit: {profit:,} | {', '.join(details)}"
    )
    return profit > 0, profit, explanation


async def _ensure_contract(
    client: SpaceTradersClient,
    ship_symbol: str,
    state: ContractState,
    market_db: MarketDatabase,
    system: str,
    hq: str,
    nav: NavContext,
) -> Contract | None:
    """Return the active contract, negotiating a new one if needed.

    Only one ship negotiates at a time (via state.negotiate_lock).
    """
    name = ship_name(ship_symbol)

    # Fast path: contract in shared state
    if state.contract and not state.contract.fulfilled:
        try:
            state.contract = await contracts_api.get_contract(client, state.contract.id)
            if not state.contract.fulfilled:
                return state.contract
        except ApiError:
            pass

    # Check API directly
    active = await _find_active_contract(client)
    if active:
        state.contract = active
        return active

    # Need to negotiate — one ship at a time
    async with state.negotiate_lock:
        # Double-check: another ship may have negotiated while we waited
        if state.contract and not state.contract.fulfilled:
            return state.contract
        active = await _find_active_contract(client)
        if active:
            state.contract = active
            return active

        # Navigate to HQ
        ship = await fleet.get_ship(client, ship_symbol)
        ship = await wait_for_arrival(client, ship_symbol)

        if ship.nav.waypoint_symbol != hq:
            logger.info("[%s] → %s for contract negotiation", name, hq)
            ship = await _smart_navigate(client, ship, hq, nav)

        if ship.nav.status.value != "DOCKED":
            await fleet.dock(client, ship_symbol)

        # Negotiate
        try:
            new_contract = await contracts_api.negotiate_contract(client, ship_symbol)
        except ApiError as e:
            if e.code == 4214:
                logger.info("[%s] Already have an active contract — re-checking", name)
                state.contract = await _find_active_contract(client)
                return state.contract
            logger.error("[%s] Negotiate failed: %s (code %d)", name, e, e.code)
            return None

        # Evaluate profitability
        profitable, profit, explanation = await _evaluate_profitability(
            new_contract, market_db, system,
        )
        logger.info("[%s] Offer: %s", name, explanation)

        if not profitable:
            logger.info("[%s] Unprofitable — skipping", name)
            # Can't negotiate another while this one exists; wait for expiry
            return None

        # Accept
        try:
            accepted = await contracts_api.accept_contract(client, new_contract.id)
        except ApiError as e:
            logger.error("[%s] Accept failed: %s (code %d)", name, e, e.code)
            return None

        advance = accepted.terms.payment.on_accepted
        state.contract = accepted
        state.total_revenue += advance
        logger.info(
            "[%s] ACCEPTED %s | +%d advance | est. profit %d",
            name, accepted.id, advance, profit,
        )
        for d in accepted.terms.deliver or []:
            logger.info(
                "[%s]   %d %s → %s", name,
                d.units_required, d.trade_symbol, d.destination_symbol,
            )
        return accepted


# ---------------------------------------------------------------------------
# Buy / deliver helpers
# ---------------------------------------------------------------------------

async def _buy_goods(
    client: SpaceTradersClient,
    ship: Ship,
    trade_symbol: str,
    units: int,
    market_db: MarketDatabase,
    system: str,
) -> tuple[Ship, int, int]:
    """Buy goods at the current market.  Returns (ship, bought, total_cost)."""
    name = ship_name(ship.symbol)

    if ship.nav.status.value != "DOCKED":
        await fleet.dock(client, ship.symbol)

    # Refresh market data while we're here and get trade_volume
    trade_volume = 60  # safe default
    try:
        market = await navigation.get_market(client, system, ship.nav.waypoint_symbol)
        if market.trade_goods:
            market_db.update_market(ship.nav.waypoint_symbol, market.trade_goods)
            for g in market.trade_goods:
                if g.symbol == trade_symbol:
                    trade_volume = g.trade_volume
                    break
    except ApiError:
        pass

    ship = await fleet.get_ship(client, ship.symbol)
    total_bought = 0
    total_cost = 0

    while total_bought < units:
        space = ship.cargo.capacity - ship.cargo.units
        batch = min(units - total_bought, space, trade_volume)
        if batch <= 0:
            break

        try:
            result = await fleet.purchase_cargo(client, ship.symbol, trade_symbol, batch)
            tx = result.get("transaction", {})
            bought = tx.get("units", batch)
            cost = tx.get("totalPrice", 0)
            total_bought += bought
            total_cost += cost
            ship = await fleet.get_ship(client, ship.symbol)
            logger.info(
                "[%s] Bought %d %s for %d cr (%d/%d)",
                name, bought, trade_symbol, cost, total_bought, units,
            )
        except ApiError as e:
            logger.error("[%s] Buy failed: %s (code %d)", name, e, e.code)
            break

    return ship, total_bought, total_cost


async def _deliver_cargo(
    client: SpaceTradersClient,
    ship: Ship,
    contract: Contract,
    trade_symbol: str,
) -> tuple[Ship, int]:
    """Deliver contract goods from cargo.  Returns (ship, units_delivered)."""
    name = ship_name(ship.symbol)

    if ship.nav.status.value != "DOCKED":
        await fleet.dock(client, ship.symbol)

    in_cargo = sum(i.units for i in ship.cargo.inventory if i.symbol == trade_symbol)
    if in_cargo == 0:
        return ship, 0

    # Clamp to what the contract still needs
    for d in contract.terms.deliver or []:
        if d.trade_symbol == trade_symbol:
            remaining = d.units_required - d.units_fulfilled
            in_cargo = min(in_cargo, remaining)
            break

    if in_cargo <= 0:
        return ship, 0

    try:
        await contracts_api.deliver_contract(
            client, contract.id, ship.symbol, trade_symbol, in_cargo,
        )
        logger.info("[%s] Delivered %d %s", name, in_cargo, trade_symbol)
        ship = await fleet.get_ship(client, ship.symbol)
        return ship, in_cargo
    except ApiError as e:
        logger.error("[%s] Deliver failed: %s (code %d)", name, e, e.code)
        return await fleet.get_ship(client, ship.symbol), 0


# ---------------------------------------------------------------------------
# Per-ship loop
# ---------------------------------------------------------------------------

OnEventCallback = Callable[[str, str, dict[str, Any]], None]


async def _ship_loop(
    client: SpaceTradersClient,
    ship_symbol: str,
    state: ContractState,
    market_db: MarketDatabase,
    system: str,
    hq: str,
    shutdown: asyncio.Event,
    nav: NavContext,
    *,
    on_event: OnEventCallback | None = None,
) -> None:
    """Main loop for one ship running contracts.

    Args:
        on_event: Optional callback(event_type, ship_symbol, data) for fleet
                  commander integration.  Called at fulfillment, delivery, and
                  when no contract is available.
    """
    name = ship_name(ship_symbol)

    while not shutdown.is_set():
        try:
            # ── 1. Ensure active contract ──────────────────────────
            contract = await _ensure_contract(
                client, ship_symbol, state, market_db, system, hq, nav,
            )
            if not contract:
                logger.info("[%s] No contract available — retrying in 5 min", name)
                if on_event:
                    on_event("trade_dry", ship_symbol, {"reason": "no_contract"})
                await sleep_with_heartbeat(300, f"{name} waiting for contract")
                continue

            # ── 2. Check what still needs delivering ───────────────
            contract = await contracts_api.get_contract(client, contract.id)
            remaining_map = _remaining_deliveries(contract)

            if not remaining_map:
                # All delivered — fulfill
                if not contract.fulfilled:
                    try:
                        contract = await contracts_api.fulfill_contract(client, contract.id)
                        payment = contract.terms.payment.on_fulfilled
                        state.total_revenue += payment
                        state.contracts_completed += 1
                        ag = await agent_api.get_agent(client)
                        logger.info(
                            "[%s] ✓ CONTRACT FULFILLED +%d cr | Profit: %d | Balance: %s",
                            name, payment, state.net_profit, f"{ag.credits:,}",
                        )
                        if on_event:
                            on_event("contract_fulfilled", ship_symbol, {
                                "contract_id": contract.id,
                                "payment": payment,
                                "credits": ag.credits,
                            })
                    except ApiError as e:
                        logger.error("[%s] Fulfill failed: %s", name, e)
                state.contract = None
                continue

            # ── 3. Pick first unfinished delivery ──────────────────
            trade_symbol, (remaining, deliver_wp) = next(iter(remaining_map.items()))

            # ── 4. Best buy market ─────────────────────────────────
            best_buy = market_db.find_best_buy(trade_symbol, system_symbol=system)
            if not best_buy:
                logger.error("[%s] No market sells %s — can't fulfill", name, trade_symbol)
                await sleep_with_heartbeat(300, f"{name} no source for {trade_symbol}")
                continue

            buy_wp = best_buy.waypoint_symbol
            ship = await fleet.get_ship(client, ship_symbol)
            ship = await wait_for_arrival(client, ship_symbol)

            # ── 5. Deliver existing cargo first ────────────────────
            existing = sum(i.units for i in ship.cargo.inventory if i.symbol == trade_symbol)
            if existing > 0:
                logger.info("[%s] Have %d %s in cargo — delivering first", name, existing, trade_symbol)
                if ship.nav.waypoint_symbol != deliver_wp:
                    ship = await _smart_navigate(client, ship, deliver_wp, nav)
                contract = await contracts_api.get_contract(client, contract.id)
                ship, _ = await _deliver_cargo(client, ship, contract, trade_symbol)
                ship = await try_refuel(client, ship)
                continue  # re-check remaining

            # ── 6. Buy ─────────────────────────────────────────────
            to_buy = min(remaining, ship.cargo.capacity - ship.cargo.units)
            logger.info(
                "[%s] Buy %d %s at %s (%d still needed)",
                name, to_buy, trade_symbol, buy_wp, remaining,
            )

            if ship.nav.waypoint_symbol != buy_wp:
                ship = await _smart_navigate(client, ship, buy_wp, nav)

            ship, bought, cost = await _buy_goods(
                client, ship, trade_symbol, to_buy, market_db, system,
            )
            state.total_cost += cost

            if bought == 0:
                logger.warning("[%s] Couldn't buy any %s — retrying in 2 min", name, trade_symbol)
                await asyncio.sleep(120)
                continue

            # ── 7. Refuel before delivery leg ──────────────────────
            ship = await try_refuel(client, ship)

            # ── 8. Deliver ─────────────────────────────────────────
            logger.info("[%s] → %s to deliver %d %s", name, deliver_wp, bought, trade_symbol)
            ship = await _smart_navigate(client, ship, deliver_wp, nav)

            contract = await contracts_api.get_contract(client, contract.id)
            ship, delivered = await _deliver_cargo(client, ship, contract, trade_symbol)

            if delivered > 0 and on_event:
                on_event("contract_delivery", ship_symbol, {
                    "contract_id": contract.id,
                    "trade_symbol": trade_symbol,
                    "units": delivered,
                })

            # ── 9. Refuel at delivery point ────────────────────────
            ship = await try_refuel(client, ship)

            # ── 10. Log progress ───────────────────────────────────
            contract = await contracts_api.get_contract(client, contract.id)
            state.contract = contract
            for d in contract.terms.deliver or []:
                if d.trade_symbol == trade_symbol:
                    logger.info(
                        "[%s] Progress: %d/%d %s",
                        name, d.units_fulfilled, d.units_required, trade_symbol,
                    )

        except ApiError as e:
            logger.error("[%s] API error: %s (code %d) — retrying in 60s", name, e, e.code)
            await asyncio.sleep(60)
        except Exception:
            logger.exception("[%s] Unexpected error — retrying in 60s", name)
            await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _pick_contractor_ships(
    fleet_db: FleetDB,
    contract: Contract | None,
    manual_ships: list[str] | None = None,
) -> list[str]:
    """Pick ships for contracting from the pool.

    If manual_ships provided, uses those (manual override).
    Otherwise, auto-picks from available 'ship' category based on contract needs.
    """
    if manual_ships:
        return manual_ships

    if not contract or contract.fulfilled:
        # No contract yet — grab 2 ships as a reasonable default
        available = fleet_db.available("ship")
        return available[:2] if len(available) >= 2 else available

    # Calculate how many ships we need: ceil(units_needed / best_cargo)
    total_units = 0
    for d in contract.terms.deliver or []:
        remaining = d.units_required - d.units_fulfilled
        if remaining > 0:
            total_units += remaining

    available = fleet_db.available("ship")
    if not available:
        return []

    # Use the largest-cargo ship as the reference
    best_cargo = max(FLEET[s].cargo for s in available) if available else 40
    ships_needed = math.ceil(total_units / best_cargo) if best_cargo > 0 else 1
    ships_needed = max(1, min(ships_needed, len(available)))

    return available[:ships_needed]


async def main_async(
    ship_symbols: list[str] | None,
    hq: str,
) -> None:
    """Run contract missions for all ships."""
    settings = load_settings()
    market_db = MarketDatabase(db_path=settings.data_dir / "markets.db")
    fleet_db = FleetDB(db_path=settings.data_dir / "fleet.db")

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    assigned_ships: list[str] = []

    try:
        async with SpaceTradersClient(settings) as client:
            ag = await agent_api.get_agent(client)
            system = "X1-XV5"

            fleet_db.release_dead()

            # Load waypoint data for fuel-aware navigation
            waypoints = await navigation.list_waypoints(client, system)
            coords = {wp.symbol: (wp.x, wp.y) for wp in waypoints}
            fuel_wps = build_fuel_waypoints(waypoints)
            nav = NavContext(coords=coords, fuel_waypoints=fuel_wps)

            state = ContractState(start_credits=ag.credits)
            state.contract = await _find_active_contract(client)

            # Auto-pick or use manual ships
            manual = ship_symbols if ship_symbols else None
            picked = _pick_contractor_ships(fleet_db, state.contract, manual)
            if not picked:
                logger.error("No ships available for contracting!")
                return

            # Assign picked ships
            for sym in picked:
                if fleet_db.assign(sym, "contract"):
                    assigned_ships.append(sym)
                else:
                    logger.warning("Ship %s unavailable — skipping", sym)

            if not assigned_ships:
                logger.error("Could not assign any ships!")
                return

            logger.info("=" * 50)
            logger.info("CONTRACT RUNNER")
            logger.info("Agent: %s | Credits: %s", ag.symbol, f"{ag.credits:,}")
            logger.info(
                "Ships: %s",
                ", ".join(f"{ship_name(s)} ({s})" for s in assigned_ships),
            )
            logger.info("HQ: %s", hq)
            if state.contract:
                logger.info("Active contract: %s", state.contract.id)
                for d in state.contract.terms.deliver or []:
                    logger.info(
                        "  %s: %d/%d → %s",
                        d.trade_symbol, d.units_fulfilled, d.units_required,
                        d.destination_symbol,
                    )
                _, profit, explanation = await _evaluate_profitability(
                    state.contract, market_db, system,
                )
                logger.info("  %s", explanation)
            else:
                logger.info("No active contract — will negotiate on first cycle")
            logger.info("=" * 50)

            # Launch one task per ship
            tasks = [
                asyncio.create_task(
                    _ship_loop(client, sym, state, market_db, system, hq, shutdown, nav),
                    name=f"contractor-{ship_name(sym)}",
                )
                for sym in assigned_ships
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)
            for sym, result in zip(assigned_ships, results):
                if isinstance(result, Exception):
                    logger.error("[%s] Crashed: %s", ship_name(sym), result)

            # Final report
            ag = await agent_api.get_agent(client)
            logger.info("")
            logger.info("=" * 50)
            logger.info("CONTRACT RUNNER STOPPED")
            logger.info("  Contracts completed: %d", state.contracts_completed)
            logger.info("  Revenue: %d cr", state.total_revenue)
            logger.info("  Costs:   %d cr", state.total_cost)
            logger.info("  Net profit: %d cr", state.net_profit)
            logger.info("  Balance: %s", f"{ag.credits:,}")
            logger.info("=" * 50)

    finally:
        for sym in assigned_ships:
            fleet_db.release(sym)
        fleet_db.close()
        market_db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="SpaceTraders contract runner")
    parser.add_argument(
        "--ship", action="append", default=None,
        help="Ship symbol(s) to use (repeat for multiple). Omit to auto-pick from pool.",
    )
    parser.add_argument(
        "--hq", default=DEFAULT_HQ,
        help=f"Faction HQ for negotiation (default: {DEFAULT_HQ})",
    )
    args = parser.parse_args()

    settings = load_settings()
    setup_logging("contractor", log_dir=settings.data_dir / "logs")
    asyncio.run(main_async(args.ship, args.hq))


if __name__ == "__main__":
    main()
