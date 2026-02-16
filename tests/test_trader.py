"""Tests for trader.py route-finding logic."""

import math
import sqlite3
import time
from pathlib import Path

import pytest

from spacetraders.data.market_db import MarketDatabase
from spacetraders.missions.trader import (
    BACKOFF_SCHEDULE,
    FAILED_ROUTE_TTL,
    TradeRoute,
    estimate_fuel_one_way,
    estimate_fuel_round_trip,
    find_best_routes,
    safe_sell_volume,
)


# --- Fixtures ---


@pytest.fixture
def coords() -> dict[str, tuple[int, int]]:
    """Waypoint coords loosely based on real X1-XV5 system."""
    return {
        "X1-XV5-A1": (0, 0),       # HQ
        "X1-XV5-A2": (5, 0),       # nearby
        "X1-XV5-K89": (60, 70),    # ~94 from A1
        "X1-XV5-H58": (30, 35),    # ~46 from A1
        "X1-XV5-FAR": (400, 400),  # way out of range
    }


@pytest.fixture
def market_db(tmp_path: Path) -> MarketDatabase:
    """In-memory market DB seeded with test data."""
    from spacetraders.models import MarketTradeGood

    db = MarketDatabase(db_path=tmp_path / "test_markets.db")

    # K89 exports CLOTHING at 3182 buy
    db.update_market("X1-XV5-K89", [
        MarketTradeGood(
            symbol="CLOTHING", type="EXPORT", tradeVolume=20,
            supply="HIGH", activity="WEAK", purchasePrice=3182, sellPrice=1537,
        ),
        MarketTradeGood(
            symbol="FOOD", type="EXPORT", tradeVolume=60,
            supply="HIGH", activity="WEAK", purchasePrice=1438, sellPrice=694,
        ),
        MarketTradeGood(
            symbol="ALUMINUM", type="IMPORT", tradeVolume=60,
            supply="MODERATE", activity="WEAK", purchasePrice=476, sellPrice=226,
        ),
    ])

    # A1 imports CLOTHING at 4790 sell, FOOD at 2060 sell
    db.update_market("X1-XV5-A1", [
        MarketTradeGood(
            symbol="CLOTHING", type="IMPORT", tradeVolume=20,
            supply="LIMITED", activity="WEAK", purchasePrice=9884, sellPrice=4790,
        ),
        MarketTradeGood(
            symbol="FOOD", type="IMPORT", tradeVolume=60,
            supply="MODERATE", activity="WEAK", purchasePrice=4294, sellPrice=2060,
        ),
    ])

    # H58 exports ALUMINUM at 155 buy
    db.update_market("X1-XV5-H58", [
        MarketTradeGood(
            symbol="ALUMINUM", type="EXPORT", tradeVolume=60,
            supply="HIGH", activity="WEAK", purchasePrice=155, sellPrice=74,
        ),
        MarketTradeGood(
            symbol="IRON", type="EXPORT", tradeVolume=60,
            supply="ABUNDANT", activity="WEAK", purchasePrice=83, sellPrice=40,
        ),
    ])

    yield db
    db.close()


# --- estimate_fuel_one_way ---


class TestEstimateFuelOneWay:
    def test_same_location_returns_zero(self, coords: dict) -> None:
        """Ship already at destination should cost 0 fuel."""
        assert estimate_fuel_one_way(coords, "X1-XV5-A1", "X1-XV5-A1") == 0

    def test_short_distance(self, coords: dict) -> None:
        """A1(0,0) → A2(5,0) = distance 5, ceil(5) = 5."""
        assert estimate_fuel_one_way(coords, "X1-XV5-A1", "X1-XV5-A2") == 5

    def test_longer_distance(self, coords: dict) -> None:
        """A1(0,0) → K89(60,70) = sqrt(3600+4900) = sqrt(8500) ≈ 92.2 → ceil = 93."""
        expected = math.ceil(math.sqrt(60**2 + 70**2))
        assert estimate_fuel_one_way(coords, "X1-XV5-A1", "X1-XV5-K89") == expected

    def test_unknown_origin_returns_high(self, coords: dict) -> None:
        assert estimate_fuel_one_way(coords, "UNKNOWN", "X1-XV5-A1") == 9999

    def test_unknown_destination_returns_high(self, coords: dict) -> None:
        assert estimate_fuel_one_way(coords, "X1-XV5-A1", "UNKNOWN") == 9999


# --- estimate_fuel_round_trip ---


class TestEstimateFuelRoundTrip:
    def test_same_location(self, coords: dict) -> None:
        assert estimate_fuel_round_trip(coords, "X1-XV5-A1", "X1-XV5-A1") == 0

    def test_short_distance(self, coords: dict) -> None:
        """A1 → A2 = 5 each way, round trip = 10."""
        assert estimate_fuel_round_trip(coords, "X1-XV5-A1", "X1-XV5-A2") == 10

    def test_symmetric(self, coords: dict) -> None:
        """Round trip should be same regardless of direction."""
        assert (
            estimate_fuel_round_trip(coords, "X1-XV5-A1", "X1-XV5-K89")
            == estimate_fuel_round_trip(coords, "X1-XV5-K89", "X1-XV5-A1")
        )


# --- find_best_routes ---


class TestFindBestRoutes:
    def test_finds_clothing_route(self, market_db: MarketDatabase, coords: dict) -> None:
        """Should find CLOTHING K89→A1 as a profitable route."""
        routes = find_best_routes(market_db, coords, "X1-XV5-A1")
        clothing_routes = [r for r in routes if r.good == "CLOTHING"]
        assert len(clothing_routes) >= 1
        best = clothing_routes[0]
        assert best.source == "X1-XV5-K89"
        assert best.destination == "X1-XV5-A1"
        assert best.profit_per_unit == 4790 - 3182  # 1608

    def test_finds_food_route(self, market_db: MarketDatabase, coords: dict) -> None:
        """Should find FOOD K89→A1."""
        routes = find_best_routes(market_db, coords, "X1-XV5-A1")
        food_routes = [r for r in routes if r.good == "FOOD"]
        assert len(food_routes) >= 1
        assert food_routes[0].profit_per_unit == 2060 - 1438  # 622

    def test_excludes_unprofitable(self, market_db: MarketDatabase, coords: dict) -> None:
        """IRON is exported at H58 but not imported anywhere — no route."""
        routes = find_best_routes(market_db, coords, "X1-XV5-A1")
        iron_routes = [r for r in routes if r.good == "IRON"]
        assert len(iron_routes) == 0

    def test_sorted_by_net_profit(self, market_db: MarketDatabase, coords: dict) -> None:
        """Routes should be sorted descending by net_profit."""
        routes = find_best_routes(market_db, coords, "X1-XV5-A1")
        for i in range(len(routes) - 1):
            assert routes[i].net_profit >= routes[i + 1].net_profit

    def test_deadhead_zero_when_at_source(self, market_db: MarketDatabase, coords: dict) -> None:
        """When ship is at the source, deadhead should be 0."""
        routes = find_best_routes(market_db, coords, "X1-XV5-K89")
        k89_routes = [r for r in routes if r.source == "X1-XV5-K89"]
        for r in k89_routes:
            assert r.deadhead_credits == 0, f"{r.good}: deadhead should be 0 at source"

    def test_deadhead_affects_ranking(self, market_db: MarketDatabase, coords: dict) -> None:
        """Same route should have different net profit from different positions."""
        from_a1 = find_best_routes(market_db, coords, "X1-XV5-A1")
        from_k89 = find_best_routes(market_db, coords, "X1-XV5-K89")

        def find_route(routes: list, good: str, src: str) -> TradeRoute | None:
            return next((r for r in routes if r.good == good and r.source == src), None)

        clothing_from_a1 = find_route(from_a1, "CLOTHING", "X1-XV5-K89")
        clothing_from_k89 = find_route(from_k89, "CLOTHING", "X1-XV5-K89")

        assert clothing_from_a1 is not None
        assert clothing_from_k89 is not None
        # From K89 (at source) should be more profitable than from A1 (deadhead needed)
        assert clothing_from_k89.net_profit > clothing_from_a1.net_profit

    def test_excludes_unreachable(self, market_db: MarketDatabase, coords: dict) -> None:
        """Routes requiring more fuel than capacity should be excluded."""
        routes = find_best_routes(market_db, coords, "X1-XV5-FAR", fuel_capacity=50)
        # Everything from FAR is >50 fuel away, nothing should be reachable
        assert len(routes) == 0

    def test_net_profit_accounts_for_fuel(self, market_db: MarketDatabase, coords: dict) -> None:
        """Net profit should be gross - route fuel - deadhead fuel."""
        routes = find_best_routes(market_db, coords, "X1-XV5-A1")
        for r in routes:
            expected_gross = r.profit_per_unit * 40
            expected_net = expected_gross - r.fuel_cost_credits - r.deadhead_credits
            assert r.net_profit == expected_net, f"{r.good}: net mismatch"

    def test_fuel_check_independent_legs(self, market_db: MarketDatabase, coords: dict) -> None:
        """Routes where deadhead + route > capacity but each leg < capacity should be included.

        Ship refuels at source between deadhead and route legs.
        """
        # A1→K89 is ~93 fuel one way, round trip K89↔A1 is ~186
        # Total = 93 + 186 = 279 — under 300
        # But if fuel_capacity=200: deadhead=93, route=186 — sum=279 > 200
        # However: deadhead=93 < 200, and after refuel route=186 < 200
        # So it SHOULD be included with fuel_capacity=200
        routes = find_best_routes(market_db, coords, "X1-XV5-A1", fuel_capacity=200)
        clothing = [r for r in routes if r.good == "CLOTHING" and r.source == "X1-XV5-K89"]
        assert len(clothing) >= 1, "Route should be feasible since ship refuels at source"

    def test_excluded_routes_filtered(self, market_db: MarketDatabase, coords: dict) -> None:
        """Routes claimed by other ships should not appear in results."""
        # Get all routes first
        all_routes = find_best_routes(market_db, coords, "X1-XV5-A1")
        clothing = [r for r in all_routes if r.good == "CLOTHING"]
        assert len(clothing) >= 1

        # Exclude the clothing route
        excluded = [("CLOTHING", "X1-XV5-K89", "X1-XV5-A1")]
        filtered = find_best_routes(
            market_db, coords, "X1-XV5-A1", excluded_routes=excluded,
        )
        clothing_after = [r for r in filtered if r.good == "CLOTHING"
                          and r.source == "X1-XV5-K89" and r.destination == "X1-XV5-A1"]
        assert len(clothing_after) == 0, "Excluded route should be filtered out"

        # Other routes should still exist
        food = [r for r in filtered if r.good == "FOOD"]
        assert len(food) >= 1, "Non-excluded routes should remain"


# --- Route claims DB ---


class TestRouteClaims:
    def test_claim_and_get(self, market_db: MarketDatabase) -> None:
        """Claiming a route should be visible to other ships."""
        market_db.claim_route("SHIP-A", "CLOTHING", "X1-XV5-K89", "X1-XV5-A1")
        claims = market_db.get_claimed_routes(exclude_ship="SHIP-B")
        assert ("CLOTHING", "X1-XV5-K89", "X1-XV5-A1") in claims

    def test_claim_invisible_to_self(self, market_db: MarketDatabase) -> None:
        """A ship's own claim should not appear in its own query."""
        market_db.claim_route("SHIP-A", "CLOTHING", "X1-XV5-K89", "X1-XV5-A1")
        claims = market_db.get_claimed_routes(exclude_ship="SHIP-A")
        assert len(claims) == 0

    def test_release_removes_claim(self, market_db: MarketDatabase) -> None:
        """Releasing should remove the claim."""
        market_db.claim_route("SHIP-A", "CLOTHING", "X1-XV5-K89", "X1-XV5-A1")
        market_db.release_route("SHIP-A")
        claims = market_db.get_claimed_routes(exclude_ship="SHIP-B")
        assert len(claims) == 0

    def test_upsert_replaces_claim(self, market_db: MarketDatabase) -> None:
        """Claiming again should replace the old claim (one claim per ship)."""
        market_db.claim_route("SHIP-A", "CLOTHING", "X1-XV5-K89", "X1-XV5-A1")
        market_db.claim_route("SHIP-A", "FOOD", "X1-XV5-K89", "X1-XV5-A1")
        claims = market_db.get_claimed_routes(exclude_ship="SHIP-B")
        assert len(claims) == 1
        assert claims[0][0] == "FOOD"

    def test_stale_claims_auto_expire(self, market_db: MarketDatabase) -> None:
        """Claims older than max_age_min should be ignored and cleaned up."""
        import datetime as dt
        # Insert a stale claim directly
        old_time = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)).isoformat()
        market_db._conn.execute(
            "INSERT INTO route_claims VALUES (?, ?, ?, ?, ?)",
            ("SHIP-A", "CLOTHING", "X1-XV5-K89", "X1-XV5-A1", old_time),
        )
        market_db._conn.commit()

        claims = market_db.get_claimed_routes(exclude_ship="SHIP-B", max_age_min=15.0)
        assert len(claims) == 0, "Stale claim should be expired"


# --- safe_sell_volume ---


class TestSafeSellVolume:
    def test_scarce_caps_low(self) -> None:
        """SCARCE supply with small trade volume → very conservative cap."""
        assert safe_sell_volume("SCARCE", "WEAK", 6) == 12

    def test_limited_caps_moderate(self) -> None:
        """LIMITED × 3.0 = 18 with vol=6 — safely under the ~20-24 crash threshold."""
        assert safe_sell_volume("LIMITED", "WEAK", 6) == 18

    def test_high_allows_more(self) -> None:
        """HIGH supply with large trade volume → capped at cargo capacity."""
        assert safe_sell_volume("HIGH", "WEAK", 60, cargo_capacity=40) == 40

    def test_strong_activity_bonus(self) -> None:
        """STRONG activity adds +1.0 to multiplier (LIMITED 3.0 → 4.0)."""
        assert safe_sell_volume("LIMITED", "STRONG", 6) == 24

    def test_unknown_supply_defaults(self) -> None:
        """Unrecognized supply level falls back to 3.0× (same as LIMITED)."""
        assert safe_sell_volume("UNKNOWN_LEVEL", "WEAK", 6) == 18

    def test_never_exceeds_cargo(self) -> None:
        """Result must never exceed cargo_capacity regardless of multiplier."""
        assert safe_sell_volume("ABUNDANT", None, 100, cargo_capacity=40) == 40
        assert safe_sell_volume("ABUNDANT", "STRONG", 100, cargo_capacity=25) == 25


# --- Dry market resilience constants ---


class TestDryMarketConstants:
    def test_failed_route_ttl_is_30_minutes(self) -> None:
        """TTL should be 1800 seconds (30 minutes)."""
        assert FAILED_ROUTE_TTL == 1800

    def test_backoff_schedule_escalates(self) -> None:
        """Backoff should be strictly increasing: 5, 10, 20, 30 min."""
        assert BACKOFF_SCHEDULE == [300, 600, 1200, 1800]
        for i in range(len(BACKOFF_SCHEDULE) - 1):
            assert BACKOFF_SCHEDULE[i] < BACKOFF_SCHEDULE[i + 1]

    def test_backoff_schedule_caps_at_30_min(self) -> None:
        """Max backoff should be 30 minutes regardless of dry streak length."""
        max_backoff = BACKOFF_SCHEDULE[min(99, len(BACKOFF_SCHEDULE) - 1)]
        assert max_backoff == 1800


class TestFailedRouteMemory:
    """Test the time-based failed route dict pattern used in run_trade."""

    def test_fresh_failure_excluded(self) -> None:
        """A just-failed route should appear in exclusion list."""
        failed: dict[tuple[str, str, str], float] = {}
        key = ("CLOTHING", "X1-XV5-K89", "X1-XV5-A1")
        failed[key] = time.monotonic()

        now = time.monotonic()
        active = {k: v for k, v in failed.items() if now - v < FAILED_ROUTE_TTL}
        assert key in active

    def test_expired_failure_pruned(self) -> None:
        """A failure older than TTL should be pruned."""
        failed: dict[tuple[str, str, str], float] = {}
        key = ("CLOTHING", "X1-XV5-K89", "X1-XV5-A1")
        # Simulate a failure that happened TTL+1 seconds ago
        failed[key] = time.monotonic() - FAILED_ROUTE_TTL - 1

        now = time.monotonic()
        active = {k: v for k, v in failed.items() if now - v < FAILED_ROUTE_TTL}
        assert key not in active

    def test_multiple_routes_independent(self) -> None:
        """Different routes have independent TTLs — one expiring doesn't affect others."""
        failed: dict[tuple[str, str, str], float] = {}
        old_key = ("FOOD", "X1-XV5-K89", "X1-XV5-A1")
        new_key = ("CLOTHING", "X1-XV5-K89", "X1-XV5-A1")
        failed[old_key] = time.monotonic() - FAILED_ROUTE_TTL - 1  # expired
        failed[new_key] = time.monotonic()  # fresh

        now = time.monotonic()
        active = {k: v for k, v in failed.items() if now - v < FAILED_ROUTE_TTL}
        assert old_key not in active
        assert new_key in active

    def test_same_route_updates_timestamp(self) -> None:
        """Re-failing the same route should update its blacklist time."""
        failed: dict[tuple[str, str, str], float] = {}
        key = ("CLOTHING", "X1-XV5-K89", "X1-XV5-A1")
        first_time = time.monotonic() - 1000
        failed[key] = first_time

        # Route fails again — timestamp updates
        failed[key] = time.monotonic()
        assert failed[key] > first_time
