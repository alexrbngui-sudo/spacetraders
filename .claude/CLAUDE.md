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
├── client.py          # HTTP client, auth, rate limiting
├── models.py          # Pydantic models for API responses
├── config.py          # Settings from .env
├── agent.py           # Agent registration and status
├── navigation.py      # Systems, waypoints, travel
├── trading.py         # Market data, buy/sell logic
├── contracts.py       # Contract negotiation and fulfillment
├── mining.py          # Asteroid extraction
└── fleet.py           # Ship management
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

---

## API Conventions

- Base URL: `https://api.spacetraders.io/v2`
- All authenticated requests need `Authorization: Bearer <token>`
- Rate limit: 2 requests/second (burst allowed, details TBD)
- Pagination: `page` and `limit` query params, response includes `meta.total`
- Errors return `{ "error": { "message": "...", "code": 1234 } }`

---

## Game State

**Important:** The game resets weekly. After a reset:
1. Generate a new account token at `my.spacetraders.io`
2. Re-register the agent via POST `/register`
3. Update `.env` with the new agent token

---

## Development Rules

- Follow global CLAUDE.md standards (type hints, Pydantic, pathlib, specific exceptions)
- Never hardcode tokens - always from environment
- All API responses validated through Pydantic models
- Rate limiter must be built into the client from day one
- Log all API calls at DEBUG level for troubleshooting

---

## Recent Changes

| Date | Change |
|------|--------|
| 2026-02-15 | Project created, documentation scaffolded |
