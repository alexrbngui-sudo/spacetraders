"""ShipAgent — per-ship task wrapper for the fleet commander."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from spacetraders.client import SpaceTradersClient
from spacetraders.fleet.events import EventType, FleetEvent
from spacetraders.fleet.missions import MissionType, get_mission_coroutine
from spacetraders.fleet.state import FleetState
from spacetraders.fleet_registry import ship_name

logger = logging.getLogger(__name__)


@dataclass
class ShipAgent:
    """Thin wrapper around a ship's mission task."""

    symbol: str
    mission: MissionType
    system: str
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    restart_count: int = 0
    mission_kwargs: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return ship_name(self.symbol)

    @property
    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()

    def launch(
        self,
        client: SpaceTradersClient,
        state: FleetState,
    ) -> asyncio.Task[None] | None:
        """Create and start the asyncio task for this agent's mission."""
        if self.mission == MissionType.IDLE:
            logger.info("[%s] Mission: IDLE — not launching", self.name)
            return None

        coroutine = get_mission_coroutine(self.mission)
        if coroutine is None:
            logger.error(
                "[%s] No coroutine registered for mission %s",
                self.name, self.mission.value,
            )
            return None

        self.task = asyncio.create_task(
            coroutine(client, self.symbol, state, **self.mission_kwargs),
            name=f"{self.mission.value}-{self.name}",
        )

        # Emit MISSION_CRASHED or MISSION_ENDED when the task finishes
        self.task.add_done_callback(self._make_done_callback(state))

        logger.info(
            "[%s] Launched %s mission (task: %s)",
            self.name, self.mission.value, self.task.get_name(),
        )
        return self.task

    def relaunch(
        self,
        client: SpaceTradersClient,
        state: FleetState,
    ) -> asyncio.Task[None] | None:
        """Restart the mission after a crash."""
        self.restart_count += 1
        logger.info(
            "[%s] Restarting %s mission (attempt %d)",
            self.name, self.mission.value, self.restart_count,
        )
        return self.launch(client, state)

    def _make_done_callback(
        self,
        state: FleetState,
    ) -> Any:
        """Create a done callback that emits fleet events."""
        symbol = self.symbol

        def _on_done(task: asyncio.Task[None]) -> None:
            if task.cancelled():
                # Cancelled by the commander (shutdown / reassignment) — no event
                return

            exc = task.exception()
            if exc is not None:
                state.emit(FleetEvent(
                    type=EventType.MISSION_CRASHED,
                    ship_symbol=symbol,
                    data={"error": str(exc), "error_type": type(exc).__name__},
                ))
            else:
                state.emit(FleetEvent(
                    type=EventType.MISSION_ENDED,
                    ship_symbol=symbol,
                ))

        return _on_done
