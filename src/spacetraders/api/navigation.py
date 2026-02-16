"""Navigation and system API operations."""

from __future__ import annotations

from spacetraders.client import SpaceTradersClient
from spacetraders.models import Market, Shipyard, System, Waypoint


async def get_system(client: SpaceTradersClient, system_symbol: str) -> System:
    """Fetch system details."""
    body = await client.get(f"/systems/{system_symbol}")
    return System.model_validate(body["data"])


async def list_waypoints(
    client: SpaceTradersClient, system_symbol: str
) -> list[Waypoint]:
    """Fetch all waypoints in a system."""
    items, _ = await client.get_paginated(f"/systems/{system_symbol}/waypoints")
    return [Waypoint.model_validate(w) for w in items]


async def get_waypoint(
    client: SpaceTradersClient, system_symbol: str, waypoint_symbol: str
) -> Waypoint:
    """Fetch a single waypoint."""
    body = await client.get(f"/systems/{system_symbol}/waypoints/{waypoint_symbol}")
    return Waypoint.model_validate(body["data"])


async def get_market(
    client: SpaceTradersClient, system_symbol: str, waypoint_symbol: str
) -> Market:
    """Fetch market data at a waypoint."""
    body = await client.get(
        f"/systems/{system_symbol}/waypoints/{waypoint_symbol}/market"
    )
    return Market.model_validate(body["data"])


async def get_shipyard(
    client: SpaceTradersClient, system_symbol: str, waypoint_symbol: str
) -> Shipyard:
    """Fetch shipyard data at a waypoint."""
    body = await client.get(
        f"/systems/{system_symbol}/waypoints/{waypoint_symbol}/shipyard"
    )
    return Shipyard.model_validate(body["data"])
