"""Trade route calculator — finds profitable buy/sell pairs from cached market data.

Usage:
    python -m spacetraders.missions.trade_routes [--min-profit 10] [--top 10]

Reads from markets.db (populated by probe scanner or mission visits).
No API calls — works entirely from cached data.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from spacetraders.config import load_settings
from spacetraders.data.market_db import MarketDatabase, MarketPriceRecord

logger = logging.getLogger("spacetraders.missions")


@dataclass(frozen=True)
class TradeRoute:
    """A profitable trade opportunity between two waypoints."""

    trade_symbol: str
    buy_waypoint: str
    buy_price: int
    buy_type: str       # EXPORT, EXCHANGE
    buy_supply: str
    sell_waypoint: str
    sell_price: int
    sell_type: str       # IMPORT, EXCHANGE
    sell_supply: str
    trade_volume: int    # min of buy/sell volume (throughput bottleneck)

    @property
    def profit_per_unit(self) -> int:
        return self.sell_price - self.buy_price

    @property
    def profit_per_trip(self) -> int:
        """Estimated profit for one full cargo load (40 units)."""
        return self.profit_per_unit * min(40, self.trade_volume)

    def summary(self) -> str:
        return (
            f"{self.trade_symbol}: "
            f"buy at {self.buy_waypoint} ({self.buy_price}/u, {self.buy_type} {self.buy_supply}) → "
            f"sell at {self.sell_waypoint} ({self.sell_price}/u, {self.sell_type} {self.sell_supply}) = "
            f"+{self.profit_per_unit}/u, ~{self.profit_per_trip:,}/trip "
            f"(vol {self.trade_volume})"
        )


def find_trade_routes(
    market_db: MarketDatabase,
    min_profit: int = 1,
) -> list[TradeRoute]:
    """Find all profitable trade routes from cached market data.

    Looks for goods where one waypoint's purchase price is lower
    than another waypoint's sell price. Prioritizes EXPORT→IMPORT
    pairs since those have the best margins.
    """
    all_markets = market_db.get_all_markets()
    if not all_markets:
        return []

    # Build price index: trade_symbol → list of (waypoint, record)
    by_good: dict[str, list[MarketPriceRecord]] = {}
    for wp in all_markets:
        for record in market_db.get_prices(wp):
            by_good.setdefault(record.trade_symbol, []).append(record)

    routes: list[TradeRoute] = []

    for trade_symbol, records in by_good.items():
        if len(records) < 2:
            continue

        # Find all buy/sell pairs across different waypoints
        for buyer in records:
            for seller in records:
                if buyer.waypoint_symbol == seller.waypoint_symbol:
                    continue

                profit = seller.sell_price - buyer.purchase_price
                if profit < min_profit:
                    continue

                routes.append(TradeRoute(
                    trade_symbol=trade_symbol,
                    buy_waypoint=buyer.waypoint_symbol,
                    buy_price=buyer.purchase_price,
                    buy_type=buyer.type,
                    buy_supply=buyer.supply,
                    sell_waypoint=seller.waypoint_symbol,
                    sell_price=seller.sell_price,
                    sell_type=seller.type,
                    sell_supply=seller.supply,
                    trade_volume=min(buyer.trade_volume, seller.trade_volume),
                ))

    # Sort by profit per unit descending
    routes.sort(key=lambda r: r.profit_per_unit, reverse=True)
    return routes


def print_market_summary(market_db: MarketDatabase) -> None:
    """Print overview of cached market data."""
    all_markets = market_db.get_all_markets()
    stale = market_db.get_stale_markets(max_age_hours=1.0)

    print(f"\nMarket cache: {len(all_markets)} waypoints")
    if stale:
        print(f"Stale (>1h): {', '.join(stale)}")

    for wp in all_markets:
        prices = market_db.get_prices(wp)
        exports = [p for p in prices if p.type == "EXPORT"]
        imports = [p for p in prices if p.type == "IMPORT"]
        exchanges = [p for p in prices if p.type == "EXCHANGE"]
        freshness = prices[0].updated_at[:19] if prices else "?"
        print(f"  {wp}: {len(exports)}E {len(imports)}I {len(exchanges)}X (as of {freshness})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Trade route calculator")
    parser.add_argument("--min-profit", type=int, default=1, help="Min profit per unit")
    parser.add_argument("--top", type=int, default=15, help="Show top N routes")
    parser.add_argument("--good", type=str, default="", help="Filter by trade good symbol")
    args = parser.parse_args()

    # Minimal logging
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    settings = load_settings()
    db_path = settings.data_dir / "markets.db"
    if not db_path.exists():
        print(f"No market cache found at {db_path}")
        print("Run the probe scanner first: python -m spacetraders.missions.probe_scanner")
        return

    market_db = MarketDatabase(db_path=db_path)

    try:
        print_market_summary(market_db)

        routes = find_trade_routes(market_db, min_profit=args.min_profit)

        if args.good:
            routes = [r for r in routes if r.trade_symbol == args.good.upper()]

        if not routes:
            print("\nNo profitable routes found.")
            if not market_db.get_all_markets():
                print("Market cache is empty — run the probe scanner first.")
            return

        print(f"\nTop {min(args.top, len(routes))} trade routes (min profit: {args.min_profit}/u):\n")
        for i, route in enumerate(routes[:args.top], 1):
            print(f"  {i:2d}. {route.summary()}")

        # Show best per-trip routes
        by_trip = sorted(routes, key=lambda r: r.profit_per_trip, reverse=True)
        print(f"\nBest per-trip (40 cargo):\n")
        for i, route in enumerate(by_trip[:5], 1):
            print(f"  {i}. {route.trade_symbol}: +{route.profit_per_trip:,} credits/trip ({route.buy_waypoint} → {route.sell_waypoint})")

    finally:
        market_db.close()


if __name__ == "__main__":
    main()
