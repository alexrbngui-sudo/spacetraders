"""System intelligence — load and cache waypoint data for a system."""

from __future__ import annotations

import logging

from spacetraders.api import navigation
from spacetraders.client import SpaceTradersClient
from spacetraders.fleet.state import FleetState, SystemState

logger = logging.getLogger(__name__)


async def load_system_intel(
    client: SpaceTradersClient,
    system_symbol: str,
    state: FleetState,
) -> SystemState:
    """Load waypoints for a system and cache in fleet state.

    Returns the SystemState (existing or newly created).
    """
    existing = state.get_system(system_symbol)
    if existing and existing.waypoints:
        return existing

    logger.info("Loading system intel for %s...", system_symbol)
    waypoints = await navigation.list_waypoints(client, system_symbol)
    sys_state = state.ensure_system(system_symbol, waypoints)

    logger.info(
        "System %s: %d waypoints, %d markets, %d shipyards",
        system_symbol,
        len(waypoints),
        len(sys_state.markets),
        len(sys_state.shipyards),
    )
    return sys_state
