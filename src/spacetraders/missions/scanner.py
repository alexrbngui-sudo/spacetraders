"""Asteroid scanning, ranking, and selection.

Filters asteroids by traits, ranks by deposit quality and distance,
uses shared yield database for history, and blacklists dry ones.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from spacetraders.data.asteroid_db import AsteroidDatabase, YieldRecord
from spacetraders.missions.router import distance, fuel_cost, usable_fuel
from spacetraders.models import Ship, Waypoint

logger = logging.getLogger(__name__)

# Asteroid types worth mining
ASTEROID_TYPES = {"ASTEROID", "ASTEROID_FIELD", "ENGINEERED_ASTEROID", "ASTEROID_BASE"}

# Traits that disqualify an asteroid
BAD_TRAITS = {"STRIPPED"}

# Deposit traits and their value for resource targeting
# Higher score = more likely to contain valuable metals
DEPOSIT_SCORES: dict[str, float] = {
    "COMMON_METAL_DEPOSITS": 2.0,
    "MINERAL_DEPOSITS": 1.5,
    "PRECIOUS_METAL_DEPOSITS": 3.0,
    "RARE_METAL_DEPOSITS": 4.0,
    "EXPLOSIVE_GASES": 0.5,
    "SHALLOW_DEPOSITS": 1.0,
    "DEEP_DEPOSITS": 1.0,
    "RADIOACTIVE_DEPOSITS": 1.5,
}


@dataclass
class AsteroidCandidate:
    """A ranked asteroid candidate for mining."""

    waypoint: Waypoint
    distance_from_ship: float
    distance_from_return: float
    deposit_score: float
    yield_history: YieldRecord | None
    fuel_cost_cruise: int
    fuel_cost_drift: int
    reachable_cruise: bool
    reachable_drift: bool

    @property
    def rank_score(self) -> float:
        """Higher = better candidate. Balances deposit quality vs travel cost."""
        # Penalize distance (closer is better)
        dist_penalty = self.distance_from_ship / 100.0

        # Bonus for good deposits
        deposit_bonus = self.deposit_score

        # Bonus/penalty for yield history
        history_bonus = 0.0
        if self.yield_history and self.yield_history.total_extractions >= 5:
            history_bonus = (self.yield_history.hit_rate - 0.15) * 5.0

        return deposit_bonus + history_bonus - dist_penalty


def is_minable_asteroid(wp: Waypoint) -> bool:
    """Check if a waypoint is a minable asteroid (not stripped)."""
    if wp.type.value not in ASTEROID_TYPES:
        return False
    trait_symbols = {t.symbol for t in wp.traits}
    if trait_symbols & BAD_TRAITS:
        return False
    return True


def deposit_score(wp: Waypoint) -> float:
    """Score an asteroid's deposit traits. Higher = better for mining."""
    return sum(
        DEPOSIT_SCORES.get(t.symbol, 0.0)
        for t in wp.traits
    )


def rank_asteroids(
    waypoints: list[Waypoint],
    ship: Ship,
    ship_x: int,
    ship_y: int,
    return_wp: Waypoint,
    asteroid_db: AsteroidDatabase,
    resource: str,
    max_results: int = 10,
) -> list[AsteroidCandidate]:
    """Rank available asteroids by mining potential.

    Filters out stripped and blacklisted asteroids, then ranks by
    deposit quality, distance, and yield history.
    """
    available_fuel = usable_fuel(ship)
    candidates: list[AsteroidCandidate] = []

    for wp in waypoints:
        if not is_minable_asteroid(wp):
            continue
        if asteroid_db.is_blacklisted(wp.symbol, resource):
            continue

        dist_ship = distance(ship_x, ship_y, wp.x, wp.y)
        dist_return = distance(wp.x, wp.y, return_wp.x, return_wp.y)
        fc_cruise = fuel_cost(dist_ship, "CRUISE")
        fc_drift = fuel_cost(dist_ship, "DRIFT")

        candidates.append(AsteroidCandidate(
            waypoint=wp,
            distance_from_ship=dist_ship,
            distance_from_return=dist_return,
            deposit_score=deposit_score(wp),
            yield_history=asteroid_db.get_stats(wp.symbol, resource),
            fuel_cost_cruise=fc_cruise,
            fuel_cost_drift=fc_drift,
            reachable_cruise=fc_cruise <= available_fuel,
            reachable_drift=fc_drift <= available_fuel,
        ))

    # Sort by rank score (descending)
    candidates.sort(key=lambda c: c.rank_score, reverse=True)
    return candidates[:max_results]
