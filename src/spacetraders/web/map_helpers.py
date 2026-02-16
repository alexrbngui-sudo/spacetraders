"""Layout computation for the SVG system map."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from spacetraders.models import Ship, ShipNavStatus, Waypoint, WaypointType


@dataclass
class DeliveryInfo:
    """Active contract delivery requirement at a waypoint."""

    contract_id: str
    trade_symbol: str
    units_required: int
    units_fulfilled: int


@dataclass
class WaypointPosition:
    """Computed position and metadata for a waypoint on the map."""

    symbol: str
    short_label: str
    wp_type: WaypointType
    x: float
    y: float
    has_market: bool
    has_shipyard: bool
    has_ship: bool
    is_destination: bool
    is_hq: bool
    is_delivery: bool
    trait_names: list[str]
    orbits: str | None
    ships_here: list[str] = field(default_factory=list)
    inbound_ships: list[str] = field(default_factory=list)
    deliveries: list[DeliveryInfo] = field(default_factory=list)


@dataclass
class ShipPosition:
    """Computed position for a ship on the map."""

    symbol: str
    short_label: str
    x: float
    y: float
    status: ShipNavStatus
    role: str
    waypoint_symbol: str
    destination_symbol: str | None = None


@dataclass
class TransitLine:
    """Dashed line from origin to destination for in-transit ships."""

    ship_symbol: str
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class OrbitalLine:
    """Thin line connecting a moon/station to its parent body."""

    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class MapLayout:
    """Everything the SVG template needs to render the system map."""

    view_box: str
    scale_unit: float
    waypoints: list[WaypointPosition] = field(default_factory=list)
    ships: list[ShipPosition] = field(default_factory=list)
    transit_lines: list[TransitLine] = field(default_factory=list)
    orbital_lines: list[OrbitalLine] = field(default_factory=list)


def _short_label(symbol: str) -> str:
    """Extract abbreviated label from waypoint symbol (e.g., 'X1-XV5-A1' → 'A1')."""
    parts = symbol.split("-")
    return parts[-1] if len(parts) >= 3 else symbol


def _ship_short_label(symbol: str) -> str:
    """Extract ship number (e.g., 'UTMOSTLY-1' → '#1')."""
    parts = symbol.split("-")
    return f"#{parts[-1]}" if len(parts) >= 2 else symbol


def compute_map_layout(
    waypoints: list[Waypoint],
    ships: list[Ship],
    *,
    hq_symbol: str | None = None,
    delivery_waypoints: dict[str, list[DeliveryInfo]] | None = None,
) -> MapLayout:
    """Compute positions and viewBox for rendering the system map SVG."""
    if not waypoints:
        return MapLayout(view_box="0 0 100 100", scale_unit=1.0)

    # Build lookup of waypoint positions by symbol
    wp_coords: dict[str, tuple[int, int]] = {wp.symbol: (wp.x, wp.y) for wp in waypoints}

    # Compute coordinate bounds
    xs = [wp.x for wp in waypoints]
    ys = [wp.y for wp in waypoints]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    # Ensure minimum range so single-point or clustered maps don't collapse
    range_x = max(max_x - min_x, 100)
    range_y = max(max_y - min_y, 100)

    # Add 15% padding
    pad_x = range_x * 0.15
    pad_y = range_y * 0.15
    vb_x = min_x - pad_x
    vb_y = min_y - pad_y
    vb_w = range_x + 2 * pad_x
    vb_h = range_y + 2 * pad_y

    scale_unit = max(range_x, range_y) / 80

    view_box = f"{vb_x:.1f} {vb_y:.1f} {vb_w:.1f} {vb_h:.1f}"

    # Group orbitals by parent to compute angular offsets
    orbitals_by_parent: dict[str, list[Waypoint]] = {}
    for wp in waypoints:
        if wp.orbits:
            orbitals_by_parent.setdefault(wp.orbits, []).append(wp)

    # Pre-compute which waypoints have ships or are destinations
    ship_waypoints: set[str] = set()
    destination_waypoints: set[str] = set()
    ships_at: dict[str, list[str]] = {}
    inbound_to: dict[str, list[str]] = {}
    for ship in ships:
        nav = ship.nav
        if nav.status == ShipNavStatus.IN_TRANSIT:
            dest_sym = nav.route.destination.symbol
            destination_waypoints.add(dest_sym)
            inbound_to.setdefault(dest_sym, []).append(ship.symbol)
        else:
            ship_waypoints.add(nav.waypoint_symbol)
            ships_at.setdefault(nav.waypoint_symbol, []).append(ship.symbol)

    # Compute waypoint positions with orbital offsets
    orbital_radius = scale_unit * 4
    wp_positions: list[WaypointPosition] = []
    adjusted_coords: dict[str, tuple[float, float]] = {}

    for wp in waypoints:
        x: float = wp.x
        y: float = wp.y

        if wp.orbits and wp.orbits in wp_coords:
            # This is an orbital — offset from parent in a ring
            siblings = orbitals_by_parent.get(wp.orbits, [])
            idx = next(
                (i for i, s in enumerate(siblings) if s.symbol == wp.symbol), 0
            )
            n = len(siblings)
            angle = (2 * math.pi * idx / n) - math.pi / 2  # start at top
            parent_x, parent_y = wp_coords[wp.orbits]
            x = parent_x + orbital_radius * math.cos(angle)
            y = parent_y + orbital_radius * math.sin(angle)

        adjusted_coords[wp.symbol] = (x, y)

        has_market = any(t.symbol == "MARKETPLACE" for t in wp.traits)
        has_shipyard = any(t.symbol == "SHIPYARD" for t in wp.traits)
        trait_names = [t.name for t in wp.traits]
        wp_deliveries = (delivery_waypoints or {}).get(wp.symbol, [])

        wp_positions.append(WaypointPosition(
            symbol=wp.symbol,
            short_label=_short_label(wp.symbol),
            wp_type=wp.type,
            x=x,
            y=y,
            has_market=has_market,
            has_shipyard=has_shipyard,
            has_ship=wp.symbol in ship_waypoints,
            is_destination=wp.symbol in destination_waypoints,
            is_hq=wp.symbol == hq_symbol,
            is_delivery=bool(wp_deliveries),
            trait_names=trait_names,
            orbits=wp.orbits,
            ships_here=ships_at.get(wp.symbol, []),
            inbound_ships=inbound_to.get(wp.symbol, []),
            deliveries=wp_deliveries,
        ))

    # Orbital connection lines
    orbital_lines: list[OrbitalLine] = []
    for wp_pos in wp_positions:
        if wp_pos.orbits and wp_pos.orbits in adjusted_coords:
            px, py = adjusted_coords[wp_pos.orbits]
            orbital_lines.append(OrbitalLine(x1=px, y1=py, x2=wp_pos.x, y2=wp_pos.y))

    # Compute ship positions
    ship_positions: list[ShipPosition] = []
    transit_lines: list[TransitLine] = []
    # Track how many ships are at each waypoint for offset stacking
    ships_at_wp: dict[str, int] = {}

    for ship in ships:
        nav = ship.nav
        sx: float
        sy: float

        dest_symbol: str | None = None

        if nav.status == ShipNavStatus.IN_TRANSIT:
            # Interpolate position between origin and destination
            origin = (nav.route.origin.x, nav.route.origin.y)
            dest = (nav.route.destination.x, nav.route.destination.y)
            dest_symbol = nav.route.destination.symbol
            now = datetime.now(timezone.utc)
            total = (nav.route.arrival - nav.route.departure_time).total_seconds()
            elapsed = (now - nav.route.departure_time).total_seconds()
            t = max(0.0, min(1.0, elapsed / total if total > 0 else 1.0))
            sx = origin[0] + (dest[0] - origin[0]) * t
            sy = origin[1] + (dest[1] - origin[1]) * t

            # Add transit line
            transit_lines.append(TransitLine(
                ship_symbol=ship.symbol,
                x1=float(origin[0]),
                y1=float(origin[1]),
                x2=float(dest[0]),
                y2=float(dest[1]),
            ))
        else:
            # Ship is at a waypoint — use adjusted coords if available
            wp_sym = nav.waypoint_symbol
            if wp_sym in adjusted_coords:
                sx, sy = adjusted_coords[wp_sym]
            else:
                sx, sy = float(nav.route.destination.x), float(nav.route.destination.y)

            # Offset multiple ships at same waypoint
            count = ships_at_wp.get(wp_sym, 0)
            ships_at_wp[wp_sym] = count + 1
            sx += scale_unit * 2.5 + count * scale_unit * 2

        ship_positions.append(ShipPosition(
            symbol=ship.symbol,
            short_label=_ship_short_label(ship.symbol),
            x=sx,
            y=sy,
            status=nav.status,
            role=ship.registration.role,
            waypoint_symbol=nav.waypoint_symbol,
            destination_symbol=dest_symbol,
        ))

    return MapLayout(
        view_box=view_box,
        scale_unit=scale_unit,
        waypoints=wp_positions,
        ships=ship_positions,
        transit_lines=transit_lines,
        orbital_lines=orbital_lines,
    )
