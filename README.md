# SpaceTraders Client

A Python client for [SpaceTraders](https://spacetraders.io/) - a multiplayer space trading game played entirely through API calls.

Explore star systems, trade goods, mine asteroids, fulfill contracts, and build a fleet. No GUI required.

## Setup

```bash
# Clone and install
git clone https://github.com/alexrbngui-sudo/spacetraders.git
cd spacetraders
pip install -e .

# Configure
cp .env.example .env
# Add your agent token to .env
```

## Configuration

1. Create an account at [my.spacetraders.io](https://my.spacetraders.io)
2. Register an agent (pick a callsign and faction)
3. Copy your agent token into `.env`

> The game resets weekly (Saturdays ~13:00 UTC). You'll need to re-register and get a new token after each reset.

## Running

All commands run from the project root (where `.env` lives).

### Mining mission

Automated mine-deliver-refuel loop. Mines a target resource, delivers to contract waypoint, refuels, repeats until the contract is fulfilled. Auto-negotiates the next contract if the fulfilled one matches the same resource.

```bash
python -m spacetraders.missions --ship UTMOSTLY-1 --resource COPPER_ORE
```

Options:
- `--trips N` — stop after N round trips (default: run until contract done)
- `--timeout N` — stop after N minutes wall-clock time

Logs to `data/logs/mission_utmostly-1.log` and stdout. Ctrl+C for graceful shutdown.

### Probe market scanner

Sends the solar-powered probe to every marketplace in the system. Caches all trade good prices to `data/markets.db` for offline analysis.

```bash
python -m spacetraders.missions.probe_scanner --ship UTMOSTLY-2
```

The probe uses DRIFT mode (no fuel cost). Visits marketplaces in nearest-neighbor order. Takes a while but runs unattended.

### Autonomous trader

Finds the most profitable EXPORT→IMPORT routes (ranked by profit per minute), then loops: fly to source, buy, fly to destination, sell, refuel, repeat. Refreshes market prices at each stop so routes adapt to shifting supply/demand. Sells any existing cargo on startup (e.g. ore from a mining drone).

```bash
python -m spacetraders.missions.trader --ship UTMOSTLY-3 --continuous
```

Options:
- `--loops N` — trades per cycle before re-evaluating (default: 3)
- `--scout` — visit key markets to discover prices before trading
- `--continuous` — run indefinitely until stopped (Ctrl+C for graceful shutdown)

Logs to `data/logs/mission_utmostly-3.log` and stdout. Requires market data in `data/markets.db` (populated by the probe scanner or market visits).

### Trade route calculator

Finds profitable buy/sell pairs from cached market data. No API calls — works entirely offline.

```bash
python -m spacetraders.missions.trade_routes
```

Options:
- `--min-profit N` — minimum profit per unit to show (default: 1)
- `--top N` — show top N routes (default: 15)
- `--good SYMBOL` — filter by trade good (e.g. `--good FUEL`)

Requires market data in `data/markets.db` (populated by the probe scanner or mission visits).

### Web dashboard

Browser UI for fleet overview, ship actions, market data, and contracts.

```bash
python -m spacetraders.web
# Opens at http://localhost:8080
```

## Project Structure

```
src/spacetraders/
├── client.py              # Async httpx client, rate limiter, retry logic
├── models.py              # Pydantic models for all API responses
├── config.py              # Settings from .env
├── api/
│   ├── agent.py           # Agent info
│   ├── fleet.py           # Ships: orbit, dock, navigate, refuel, cargo, buy/sell
│   ├── contracts.py       # Contract lifecycle: list, accept, deliver, fulfill, negotiate
│   ├── navigation.py      # Systems, waypoints, markets, shipyards
│   └── mining.py          # Extract, survey
├── data/
│   ├── asteroid_db.py     # SQLite asteroid yield tracking + blacklist
│   └── market_db.py       # SQLite market price cache
├── missions/              # Automated operations
│   ├── runner.py          # Mining mission loop + contract auto-negotiation
│   ├── mining.py          # Extract loop: survey, extract, sell/jettison junk
│   ├── router.py          # Fuel-aware route planning, flight mode selection
│   ├── scanner.py         # Asteroid ranking, trait filtering, blacklist
│   ├── probe_scanner.py   # Probe visits all marketplaces, caches prices
│   ├── trade_routes.py    # Offline trade route calculator
│   ├── trader.py          # Autonomous trader: buy/sell loops, profit/min routing
│   ├── shipyard_scout.py  # One-shot probe mission to catalog shipyards
│   └── drone_swarm.py     # Multi-drone mining + shuttle hauler
└── web/
    ├── app.py             # FastAPI + Jinja2 + htmx
    ├── routes/            # Page handlers (dashboard, fleet, contracts, navigation, market)
    ├── templates/         # HTML templates
    └── static/            # CSS + JS

docs/
├── api-reference.md       # API endpoint reference
├── game-concepts.md       # Game mechanics
├── strategy.md            # Strategies, testing checklist, automation backlog
└── crew-manifest.md       # Company lore and officer roster
```

## API

- **Base URL:** `https://api.spacetraders.io/v2`
- **Docs:** [docs.spacetraders.io](https://docs.spacetraders.io)
- **OpenAPI Spec:** [api.spacetraders.io/v2/documentation/json](https://api.spacetraders.io/v2/documentation/json)
