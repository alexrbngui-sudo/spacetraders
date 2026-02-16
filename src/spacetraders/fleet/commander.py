"""FleetCommander — event-driven orchestrator for the entire fleet.

One process, one client, one rate limiter, all ships as asyncio tasks.
The strategy engine decides assignments; events drive re-evaluation.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from spacetraders.api import agent as agent_api, contracts as contracts_api, fleet as fleet_api
from spacetraders.client import ApiError, SpaceTradersClient
from spacetraders.config import Settings, load_settings
from spacetraders.data.market_db import MarketDatabase
from spacetraders.data.operations_db import OperationsDB
from spacetraders.fleet.events import EventType, FleetEvent, STRATEGIC_EVENTS
from spacetraders.fleet.missions import MissionType
from spacetraders.fleet.scheduler import Priority, RequestScheduler
from spacetraders.fleet.ship_agent import ShipAgent
from spacetraders.fleet.state import FleetState
from spacetraders.fleet.strategy import CapitalPolicy, FleetPlan, FleetStrategy, ShipCapability
from spacetraders.fleet.system_intel import load_system_intel
from spacetraders.fleet_registry import FLEET, ShipRecord, ship_name
from spacetraders.models import Ship

logger = logging.getLogger(__name__)

# Ships that should never be commanded (known API issues)
SKIP_SHIPS: set[str] = {"UTMOSTLY-1"}  # error 3000

# Max restarts before giving up on a ship
MAX_RESTARTS = 5

# Backoff seconds by restart count: 10s, 30s, 60s, 120s, 300s
RESTART_BACKOFF = (10, 30, 60, 120, 300)

# Event loop timeout — fallback health check interval
EVENT_TIMEOUT = 30.0

# Snapshot every N event-loop cycles (~30s each)
SNAPSHOT_EVERY_N_CYCLES = 10


def _drain_queue(queue: asyncio.Queue[FleetEvent]) -> list[FleetEvent]:
    """Drain all available events from the queue without blocking."""
    events: list[FleetEvent] = []
    while True:
        try:
            events.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return events


class FleetCommander:
    """Orchestrates the entire fleet from a single process."""

    def __init__(
        self,
        settings: Settings,
        *,
        overrides: dict[str, str] | None = None,
        capital: CapitalPolicy | None = None,
    ) -> None:
        self.settings = settings
        self._overrides = overrides or {}  # ship_symbol → mission_type string
        self._scheduler = RequestScheduler()
        self._strategy = FleetStrategy(capital=capital)
        self._state: FleetState | None = None
        self._client: SpaceTradersClient | None = None

    async def run(self) -> None:
        """Main entry point — run until shutdown signal."""
        market_db = MarketDatabase(db_path=self.settings.data_dir / "markets.db")
        ops_db = OperationsDB(db_path=self.settings.data_dir / "operations.db")
        shutdown = asyncio.Event()
        state = FleetState(market_db=market_db, ops_db=ops_db, shutdown=shutdown)
        self._state = state

        # Signal handling
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, shutdown.set)

        self._scheduler.start()

        try:
            async with SpaceTradersClient(
                self.settings, scheduler=self._scheduler,
            ) as client:
                self._client = client

                # Startup
                ag = await agent_api.get_agent(client)
                logger.info("=" * 70)
                logger.info("FLEET COMMANDER ONLINE — Mission Control")
                logger.info(
                    "Agent: %s | Credits: %s | Ships: %d",
                    ag.symbol, f"{ag.credits:,}", ag.ship_count,
                )
                logger.info("=" * 70)
                ops_db.snapshot_agent(ag.credits, ag.ship_count)

                # Discover fleet from API
                ships = await self._discover_fleet(client)
                if not ships:
                    logger.error("No ships found! Exiting.")
                    return

                # Load system intel for each system our ships are in
                systems_seen: set[str] = set()
                for ship in ships:
                    sys_sym = ship.nav.system_symbol
                    if sys_sym not in systems_seen:
                        await load_system_intel(client, sys_sym, state)
                        systems_seen.add(sys_sym)

                # Gather world state for initial strategy evaluation
                plan = await self._evaluate_strategy(client, ships, state)

                # Create agents and launch based on strategy
                active_tasks = await self._apply_plan(client, ships, state, plan)

                if not active_tasks:
                    logger.warning("No active missions to run. Exiting.")
                    return

                self._log_fleet_status(state, active_tasks)

                # Event-driven loop
                await self._run_event_loop(client, state, active_tasks, shutdown)

                # Shutdown
                logger.info("")
                logger.info("Shutting down fleet...")
                shutdown.set()
                await self._cancel_all(active_tasks)

                # Final status
                ag = await agent_api.get_agent(client)
                logger.info("")
                logger.info("=" * 70)
                logger.info("FLEET COMMANDER OFFLINE")
                logger.info("Credits: %s", f"{ag.credits:,}")
                for agent in state.agents.values():
                    if agent.mission == MissionType.IDLE:
                        continue
                    status = "crashed" if agent.restart_count > MAX_RESTARTS else "stopped"
                    logger.info(
                        "  [%s] %s — %s (restarts: %d)",
                        agent.name, agent.mission.value, status, agent.restart_count,
                    )
                logger.info("=" * 70)

        except KeyboardInterrupt:
            logger.info("Fleet interrupted by user.")
        except Exception:
            logger.exception("FLEET COMMANDER CRASHED")
        finally:
            await self._scheduler.stop()
            market_db.close()
            ops_db.close()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def _discover_fleet(
        self, client: SpaceTradersClient,
    ) -> list[Ship]:
        """Fetch all ships from the API, skipping known-broken ones."""
        logger.info("Discovering fleet...")
        all_ships = await fleet_api.list_ships(client)

        ships: list[Ship] = []
        for ship in all_ships:
            if ship.symbol in SKIP_SHIPS:
                logger.info(
                    "  [%s] %s — SKIPPED (known issue)",
                    ship_name(ship.symbol), ship.symbol,
                )
                continue
            logger.info(
                "  [%s] %s at %s (%s)",
                ship_name(ship.symbol), ship.symbol,
                ship.nav.waypoint_symbol, ship.nav.status.value,
            )
            ships.append(ship)

        logger.info("Discovered %d/%d ships", len(ships), len(all_ships))
        return ships

    # ------------------------------------------------------------------
    # Strategy
    # ------------------------------------------------------------------

    async def _evaluate_strategy(
        self,
        client: SpaceTradersClient,
        ships: list[Ship],
        state: FleetState,
    ) -> FleetPlan:
        """Gather world state and ask the strategy engine for a plan."""
        ag = await agent_api.get_agent(client)

        # Check for active contract
        has_contract = False
        contract_profitable = False
        try:
            contracts = await contracts_api.list_contracts(client)
            for c in contracts:
                if c.accepted and not c.fulfilled and c.type.value == "PROCUREMENT":
                    has_contract = True
                    # Simple profitability check: payment > 0
                    total_payment = (
                        c.terms.payment.on_accepted + c.terms.payment.on_fulfilled
                    )
                    contract_profitable = total_payment > 0
                    break
        except ApiError:
            pass

        # Check gate construction status
        gate_needs = False
        try:
            from spacetraders.missions.gate_builder import check_construction
            is_complete, needs = await check_construction(client)
            gate_needs = not is_complete and len(needs) > 0
        except ApiError:
            pass

        # Check if market routes are available (quick heuristic)
        market_routes = state.market_db.has_profitable_routes()

        # Build ship capabilities
        capabilities: list[ShipCapability] = []
        current_assignments: dict[str, MissionType] = {}
        for ship in ships:
            record = FLEET.get(ship.symbol)
            category = record.category if record else "ship"
            cargo = record.cargo if record else ship.cargo.capacity
            fuel = record.fuel if record else ship.fuel.capacity

            agent = state.agents.get(ship.symbol)
            current_mission = agent.mission if agent else MissionType.IDLE

            capabilities.append(ShipCapability(
                symbol=ship.symbol,
                cargo=cargo,
                fuel=fuel,
                category=category,
                current_mission=current_mission,
            ))
            current_assignments[ship.symbol] = current_mission

        logger.info("")
        logger.info("Strategy inputs: credits=%s contract=%s gate=%s routes=%s",
                     f"{ag.credits:,}", has_contract, gate_needs, market_routes)

        plan = self._strategy.evaluate(
            credits=ag.credits,
            ships=capabilities,
            current_assignments=current_assignments,
            has_active_contract=has_contract,
            contract_profitable=contract_profitable,
            gate_needs_supplies=gate_needs,
            market_routes_available=market_routes,
            skip_ships=SKIP_SHIPS,
            overrides=self._overrides,
        )

        return plan

    async def _apply_plan(
        self,
        client: SpaceTradersClient,
        ships: list[Ship],
        state: FleetState,
        plan: FleetPlan,
    ) -> dict[str, asyncio.Task[None]]:
        """Create agents from plan and launch tasks."""
        active_tasks: dict[str, asyncio.Task[None]] = {}

        for ship in ships:
            symbol = ship.symbol
            assignment = plan.assignments.get(symbol)
            if assignment is None:
                continue

            agent = ShipAgent(
                symbol=symbol,
                mission=assignment.mission,
                system=ship.nav.system_symbol,
                mission_kwargs=assignment.kwargs,
            )
            state.agents[symbol] = agent

            task = agent.launch(client, state)
            if task is not None:
                active_tasks[symbol] = task

        return active_tasks

    # ------------------------------------------------------------------
    # Event-driven loop
    # ------------------------------------------------------------------

    async def _run_event_loop(
        self,
        client: SpaceTradersClient,
        state: FleetState,
        active_tasks: dict[str, asyncio.Task[None]],
        shutdown: asyncio.Event,
    ) -> None:
        """Wait for events and re-evaluate strategy on meaningful ones."""
        cycles = 0

        while not shutdown.is_set() and active_tasks:
            # Wait for an event or timeout
            event: FleetEvent | None = None
            try:
                event = await asyncio.wait_for(
                    state.event_queue.get(), timeout=EVENT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                pass

            cycles += 1

            # Drain any additional queued events
            batch: list[FleetEvent] = []
            if event is not None:
                batch.append(event)
            batch.extend(_drain_queue(state.event_queue))

            # Log events
            for ev in batch:
                logger.info("EVENT: %s", ev)

            # Handle crashes and completions
            for ev in batch:
                if ev.type == EventType.MISSION_CRASHED:
                    await self._handle_crash(
                        client, state, active_tasks, ev.ship_symbol, ev.data,
                    )
                elif ev.type == EventType.MISSION_ENDED:
                    self._handle_completion(state, active_tasks, ev.ship_symbol)

            # Periodic agent snapshot
            if cycles % SNAPSHOT_EVERY_N_CYCLES == 0 and state.ops_db:
                try:
                    ag = await agent_api.get_agent(client)
                    state.ops_db.snapshot_agent(ag.credits, ag.ship_count)
                except Exception:
                    pass

            # Strategy re-evaluation on meaningful events
            strategic_events = [
                ev for ev in batch if ev.type in STRATEGIC_EVENTS
            ]
            if strategic_events:
                logger.info(
                    "Re-evaluating strategy (%d strategic events)...",
                    len(strategic_events),
                )
                await self._maybe_reassign(client, state, active_tasks)

    async def _handle_crash(
        self,
        client: SpaceTradersClient,
        state: FleetState,
        active_tasks: dict[str, asyncio.Task[None]],
        ship_symbol: str,
        data: dict[str, Any],
    ) -> None:
        """Handle a crashed mission — restart with backoff or park."""
        agent = state.agents.get(ship_symbol)
        if agent is None:
            active_tasks.pop(ship_symbol, None)
            return

        logger.error(
            "[%s] Mission crashed: %s (%s)",
            agent.name, data.get("error", "unknown"),
            data.get("error_type", ""),
        )

        if agent.restart_count >= MAX_RESTARTS:
            logger.error(
                "[%s] Max restarts (%d) exceeded — parking ship",
                agent.name, MAX_RESTARTS,
            )
            agent.mission = MissionType.IDLE
            active_tasks.pop(ship_symbol, None)
            return

        # Backoff before restart
        backoff = RESTART_BACKOFF[
            min(agent.restart_count, len(RESTART_BACKOFF) - 1)
        ]
        logger.info("[%s] Restarting in %ds...", agent.name, backoff)
        await asyncio.sleep(backoff)

        if state.shutdown.is_set():
            active_tasks.pop(ship_symbol, None)
            return

        new_task = agent.relaunch(client, state)
        if new_task:
            active_tasks[ship_symbol] = new_task
        else:
            active_tasks.pop(ship_symbol, None)

    def _handle_completion(
        self,
        state: FleetState,
        active_tasks: dict[str, asyncio.Task[None]],
        ship_symbol: str,
    ) -> None:
        """Handle a mission that completed normally."""
        agent = state.agents.get(ship_symbol)
        if agent:
            logger.info(
                "[%s] Mission %s completed normally",
                agent.name, agent.mission.value,
            )
        active_tasks.pop(ship_symbol, None)

    async def _maybe_reassign(
        self,
        client: SpaceTradersClient,
        state: FleetState,
        active_tasks: dict[str, asyncio.Task[None]],
    ) -> None:
        """Re-evaluate strategy and reassign ships if needed."""
        # Rebuild ship list from current state
        ships: list[Ship] = []
        for symbol in state.agents:
            try:
                ship = await fleet_api.get_ship(client, symbol)
                ships.append(ship)
            except ApiError:
                pass

        if not ships:
            return

        plan = await self._evaluate_strategy(client, ships, state)

        # Compute changes from current assignments
        current: dict[str, MissionType] = {
            sym: agent.mission for sym, agent in state.agents.items()
        }
        changes = plan.changes_from(current)

        if not changes:
            logger.info("Strategy: no changes needed")
            return

        for symbol, (old_mission, new_assignment) in changes.items():
            logger.info(
                "Strategy: %s %s → %s",
                ship_name(symbol), old_mission.value, new_assignment.mission.value,
            )
            await self._reassign_ship(
                client, state, active_tasks, symbol, new_assignment,
            )

    async def _reassign_ship(
        self,
        client: SpaceTradersClient,
        state: FleetState,
        active_tasks: dict[str, asyncio.Task[None]],
        ship_symbol: str,
        new_assignment: Any,
    ) -> None:
        """Cancel old task, update agent, launch new task."""
        from spacetraders.fleet.strategy import ShipAssignment

        agent = state.agents.get(ship_symbol)
        if agent is None:
            return

        # Cancel existing task
        old_task = active_tasks.pop(ship_symbol, None)
        if old_task and not old_task.done():
            old_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(old_task), timeout=5.0,
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # Update agent
        agent.mission = new_assignment.mission
        agent.mission_kwargs = new_assignment.kwargs
        agent.restart_count = 0  # Reset on reassignment

        # Launch new task
        task = agent.launch(client, state)
        if task is not None:
            active_tasks[ship_symbol] = task

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _log_fleet_status(
        self,
        state: FleetState,
        active_tasks: dict[str, asyncio.Task[None]],
    ) -> None:
        """Log the current fleet assignment status."""
        logger.info("")
        logger.info("--- %d missions active ---", len(active_tasks))
        for sym, task in active_tasks.items():
            agent = state.agents[sym]
            logger.info(
                "  [%s] %s in %s",
                agent.name, agent.mission.value.upper(), agent.system,
            )
        idle_count = sum(
            1 for a in state.agents.values()
            if a.mission == MissionType.IDLE
        )
        if idle_count > 0:
            logger.info("  + %d ships IDLE", idle_count)
        logger.info("")

    async def _cancel_all(
        self, active_tasks: dict[str, asyncio.Task[None]],
    ) -> None:
        """Cancel all running tasks gracefully."""
        for task in active_tasks.values():
            task.cancel()
        if active_tasks:
            await asyncio.gather(
                *active_tasks.values(), return_exceptions=True,
            )
        active_tasks.clear()
