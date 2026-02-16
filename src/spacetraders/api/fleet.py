"""Fleet (ships) API operations."""

from __future__ import annotations

from typing import Any

from spacetraders.client import SpaceTradersClient
from spacetraders.models import Cooldown, Ship, ShipCargo, ShipNav


async def list_ships(client: SpaceTradersClient) -> list[Ship]:
    """Fetch all ships in the fleet."""
    items, _ = await client.get_paginated("/my/ships")
    return [Ship.model_validate(s) for s in items]


async def get_ship(client: SpaceTradersClient, ship_symbol: str) -> Ship:
    """Fetch a single ship's details."""
    body = await client.get(f"/my/ships/{ship_symbol}")
    return Ship.model_validate(body["data"])


async def orbit(client: SpaceTradersClient, ship_symbol: str) -> ShipNav:
    """Move ship into orbit."""
    body = await client.post(f"/my/ships/{ship_symbol}/orbit")
    return ShipNav.model_validate(body["data"]["nav"])


async def dock(client: SpaceTradersClient, ship_symbol: str) -> ShipNav:
    """Dock ship at current waypoint."""
    body = await client.post(f"/my/ships/{ship_symbol}/dock")
    return ShipNav.model_validate(body["data"]["nav"])


async def navigate(
    client: SpaceTradersClient, ship_symbol: str, waypoint_symbol: str
) -> dict[str, Any]:
    """Navigate ship to a waypoint. Returns full response with nav + fuel."""
    body = await client.post(
        f"/my/ships/{ship_symbol}/navigate",
        json={"waypointSymbol": waypoint_symbol},
    )
    return body["data"]


async def refuel(
    client: SpaceTradersClient, ship_symbol: str, *, from_cargo: bool = False,
) -> dict[str, Any]:
    """Refuel ship at current waypoint (must be docked).

    Args:
        from_cargo: If True, use FUEL cargo instead of buying from market.
    """
    payload: dict[str, Any] = {}
    if from_cargo:
        payload["fromCargo"] = True
    body = await client.post(
        f"/my/ships/{ship_symbol}/refuel", json=payload or None,
    )
    return body["data"]


async def get_cargo(client: SpaceTradersClient, ship_symbol: str) -> ShipCargo:
    """Fetch ship's cargo."""
    body = await client.get(f"/my/ships/{ship_symbol}/cargo")
    return ShipCargo.model_validate(body["data"])


async def purchase_cargo(
    client: SpaceTradersClient, ship_symbol: str, trade_symbol: str, units: int
) -> dict[str, Any]:
    """Buy goods at the current market."""
    body = await client.post(
        f"/my/ships/{ship_symbol}/purchase",
        json={"symbol": trade_symbol, "units": units},
    )
    return body["data"]


async def sell_cargo(
    client: SpaceTradersClient, ship_symbol: str, trade_symbol: str, units: int
) -> dict[str, Any]:
    """Sell goods at the current market."""
    body = await client.post(
        f"/my/ships/{ship_symbol}/sell",
        json={"symbol": trade_symbol, "units": units},
    )
    return body["data"]


async def jettison_cargo(
    client: SpaceTradersClient, ship_symbol: str, trade_symbol: str, units: int
) -> ShipCargo:
    """Jettison cargo from ship."""
    body = await client.post(
        f"/my/ships/{ship_symbol}/jettison",
        json={"symbol": trade_symbol, "units": units},
    )
    return ShipCargo.model_validate(body["data"]["cargo"])


async def set_flight_mode(
    client: SpaceTradersClient, ship_symbol: str, flight_mode: str
) -> str:
    """Set ship flight mode (CRUISE, DRIFT, BURN, STEALTH). Returns the new mode."""
    await client.patch(
        f"/my/ships/{ship_symbol}/nav",
        json={"flightMode": flight_mode},
    )
    return flight_mode


async def transfer_cargo(
    client: SpaceTradersClient,
    from_ship: str,
    to_ship: str,
    trade_symbol: str,
    units: int,
) -> dict[str, Any]:
    """Transfer cargo between ships at the same waypoint."""
    body = await client.post(
        f"/my/ships/{from_ship}/transfer",
        json={"tradeSymbol": trade_symbol, "units": units, "shipSymbol": to_ship},
    )
    return body["data"]


async def purchase_ship(
    client: SpaceTradersClient, ship_type: str, waypoint_symbol: str
) -> dict[str, Any]:
    """Purchase a ship at a shipyard. Returns agent, ship, and transaction data."""
    body = await client.post(
        "/my/ships",
        json={"shipType": ship_type, "waypointSymbol": waypoint_symbol},
    )
    return body["data"]


async def get_cooldown(
    client: SpaceTradersClient, ship_symbol: str
) -> Cooldown | None:
    """Get ship cooldown status. Returns None if no active cooldown."""
    try:
        body = await client.get(f"/my/ships/{ship_symbol}/cooldown")
        if not body or "data" not in body:
            return None
        return Cooldown.model_validate(body["data"])
    except Exception:
        return None
