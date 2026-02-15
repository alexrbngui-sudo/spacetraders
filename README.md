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

> The game resets weekly (Saturdays). You'll need to re-register and get a new token after each reset.

## API

- **Base URL:** `https://api.spacetraders.io/v2`
- **Docs:** [docs.spacetraders.io](https://docs.spacetraders.io)
- **OpenAPI Spec:** [api.spacetraders.io/v2/documentation/json](https://api.spacetraders.io/v2/documentation/json)

## Project Structure

```
src/spacetraders/
├── client.py          # HTTP client with auth and rate limiting
├── models.py          # Pydantic models for API responses
├── config.py          # Settings from .env
├── agent.py           # Agent registration and status
├── navigation.py      # Systems, waypoints, travel
├── trading.py         # Market data, buy/sell
├── contracts.py       # Contract management
├── mining.py          # Resource extraction
└── fleet.py           # Ship management

docs/
├── api-reference.md   # Endpoint reference
├── game-concepts.md   # Game mechanics
└── strategy.md        # Trading strategies and notes
```

## Current Game Stats

As of 2026-02-15:
- 2,873 player accounts
- 6,995 explored systems / 200,685 waypoints
- Next reset: February 22, 2026 at 13:00 UTC
