# SpaceTraders Client

Python client for the [SpaceTraders](https://spacetraders.io/) API game - a space-themed economic simulation played entirely through REST API calls.

---

## Project Overview

**What:** Automated agent for exploring, trading, and mining in the SpaceTraders universe.
**API:** `https://api.spacetraders.io/v2` (v2.3.0, alpha)
**Auth:** Bearer token in `Authorization` header
**Resets:** Weekly (Saturdays ~13:00 UTC) - tokens invalidated, must re-register

---

## Architecture

```
src/spacetraders/
├── __init__.py
├── config.py          # Pydantic Settings from .env
├── client.py          # Async httpx client, rate limiter, error handling
├── models.py          # Pydantic models for all API responses
├── api/
│   ├── agent.py       # Agent info
│   ├── fleet.py       # Ships: orbit, dock, navigate, refuel, cargo, buy/sell
│   ├── contracts.py   # Contract lifecycle: list, accept, deliver, fulfill, negotiate
│   ├── navigation.py  # Systems, waypoints, markets, shipyards
│   └── mining.py      # Extract, survey
├── data/
│   ├── asteroid_db.py    # SQLite asteroid yield tracking + blacklist
│   ├── fleet_db.py       # SQLite ship assignment registry (cross-process requisition)
│   ├── market_db.py      # SQLite market price cache (updated by probe + missions)
│   └── operations_db.py  # SQLite operations ledger (trades, extractions, snapshots)
├── missions/              # AUTOMATED OPERATIONS — run as: python -m spacetraders.missions
│   ├── __init__.py
│   ├── __main__.py    # Entry point (mining missions)
│   ├── runner.py      # Mining mission loop: mine → deliver → refuel → repeat + auto-negotiate
│   ├── mining.py      # Core mining: survey, extract, sell/jettison, yield tracking
│   ├── router.py      # Fuel-aware route planning, distance/cost/time calculations
│   ├── scanner.py     # Asteroid ranking, trait filtering, blacklist management
│   ├── probe_scanner.py  # Probe visits all marketplaces, caches prices to market_db
│   ├── trade_routes.py   # Offline trade route calculator from cached market data
│   ├── trader.py      # **Autonomous trader**: finds best buy→sell routes, executes continuously
│   ├── shipyard_scout.py  # One-shot probe mission to catalog all shipyards
│   ├── drone_swarm.py    # Multi-drone mining swarm + shuttle hauler orchestrator
│   ├── contractor.py     # **Contract runner**: negotiate → buy → deliver → fulfill → repeat
│   └── gate_builder.py   # **Gate builder**: haul FAB_MATS + ADVANCED_CIRCUITRY to jump gate
├── fleet/                     # MISSION CONTROL — run as: python -m spacetraders.fleet
│   ├── __init__.py
│   ├── __main__.py        # Entry point: python -m spacetraders.fleet [--assign SHIP:mission]
│   ├── commander.py       # FleetCommander — event-driven orchestrator, strategy re-evaluation
│   ├── events.py          # EventType enum + FleetEvent dataclass (drives strategy decisions)
│   ├── strategy.py        # FleetStrategy — pure decision engine: Gate > Contracts > Trade > Idle
│   ├── scheduler.py       # RequestScheduler — in-memory priority rate limiter (replaces SQLite)
│   ├── state.py           # FleetState + SystemState — shared fleet coordination + event queue
│   ├── ship_agent.py      # ShipAgent — per-ship asyncio task wrapper with done callbacks
│   ├── missions.py        # MissionType enum (TRADE, SCAN, CONTRACT, GATE_BUILD, IDLE) + registry
│   ├── system_intel.py    # System discovery + waypoint caching
│   └── _adapters.py       # Bridges all missions to fleet commander (trade, scan, contract, gate)
├── dashboard/             # CLI DASHBOARD — python -m spacetraders.dashboard
│   ├── __init__.py
│   ├── __main__.py    # Entry point
│   └── app.py         # Rich Live display, read-only DB queries
└── web/                   # PRIMARY INTERFACE
    ├── __main__.py    # Entry point: python -m spacetraders.web → uvicorn :8080
    ├── app.py         # FastAPI factory, lifespan (client lifecycle), template helpers
    ├── routes/
    │   ├── dashboard.py   # GET / — agent overview, fleet, contracts
    │   ├── fleet.py       # Ship list, detail, actions (orbit/dock/refuel/navigate/extract/survey)
    │   ├── contracts.py   # Contract detail, accept/deliver/fulfill
    │   ├── navigation.py  # System map, waypoint detail
    │   └── market.py      # Market data, buy/sell
    ├── templates/         # Jinja2: base.html + page templates + components/
    └── static/            # css/style.css (Pico + space theme), js/app.js (cooldowns, toasts)
```

---

## Key Files

| File | Purpose |
|------|---------|
| `README.md` | Project overview and setup |
| `docs/api-reference.md` | API endpoints and patterns |
| `docs/game-concepts.md` | Game mechanics reference |
| `docs/strategy.md` | Trading strategies and notes |
| `.env.example` | Required environment variables |
| `pyproject.toml` | Project configuration |
| `docs/crew-manifest.md` | Company lore, officer roster, titles, personalities |
| `src/spacetraders/fleet_registry.py` | Local ship registry — names, roles, categories, specs (source of truth when API is down) |
| `src/spacetraders/rate_limiter.py` | Cross-process shared rate limiter (SQLite-backed token bucket) |
| `src/spacetraders/data/fleet_db.py` | **Ship assignment registry** — SQLite-backed cross-process ship requisition. Prevents two missions from controlling the same ship. Functions: assign(), release(), release_dead(), available(category) |
| `src/spacetraders/missions/trader.py` | **Autonomous trader** — finds best EXPORT→IMPORT routes by profit/min, buys and sells in loops. Sells existing cargo on startup, handles cargo-full recovery. Run: `python -m spacetraders.missions.trader --continuous` |
| `src/spacetraders/missions/shipyard_scout.py` | One-shot probe mission to catalog all shipyards |
| `src/spacetraders/missions/drone_swarm.py` | Multi-drone mining swarm + shuttle hauler (4 drones mine, shuttle collects & sells) |
| `src/spacetraders/fleet/commander.py` | **Fleet Commander (Mission Control)** — event-driven orchestrator. Strategy engine decides assignments; events from missions drive re-evaluation. Run: `python -m spacetraders.fleet` |
| `src/spacetraders/fleet/events.py` | EventType enum + FleetEvent dataclass — typed event system that drives strategy re-evaluation |
| `src/spacetraders/fleet/strategy.py` | FleetStrategy — pure synchronous decision engine (no I/O). Priority: Gate > Contracts > Trade > Idle. CapitalPolicy thresholds. |
| `src/spacetraders/fleet/scheduler.py` | In-memory priority rate limiter (replaces SQLite limiter when running in-process) |
| `src/spacetraders/fleet/_adapters.py` | Bridges all missions (trade, scan, contract, gate_build) to fleet commander with event emission |
| `src/spacetraders/missions/contractor.py` | **Contract runner** — negotiate → buy → deliver → fulfill → repeat. Auto-picks ships from pool or use `--ship` override. Run: `python -m spacetraders.missions.contractor` |
| `src/spacetraders/missions/ops.py` | **One-shot ops**: fulfill contracts, negotiate new ones, buy ships, fleet status. Run: `python -m spacetraders.missions.ops <command>` |
| `src/spacetraders/data/operations_db.py` | **Operations ledger** — SQLite-backed trade/extraction/snapshot tracking across all processes |
| `src/spacetraders/dashboard/app.py` | **CLI dashboard** — Rich Live read-only display of fleet operations. Run: `python -m spacetraders.dashboard` |

> **Deep game knowledge:** For comprehensive game mechanics and strategy, load `SpaceTraders Rules.md` and `SpaceTraders Strategy.md` from the Obsidian vault at `/Users/alex/Documents/Obsidian/alexetc/Claude Context/`. These contain full rules reference and decision frameworks that any Claude session can use.

---

## API Conventions

- Base URL: `https://api.spacetraders.io/v2`
- All authenticated requests need `Authorization: Bearer <token>`
- Rate limit: 2 requests/second (burst allowed, details TBD)
- Pagination: `page` and `limit` query params, response includes `meta.total`
- Errors return `{ "error": { "message": "...", "code": 1234 } }`

---

## Game State

**Agent:** UTMOSTLY
**Faction:** COBALT (Cobalt Traders Alliance)
**Home system:** X1-XV5
**HQ:** X1-XV5-A1 (planet)

**Fleet:** See `src/spacetraders/fleet_registry.py` (ship specs, categories) and `data/fleet.db` (live assignments via `ops status`).

**Tokens:** Both account token and agent token stored in `.env` (gitignored).
Two token types:
- **Account token** (`sub: account-token`) - for registering agents after resets
- **Agent token** (`sub: agent-token`) - for all gameplay API calls

**Important:** The game resets weekly (Saturdays ~13:00 UTC). After a reset:
1. Log in at `my.spacetraders.io` (alex.etc@gmail.com / alex.etc)
2. Register a new agent (callsign + faction)
3. Copy new agent token into `.env`
4. Account token persists across resets

---

## Development Rules

- Follow global CLAUDE.md standards (type hints, Pydantic, pathlib, specific exceptions)
- Never hardcode tokens - always from environment
- All API responses validated through Pydantic models
- Rate limiter must be built into the client from day one
- Log all API calls at DEBUG level for troubleshooting

---

## Operational Notes

**Warnings:**
- **UTMOSTLY-1 unreachable** (2026-02-15): API returns error 3000 on all requests. Do not query or command — burns retries and rate limit.
- **FX5D (engineered asteroid) is STRIPPED** — do not mine.

**Shipyards:** 3 in system (A2, H59, C45). Run `shipyard_scout` for current prices. Light Hauler ~307K at A2. C45 has siphon drones.

**Next goal:** Find warp/jump drive modules (may need to explore other systems). Haulers have 2 free module slots.

**Running missions:**
```bash
cd /Users/alex/github/spacetraders

# FLEET COMMANDER — all ships in one process (probes scan, shuttles/haulers trade)
.venv/bin/python -m spacetraders.fleet
# Override assignment: .venv/bin/python -m spacetraders.fleet --assign UTMOSTLY-3:trade UTMOSTLY-9:idle

# --- Standalone scripts (auto-pick ships from pool, or use --ship to override) ---

# Mining mission (mine → deliver → refuel loop)
.venv/bin/python -m spacetraders.missions --ship UTMOSTLY-1 --resource COPPER_ORE

# Autonomous trader (auto-picks best available ship from pool)
.venv/bin/python -m spacetraders.missions.trader --continuous
# Options: --ship UTMOSTLY-3 (override), --loops N, --scout

# Probe market scanner (auto-picks all available probes)
.venv/bin/python -m spacetraders.missions.probe_scanner --continuous
# Options: --ship UTMOSTLY-2 (single), --ships UTMOSTLY-2 UTMOSTLY-4 (explicit)

# Shipyard scout (catalog shipyards)
.venv/bin/python -m spacetraders.missions.shipyard_scout --ship UTMOSTLY-4

# Contract runner (auto-picks ships based on contract size)
.venv/bin/python -m spacetraders.missions.contractor
# Options: --ship UTMOSTLY-9 --ship UTMOSTLY-3 (explicit override)

# Gate builder (auto-picks largest cargo ship)
.venv/bin/python -m spacetraders.missions.gate_builder
# Options: --ship UTMOSTLY-C (override), --floor 300000

# Deploy sentinels (marks ships as sentinel in assignment DB)
.venv/bin/python -m spacetraders.missions.deploy_sentinels

# Drone swarm (self-sufficient: each drone mines → sells at B7 → refuels → returns)
.venv/bin/python -m spacetraders.missions.drone_swarm --surveyor UTMOSTLY-D

# One-shot operations (contracts, ship purchases, status)
.venv/bin/python -m spacetraders.missions.ops fulfill        # Collect fulfilled contract payment
.venv/bin/python -m spacetraders.missions.ops negotiate SHIP  # Negotiate new contract
.venv/bin/python -m spacetraders.missions.ops buy-ship TYPE WP # Buy a ship
.venv/bin/python -m spacetraders.missions.ops status          # Fleet overview

# Operations dashboard (read-only, no API calls)
.venv/bin/python -m spacetraders.dashboard              # Live CLI dashboard
.venv/bin/python -m spacetraders.dashboard --refresh 5  # Faster refresh
```

---

## Recent Changes

| Date | Change |
|------|--------|
| 2026-02-15 | Project created, documentation scaffolded |
| 2026-02-15 | Agent UTMOSTLY registered, COBALT faction, game state documented |
| 2026-02-15 | Full TUI implemented: async client, Pydantic models, 6 screens, Textual app |
| 2026-02-15 | Web UI implemented: FastAPI + Jinja2 + htmx, 6 pages, all ship/market/contract actions |
| 2026-02-15 | Missions module: automated mining operations (runner, mining loop, fuel-aware router, asteroid scanner) |
| 2026-02-15 | Drone swarm module: 4 drones mine concurrently, shuttle hauler collects cargo & sells at best market |
| 2026-02-16 | Fleet Commander Phase 1: single-process orchestrator with priority scheduler, auto-assignment, crash supervision |
| 2026-02-16 | Multi-hop refueling: greedy pathfinder in router.py, navigate_multihop in runner.py, smarter fuel filter in trader.py (checks one-way legs, not round-trip). Unlocked distant routes like F56→C45 for shuttles. |
| 2026-02-16 | Purchased UTMOSTLY-E (Light Hauler, 80 cargo, 600 fuel) for 316K at A2. 3 traders running: UTMOSTLY-3, UTMOSTLY-9, UTMOSTLY-E |
| 2026-02-16 | Mining economics analysis: reactivated drone swarm at B7 cluster, bought Surveyor (UTMOSTLY-D), negotiated FERTILIZERS contract, added ops.py for one-shot operations, added survey support to drone_swarm.py |
| 2026-02-16 | Operations ledger + CLI dashboard: SQLite-backed trade/extraction/snapshot tracking (operations_db.py), instrumented trader, drone swarm, and fleet commander to record all transactions, Rich Live dashboard for monitoring |
| 2026-02-16 | Ship assignment system: fleet_db.py (SQLite requisition), category field on fleet_registry, auto-pick from pool in contractor/trader/scanner/gate_builder. `ops status` shows assignments. All missions `--ship` is now optional. |
| 2026-02-16 | **Mission Control**: Event-driven fleet commander. FleetStrategy decides assignments (Gate > Contracts > Trade > Idle). Events from missions (TRADE_COMPLETED, CONTRACT_FULFILLED, GATE_DELIVERY, etc.) trigger re-evaluation. CONTRACT + GATE_BUILD mission types. Deleted archived tui/. |
