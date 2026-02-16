"""Fleet strategy — pure decision engine for ship assignment.

No I/O, no async.  Takes world state in, returns a FleetPlan out.
The commander calls this on startup and after every strategic event.

Priority order: Gate building > Contracts > Trading > Idle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from spacetraders.fleet.missions import MissionType
from spacetraders.fleet_registry import FLEET, ShipRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CapitalPolicy:
    """Thresholds that gate capital-intensive decisions."""

    gate_floor: int = 300_000
    trade_min: int = 50_000
    idle_threshold: int = 30_000


@dataclass(frozen=True)
class ShipAssignment:
    """A mission assignment for a single ship."""

    mission: MissionType
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class FleetPlan:
    """The output of strategy evaluation — one assignment per ship."""

    assignments: dict[str, ShipAssignment] = field(default_factory=dict)

    def changes_from(
        self,
        current: dict[str, MissionType],
    ) -> dict[str, tuple[MissionType, ShipAssignment]]:
        """Return ships whose mission changed: {symbol: (old_mission, new_assignment)}."""
        changes: dict[str, tuple[MissionType, ShipAssignment]] = {}
        for symbol, new in self.assignments.items():
            old = current.get(symbol, MissionType.IDLE)
            if new.mission != old:
                changes[symbol] = (old, new)
        return changes


@dataclass
class ShipCapability:
    """Simplified ship info for strategy decisions."""

    symbol: str
    cargo: int
    fuel: int
    category: str  # probe | ship | sentinel | disabled
    current_mission: MissionType = MissionType.IDLE


class FleetStrategy:
    """Evaluates world state and produces a FleetPlan.

    Pure logic — all inputs are passed in, no side effects.
    """

    def __init__(self, capital: CapitalPolicy | None = None) -> None:
        self.capital = capital or CapitalPolicy()

    def evaluate(
        self,
        *,
        credits: int,
        ships: list[ShipCapability],
        current_assignments: dict[str, MissionType],
        has_active_contract: bool,
        contract_profitable: bool = True,
        gate_needs_supplies: bool,
        market_routes_available: bool,
        skip_ships: set[str],
        overrides: dict[str, str] | None = None,
    ) -> FleetPlan:
        """Decide what each ship should do.

        Args:
            credits: Current agent balance.
            ships: All known ships with their capabilities.
            current_assignments: What each ship is currently doing.
            has_active_contract: Whether a profitable contract is active.
            contract_profitable: Whether the current contract is worth running.
            gate_needs_supplies: Whether the jump gate still needs materials.
            market_routes_available: Whether there are profitable trade routes.
            skip_ships: Ships to leave alone (broken, externally managed).
            overrides: Manual mission overrides from CLI (ship → mission string).

        Returns:
            FleetPlan with an assignment for every ship.
        """
        overrides = overrides or {}
        plan = FleetPlan()

        # Partition ships by category
        probes: list[ShipCapability] = []
        cargo_ships: list[ShipCapability] = []

        for ship in ships:
            if ship.symbol in skip_ships:
                plan.assignments[ship.symbol] = ShipAssignment(MissionType.IDLE)
                continue

            # Manual override takes absolute priority
            if ship.symbol in overrides:
                try:
                    mission = MissionType(overrides[ship.symbol])
                except ValueError:
                    mission = MissionType.IDLE
                plan.assignments[ship.symbol] = ShipAssignment(mission)
                continue

            if ship.category == "disabled":
                plan.assignments[ship.symbol] = ShipAssignment(MissionType.IDLE)
                continue

            if ship.category in ("sentinel",):
                # Drones/surveyors managed by drone_swarm, not commander
                plan.assignments[ship.symbol] = ShipAssignment(MissionType.IDLE)
                continue

            if ship.category == "probe":
                probes.append(ship)
            elif ship.category == "ship":
                cargo_ships.append(ship)
            else:
                plan.assignments[ship.symbol] = ShipAssignment(MissionType.IDLE)

        # Probes always scan
        for probe in probes:
            plan.assignments[probe.symbol] = ShipAssignment(MissionType.SCAN)

        # If credits are critically low, park everyone
        if credits < self.capital.idle_threshold:
            logger.warning(
                "Credits %d below idle threshold %d — parking all cargo ships",
                credits, self.capital.idle_threshold,
            )
            for ship in cargo_ships:
                plan.assignments[ship.symbol] = ShipAssignment(MissionType.IDLE)
            return plan

        # Sort cargo ships by capacity desc — assign biggest first
        cargo_ships.sort(key=lambda s: s.cargo, reverse=True)

        # Track remaining ships as we assign them
        unassigned = list(cargo_ships)

        # 1. Gate building — assign the largest hauler
        if gate_needs_supplies and credits >= self.capital.gate_floor and unassigned:
            gate_ship = unassigned.pop(0)
            plan.assignments[gate_ship.symbol] = ShipAssignment(
                MissionType.GATE_BUILD,
                kwargs={"capital_floor": self.capital.gate_floor},
            )
            logger.info(
                "Strategy: %s → GATE_BUILD (largest cargo: %d)",
                gate_ship.symbol, gate_ship.cargo,
            )

        # 2. Contracts — assign 1-2 ships if profitable
        contract_ships_assigned = 0
        max_contract_ships = 2
        if has_active_contract and contract_profitable:
            for _ in range(min(max_contract_ships, len(unassigned))):
                ship = unassigned.pop(0)
                plan.assignments[ship.symbol] = ShipAssignment(MissionType.CONTRACT)
                contract_ships_assigned += 1
                logger.info("Strategy: %s → CONTRACT", ship.symbol)

        # 3. Trading — remaining ships trade if routes exist
        if market_routes_available and credits >= self.capital.trade_min:
            still_unassigned = []
            for ship in unassigned:
                plan.assignments[ship.symbol] = ShipAssignment(MissionType.TRADE)
                logger.info("Strategy: %s → TRADE", ship.symbol)
            unassigned = still_unassigned
        else:
            reason = "low credits" if credits < self.capital.trade_min else "no routes"
            if unassigned:
                logger.info(
                    "Strategy: parking %d ships (%s)", len(unassigned), reason,
                )

        # 4. Idle — anything left
        for ship in unassigned:
            plan.assignments[ship.symbol] = ShipAssignment(MissionType.IDLE)

        return plan
