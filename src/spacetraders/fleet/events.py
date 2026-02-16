"""Fleet events — typed event system for the fleet commander."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """Events that drive fleet strategy re-evaluation."""

    TRADE_COMPLETED = "trade_completed"
    TRADE_DRY = "trade_dry"
    CONTRACT_FULFILLED = "contract_fulfilled"
    CONTRACT_DELIVERY = "contract_delivery"
    GATE_DELIVERY = "gate_delivery"
    GATE_COMPLETE = "gate_complete"
    SCAN_COMPLETE = "scan_complete"
    MISSION_CRASHED = "mission_crashed"
    MISSION_ENDED = "mission_ended"
    CAPITAL_LOW = "capital_low"


# Events that should trigger strategy re-evaluation
STRATEGIC_EVENTS: frozenset[EventType] = frozenset({
    EventType.TRADE_COMPLETED,
    EventType.TRADE_DRY,
    EventType.CONTRACT_FULFILLED,
    EventType.GATE_DELIVERY,
    EventType.GATE_COMPLETE,
    EventType.MISSION_CRASHED,
    EventType.MISSION_ENDED,
    EventType.CAPITAL_LOW,
})


@dataclass(frozen=True)
class FleetEvent:
    """A single event emitted by a ship agent or the commander."""

    type: EventType
    ship_symbol: str
    timestamp: float = field(default_factory=time.monotonic)
    data: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.type.value}({self.ship_symbol})"
