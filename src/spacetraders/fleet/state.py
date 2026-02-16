"""Shared fleet state — the single source of truth for the commander."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from spacetraders.data.market_db import MarketDatabase
from spacetraders.data.operations_db import OperationsDB
from spacetraders.fleet.events import FleetEvent
from spacetraders.models import Waypoint

if TYPE_CHECKING:
    from spacetraders.fleet.ship_agent import ShipAgent
    from spacetraders.missions.contractor import ContractState


@dataclass
class SystemState:
    """Per-system knowledge cache."""

    symbol: str
    waypoints: list[Waypoint] = field(default_factory=list)
    coords: dict[str, tuple[int, int]] = field(default_factory=dict)
    markets: list[Waypoint] = field(default_factory=list)
    shipyards: list[Waypoint] = field(default_factory=list)
    # ship_symbol → (good, source, dest) for route collision avoidance
    claimed_routes: dict[str, tuple[str, str, str]] = field(default_factory=dict)

    @classmethod
    def from_waypoints(cls, system_symbol: str, waypoints: list[Waypoint]) -> SystemState:
        """Build system state from a list of waypoints."""
        return cls(
            symbol=system_symbol,
            waypoints=waypoints,
            coords={wp.symbol: (wp.x, wp.y) for wp in waypoints},
            markets=[
                wp for wp in waypoints
                if any(t.symbol == "MARKETPLACE" for t in wp.traits)
            ],
            shipyards=[
                wp for wp in waypoints
                if any(t.symbol == "SHIPYARD" for t in wp.traits)
            ],
        )


@dataclass
class FleetState:
    """Global fleet state shared by all ship agents."""

    market_db: MarketDatabase
    ops_db: OperationsDB | None = None
    shutdown: asyncio.Event = field(default_factory=asyncio.Event)

    # Event queue for strategy re-evaluation
    event_queue: asyncio.Queue[FleetEvent] = field(default_factory=asyncio.Queue)

    # Shared contract state (used by CONTRACT missions)
    contract_state: ContractState | None = None

    # Per-system state keyed by system symbol
    systems: dict[str, SystemState] = field(default_factory=dict)

    # All active ship agents keyed by ship symbol
    agents: dict[str, ShipAgent] = field(default_factory=dict)

    def get_system(self, system_symbol: str) -> SystemState | None:
        """Get cached system state, or None if not yet loaded."""
        return self.systems.get(system_symbol)

    def ensure_system(self, system_symbol: str, waypoints: list[Waypoint]) -> SystemState:
        """Get or create system state from waypoints."""
        if system_symbol not in self.systems:
            self.systems[system_symbol] = SystemState.from_waypoints(
                system_symbol, waypoints,
            )
        return self.systems[system_symbol]

    def get_coords(self, system_symbol: str) -> dict[str, tuple[int, int]]:
        """Get waypoint coordinate lookup for a system."""
        sys_state = self.systems.get(system_symbol)
        return sys_state.coords if sys_state else {}

    def claim_route(
        self,
        system_symbol: str,
        ship_symbol: str,
        good: str,
        source: str,
        destination: str,
    ) -> None:
        """Register a trade route claim for a ship in a system."""
        sys_state = self.systems.get(system_symbol)
        if sys_state:
            sys_state.claimed_routes[ship_symbol] = (good, source, destination)
        # Also persist to market_db for backward compat with standalone scripts
        self.market_db.claim_route(ship_symbol, good, source, destination)

    def release_route(self, system_symbol: str, ship_symbol: str) -> None:
        """Release a ship's route claim."""
        sys_state = self.systems.get(system_symbol)
        if sys_state:
            sys_state.claimed_routes.pop(ship_symbol, None)
        self.market_db.release_route(ship_symbol)

    def get_excluded_routes(
        self, system_symbol: str, exclude_ship: str,
    ) -> list[tuple[str, str, str]]:
        """Get all claimed routes in a system, excluding one ship."""
        sys_state = self.systems.get(system_symbol)
        if not sys_state:
            return []
        return [
            route for ship, route in sys_state.claimed_routes.items()
            if ship != exclude_ship
        ]

    def emit(self, event: FleetEvent) -> None:
        """Push an event onto the queue for the commander's event loop."""
        self.event_queue.put_nowait(event)
