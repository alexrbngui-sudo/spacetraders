"""Pydantic models for SpaceTraders API responses."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# --- Enums ---


class ShipNavStatus(str, Enum):
    DOCKED = "DOCKED"
    IN_ORBIT = "IN_ORBIT"
    IN_TRANSIT = "IN_TRANSIT"


class FlightMode(str, Enum):
    CRUISE = "CRUISE"
    DRIFT = "DRIFT"
    BURN = "BURN"
    STEALTH = "STEALTH"


class ContractType(str, Enum):
    PROCUREMENT = "PROCUREMENT"
    TRANSPORT = "TRANSPORT"
    SHUTTLE = "SHUTTLE"


class WaypointType(str, Enum):
    PLANET = "PLANET"
    GAS_GIANT = "GAS_GIANT"
    MOON = "MOON"
    ORBITAL_STATION = "ORBITAL_STATION"
    JUMP_GATE = "JUMP_GATE"
    ASTEROID_FIELD = "ASTEROID_FIELD"
    ASTEROID = "ASTEROID"
    ENGINEERED_ASTEROID = "ENGINEERED_ASTEROID"
    ASTEROID_BASE = "ASTEROID_BASE"
    NEBULA = "NEBULA"
    DEBRIS_FIELD = "DEBRIS_FIELD"
    GRAVITY_WELL = "GRAVITY_WELL"
    ARTIFICIAL_GRAVITY_WELL = "ARTIFICIAL_GRAVITY_WELL"
    FUEL_STATION = "FUEL_STATION"


# --- Response envelope ---


class Meta(BaseModel):
    total: int
    page: int
    limit: int


class ApiResponse(BaseModel, Generic[T]):
    """Single-item API response wrapper."""

    data: T


class PaginatedResponse(BaseModel, Generic[T]):
    """Paginated list API response wrapper."""

    data: list[T]
    meta: Meta


# --- Agent ---


class Agent(BaseModel):
    account_id: str = Field(alias="accountId")
    symbol: str
    headquarters: str
    credits: int
    starting_faction: str = Field(alias="startingFaction")
    ship_count: int = Field(alias="shipCount")


# --- Ship sub-models ---


class ShipRegistration(BaseModel):
    name: str
    faction_symbol: str = Field(alias="factionSymbol")
    role: str


class RouteWaypoint(BaseModel):
    symbol: str
    type: str
    system_symbol: str = Field(alias="systemSymbol")
    x: int
    y: int


class ShipRoute(BaseModel):
    destination: RouteWaypoint
    origin: RouteWaypoint
    departure_time: datetime = Field(alias="departureTime")
    arrival: datetime


class ShipNav(BaseModel):
    system_symbol: str = Field(alias="systemSymbol")
    waypoint_symbol: str = Field(alias="waypointSymbol")
    route: ShipRoute
    status: ShipNavStatus
    flight_mode: FlightMode = Field(alias="flightMode")


class ShipCrew(BaseModel):
    current: int
    required: int
    capacity: int
    rotation: str
    morale: int
    wages: int


class Requirements(BaseModel):
    power: int = 0
    crew: int = 0
    slots: int = 0


class ShipFrame(BaseModel):
    symbol: str
    name: str
    condition: float = 1.0
    integrity: float = 1.0
    description: str = ""
    module_slots: int = Field(0, alias="moduleSlots")
    mounting_points: int = Field(0, alias="mountingPoints")
    fuel_capacity: int = Field(0, alias="fuelCapacity")
    requirements: Requirements = Field(default_factory=Requirements)
    quality: float = 1.0


class ShipReactor(BaseModel):
    symbol: str
    name: str
    condition: float = 1.0
    integrity: float = 1.0
    description: str = ""
    power_output: int = Field(0, alias="powerOutput")
    requirements: Requirements = Field(default_factory=Requirements)
    quality: float = 1.0


class ShipEngine(BaseModel):
    symbol: str
    name: str
    condition: float = 1.0
    integrity: float = 1.0
    description: str = ""
    speed: int = 0
    requirements: Requirements = Field(default_factory=Requirements)
    quality: float = 1.0


class ShipModule(BaseModel):
    symbol: str
    name: str
    description: str = ""
    capacity: int | None = None
    requirements: Requirements = Field(default_factory=Requirements)


class ShipMount(BaseModel):
    symbol: str
    name: str
    description: str = ""
    strength: int | None = None
    deposits: list[str] | None = None
    requirements: Requirements = Field(default_factory=Requirements)


class CargoItem(BaseModel):
    symbol: str
    name: str
    description: str
    units: int


class ShipCargo(BaseModel):
    capacity: int
    units: int
    inventory: list[CargoItem] = Field(default_factory=list)


class FuelConsumed(BaseModel):
    amount: int
    timestamp: datetime


class ShipFuel(BaseModel):
    current: int
    capacity: int
    consumed: FuelConsumed | None = None


class Cooldown(BaseModel):
    ship_symbol: str = Field(alias="shipSymbol")
    total_seconds: int = Field(alias="totalSeconds")
    remaining_seconds: int = Field(alias="remainingSeconds")
    expiration: datetime | None = None


class Ship(BaseModel):
    symbol: str
    registration: ShipRegistration
    nav: ShipNav
    crew: ShipCrew
    frame: ShipFrame
    reactor: ShipReactor
    engine: ShipEngine
    modules: list[ShipModule] = Field(default_factory=list)
    mounts: list[ShipMount] = Field(default_factory=list)
    cargo: ShipCargo
    fuel: ShipFuel
    cooldown: Cooldown


# --- Contracts ---


class ContractDelivery(BaseModel):
    trade_symbol: str = Field(alias="tradeSymbol")
    destination_symbol: str = Field(alias="destinationSymbol")
    units_required: int = Field(alias="unitsRequired")
    units_fulfilled: int = Field(alias="unitsFulfilled")


class ContractPayment(BaseModel):
    on_accepted: int = Field(alias="onAccepted")
    on_fulfilled: int = Field(alias="onFulfilled")


class ContractTerms(BaseModel):
    deadline: datetime
    payment: ContractPayment
    deliver: list[ContractDelivery] = Field(default_factory=list)


class Contract(BaseModel):
    id: str
    faction_symbol: str = Field(alias="factionSymbol")
    type: ContractType
    terms: ContractTerms
    accepted: bool
    fulfilled: bool
    expiration: datetime
    deadline_to_accept: datetime = Field(alias="deadlineToAccept")


# --- Waypoints & Systems ---


class WaypointTrait(BaseModel):
    symbol: str
    name: str
    description: str


class WaypointOrbital(BaseModel):
    symbol: str


class WaypointFaction(BaseModel):
    symbol: str


class WaypointChart(BaseModel):
    waypoint_symbol: str | None = Field(None, alias="waypointSymbol")
    submitted_by: str | None = Field(None, alias="submittedBy")
    submitted_on: datetime | None = Field(None, alias="submittedOn")


class Waypoint(BaseModel):
    symbol: str
    type: WaypointType
    system_symbol: str = Field(alias="systemSymbol")
    x: int
    y: int
    orbits: str | None = None
    orbitals: list[WaypointOrbital] = Field(default_factory=list)
    traits: list[WaypointTrait] = Field(default_factory=list)
    faction: WaypointFaction | None = None
    is_under_construction: bool = Field(False, alias="isUnderConstruction")
    modifiers: list[dict] = Field(default_factory=list)
    chart: WaypointChart | None = None


class System(BaseModel):
    symbol: str
    sector_symbol: str = Field(alias="sectorSymbol")
    type: str
    x: int
    y: int
    waypoints: list[dict] = Field(default_factory=list)
    factions: list[dict] = Field(default_factory=list)


# --- Market ---


class TradeGood(BaseModel):
    symbol: str
    name: str
    description: str


class MarketTransaction(BaseModel):
    waypoint_symbol: str = Field(alias="waypointSymbol")
    ship_symbol: str = Field(alias="shipSymbol")
    trade_symbol: str = Field(alias="tradeSymbol")
    type: str
    units: int
    price_per_unit: int = Field(alias="pricePerUnit")
    total_price: int = Field(alias="totalPrice")
    timestamp: datetime


class MarketTradeGood(BaseModel):
    symbol: str
    type: str
    trade_volume: int = Field(alias="tradeVolume")
    supply: str
    activity: str | None = None
    purchase_price: int = Field(alias="purchasePrice")
    sell_price: int = Field(alias="sellPrice")


class Market(BaseModel):
    symbol: str
    exports: list[TradeGood] = Field(default_factory=list)
    imports: list[TradeGood] = Field(default_factory=list)
    exchange: list[TradeGood] = Field(default_factory=list)
    transactions: list[MarketTransaction] = Field(default_factory=list)
    trade_goods: list[MarketTradeGood] = Field(default_factory=list, alias="tradeGoods")


# --- Survey ---


class SurveyDeposit(BaseModel):
    symbol: str


class Survey(BaseModel):
    signature: str
    symbol: str
    deposits: list[SurveyDeposit]
    expiration: datetime
    size: str


# --- Extraction ---


class ExtractionYield(BaseModel):
    symbol: str
    units: int


class Extraction(BaseModel):
    ship_symbol: str = Field(alias="shipSymbol")
    yield_: ExtractionYield = Field(alias="yield")


# --- Shipyard ---


class ShipyardShipType(BaseModel):
    type: str


class ShipyardTransaction(BaseModel):
    waypoint_symbol: str = Field(alias="waypointSymbol")
    ship_symbol: str = Field(alias="shipSymbol")
    ship_type: str = Field(alias="shipType")
    price: int
    agent_symbol: str = Field(alias="agentSymbol")
    timestamp: datetime


class ShipyardShip(BaseModel):
    type: str | None = None
    name: str
    description: str
    supply: str = ""
    activity: str | None = None
    purchase_price: int = Field(0, alias="purchasePrice")
    frame: ShipFrame
    reactor: ShipReactor
    engine: ShipEngine
    modules: list[ShipModule] = Field(default_factory=list)
    mounts: list[ShipMount] = Field(default_factory=list)
    crew: dict = Field(default_factory=dict)


class Shipyard(BaseModel):
    symbol: str
    ship_types: list[ShipyardShipType] = Field(default_factory=list, alias="shipTypes")
    transactions: list[ShipyardTransaction] = Field(default_factory=list)
    ships: list[ShipyardShip] = Field(default_factory=list)
    modifications_fee: int = Field(0, alias="modificationsFee")


# --- Utilities ---


def system_symbol_from_waypoint(waypoint_symbol: str) -> str:
    """Extract system symbol from a waypoint symbol (e.g. 'X1-XV5-H58' → 'X1-XV5')."""
    parts = waypoint_symbol.split("-")
    if len(parts) < 3:
        raise ValueError(
            f"Invalid waypoint symbol '{waypoint_symbol}': expected format like 'X1-XV5-H58'",
        )
    return f"{parts[0]}-{parts[1]}"
