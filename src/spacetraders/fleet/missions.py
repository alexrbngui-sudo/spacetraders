"""Mission type registry — maps ship roles to async coroutines."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import asyncio

    from spacetraders.client import SpaceTradersClient
    from spacetraders.fleet.state import FleetState


class MissionType(str, Enum):
    """Mission types assignable to ships."""

    TRADE = "trade"
    SCAN = "scan"
    CONTRACT = "contract"
    GATE_BUILD = "gate_build"
    IDLE = "idle"


class MissionCoroutine(Protocol):
    """Protocol for mission coroutine signatures."""

    async def __call__(
        self,
        client: SpaceTradersClient,
        ship_symbol: str,
        state: FleetState,
        **kwargs: Any,
    ) -> None: ...


# Registry populated at import time by adapters in mission modules
_REGISTRY: dict[MissionType, MissionCoroutine] = {}


def register_mission(
    mission_type: MissionType,
    coroutine: MissionCoroutine,
) -> None:
    """Register a mission coroutine for a mission type."""
    _REGISTRY[mission_type] = coroutine


def get_mission_coroutine(mission_type: MissionType) -> MissionCoroutine | None:
    """Look up the coroutine for a mission type."""
    return _REGISTRY.get(mission_type)
