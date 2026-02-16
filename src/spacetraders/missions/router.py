"""Fuel-aware route planning for ship navigation.

Calculates distances, fuel costs, and travel times between waypoints.
Enforces a fuel reserve so ships never strand themselves.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from spacetraders.models import Ship, Waypoint


# Fuel reserve: never use more than this fraction of capacity
FUEL_RESERVE_FRACTION = 0.20

# Flight mode parameters (from SpaceTraders docs + observations)
# CRUISE: fuel = distance, time = round(15 + distance * 25 / speed)
# DRIFT:  fuel = 1,         time = round(15 + distance * 250 / speed)
# BURN:   fuel = 2*distance, time = round(15 + distance * 12.5 / speed)


@dataclass(frozen=True)
class RouteSegment:
    """A single leg of a route."""

    origin: str
    destination: str
    distance: float
    flight_mode: str
    fuel_cost: int
    travel_seconds: int


@dataclass(frozen=True)
class RoutePlan:
    """A complete route with fuel analysis."""

    segments: list[RouteSegment]
    total_fuel: int
    total_seconds: int
    feasible: bool
    reason: str = ""

    @property
    def total_minutes(self) -> float:
        return self.total_seconds / 60

    @property
    def num_stops(self) -> int:
        """Number of intermediate refueling stops (0 for direct routes)."""
        if not self.feasible or not self.segments:
            return 0
        return len(self.segments) - 1


def distance(x1: int, y1: int, x2: int, y2: int) -> float:
    """Euclidean distance between two waypoints."""
    return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)


def waypoint_distance(a: Waypoint, b: Waypoint) -> float:
    """Distance between two Waypoint objects."""
    return distance(a.x, a.y, b.x, b.y)


def fuel_cost(dist: float, mode: str) -> int:
    """Estimate fuel consumed for a given distance and flight mode."""
    d = max(1, math.ceil(dist))
    if mode == "DRIFT":
        return 1
    if mode == "BURN":
        return d * 2
    # CRUISE (default)
    return d


def travel_time(dist: float, speed: int, mode: str) -> int:
    """Estimate travel time in seconds for a given distance, speed, and flight mode."""
    if mode == "DRIFT":
        multiplier = 250.0
    elif mode == "BURN":
        multiplier = 12.5
    else:  # CRUISE
        multiplier = 25.0
    return round(15 + dist * multiplier / max(speed, 1))


def usable_fuel(ship: Ship) -> int:
    """Fuel available after reserving safety margin."""
    reserve = math.ceil(ship.fuel.capacity * FUEL_RESERVE_FRACTION)
    return max(0, ship.fuel.current - reserve)


def can_reach(ship: Ship, dist: float, mode: str = "CRUISE") -> bool:
    """Can the ship reach a destination at the given distance with fuel to spare?"""
    cost = fuel_cost(dist, mode)
    return cost <= usable_fuel(ship)


def plan_segment(
    origin_x: int,
    origin_y: int,
    dest_x: int,
    dest_y: int,
    origin_symbol: str,
    dest_symbol: str,
    speed: int,
    mode: str = "CRUISE",
) -> RouteSegment:
    """Plan a single route segment between two points."""
    dist = distance(origin_x, origin_y, dest_x, dest_y)
    return RouteSegment(
        origin=origin_symbol,
        destination=dest_symbol,
        distance=dist,
        flight_mode=mode,
        fuel_cost=fuel_cost(dist, mode),
        travel_seconds=travel_time(dist, speed, mode),
    )


def plan_round_trip(
    ship: Ship,
    ship_wp: Waypoint,
    target_wp: Waypoint,
    return_wp: Waypoint,
    prefer_drift: bool = False,
) -> RoutePlan:
    """Plan a round trip: current → target → return point.

    Picks CRUISE if fuel allows, DRIFT if not. If even DRIFT is infeasible
    for the return leg, marks the plan as infeasible.
    """
    speed = ship.engine.speed
    available = usable_fuel(ship)

    # Leg 1: ship → target
    dist_out = waypoint_distance(ship_wp, target_wp)
    mode_out = "DRIFT" if prefer_drift or fuel_cost(dist_out, "CRUISE") > available else "CRUISE"
    leg1 = plan_segment(
        ship_wp.x, ship_wp.y, target_wp.x, target_wp.y,
        ship_wp.symbol, target_wp.symbol, speed, mode_out,
    )

    # Fuel remaining after leg 1
    fuel_after_leg1 = available - leg1.fuel_cost

    # Leg 2: target → return
    dist_back = waypoint_distance(target_wp, return_wp)
    cost_cruise_back = fuel_cost(dist_back, "CRUISE")
    cost_drift_back = fuel_cost(dist_back, "DRIFT")

    if not prefer_drift and cost_cruise_back <= fuel_after_leg1:
        mode_back = "CRUISE"
    elif cost_drift_back <= fuel_after_leg1:
        mode_back = "DRIFT"
    else:
        # Can't make it back even on DRIFT
        return RoutePlan(
            segments=[],
            total_fuel=0,
            total_seconds=0,
            feasible=False,
            reason=f"Insufficient fuel for return. Need {cost_drift_back} (DRIFT), have {fuel_after_leg1} after outbound.",
        )

    leg2 = plan_segment(
        target_wp.x, target_wp.y, return_wp.x, return_wp.y,
        target_wp.symbol, return_wp.symbol, speed, mode_back,
    )

    total_fuel = leg1.fuel_cost + leg2.fuel_cost
    if total_fuel > available:
        return RoutePlan(
            segments=[],
            total_fuel=total_fuel,
            total_seconds=0,
            feasible=False,
            reason=f"Total fuel {total_fuel} exceeds available {available} (with {FUEL_RESERVE_FRACTION:.0%} reserve).",
        )

    return RoutePlan(
        segments=[leg1, leg2],
        total_fuel=total_fuel,
        total_seconds=leg1.travel_seconds + leg2.travel_seconds,
        feasible=True,
    )


def best_flight_mode(ship: Ship, dist: float) -> str:
    """Pick the best flight mode the ship can afford for a given distance."""
    available = usable_fuel(ship)
    if fuel_cost(dist, "CRUISE") <= available:
        return "CRUISE"
    if fuel_cost(dist, "DRIFT") <= available:
        return "DRIFT"
    return "DRIFT"  # DRIFT costs 1 fuel, always cheapest


# ---------------------------------------------------------------------------
# Multi-hop refueling pathfinder
# ---------------------------------------------------------------------------

REFUEL_STOP_OVERHEAD = 30  # seconds per intermediate stop (dock + refuel + orbit)


def build_fuel_waypoints(waypoints: list[Waypoint]) -> set[str]:
    """Build set of waypoint symbols that have marketplaces (for refueling)."""
    return {
        wp.symbol for wp in waypoints
        if any(t.symbol == "MARKETPLACE" for t in wp.traits)
    }


def plan_multihop(
    coords: dict[str, tuple[int, int]],
    fuel_waypoints: set[str],
    origin: str,
    destination: str,
    fuel_capacity: int,
    speed: int,
    mode: str = "CRUISE",
) -> RoutePlan:
    """Plan a multi-hop route with refueling stops.

    Uses greedy forward-progress: at each step, pick the reachable fuel
    waypoint that makes the most progress toward the destination.
    Returns a single-leg plan if destination is directly reachable.
    Assumes the ship starts with full fuel and refuels to full at each stop.
    """
    if origin == destination:
        return RoutePlan(segments=[], total_fuel=0, total_seconds=0, feasible=True)

    if origin not in coords or destination not in coords:
        return RoutePlan(
            segments=[], total_fuel=0, total_seconds=0, feasible=False,
            reason=f"Unknown coordinates for {origin} or {destination}",
        )

    dest_x, dest_y = coords[destination]
    current = origin
    segments: list[RouteSegment] = []
    visited: set[str] = {origin}

    max_hops = len(fuel_waypoints) + 1

    for _ in range(max_hops):
        cx, cy = coords[current]
        dist_to_dest = distance(cx, cy, dest_x, dest_y)

        # Can we reach destination directly?
        if fuel_cost(dist_to_dest, mode) <= fuel_capacity:
            seg = plan_segment(cx, cy, dest_x, dest_y, current, destination, speed, mode)
            segments.append(seg)
            total_fuel = sum(s.fuel_cost for s in segments)
            total_secs = sum(s.travel_seconds for s in segments)
            # Add overhead for intermediate stops (all except final arrival)
            total_secs += (len(segments) - 1) * REFUEL_STOP_OVERHEAD
            return RoutePlan(
                segments=segments,
                total_fuel=total_fuel,
                total_seconds=total_secs,
                feasible=True,
            )

        # Find reachable fuel waypoints that make forward progress
        best_wp: str | None = None
        best_remaining_dist = dist_to_dest  # must improve on current distance

        for wp_sym in fuel_waypoints:
            if wp_sym in visited or wp_sym not in coords:
                continue
            wx, wy = coords[wp_sym]
            dist_to_wp = distance(cx, cy, wx, wy)

            # Must be reachable with current fuel capacity
            if fuel_cost(dist_to_wp, mode) > fuel_capacity:
                continue

            # Must make progress toward destination
            remaining = distance(wx, wy, dest_x, dest_y)
            if remaining < best_remaining_dist:
                best_remaining_dist = remaining
                best_wp = wp_sym

        if best_wp is None:
            return RoutePlan(
                segments=[], total_fuel=0, total_seconds=0, feasible=False,
                reason=f"No reachable fuel waypoint makes progress from {current} toward {destination}",
            )

        # Add segment to this waypoint
        wx, wy = coords[best_wp]
        seg = plan_segment(cx, cy, wx, wy, current, best_wp, speed, mode)
        segments.append(seg)
        visited.add(best_wp)
        current = best_wp

    return RoutePlan(
        segments=[], total_fuel=0, total_seconds=0, feasible=False,
        reason=f"Exceeded max hops ({max_hops}) — route infeasible",
    )
