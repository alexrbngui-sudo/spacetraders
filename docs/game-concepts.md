# Game Concepts

Reference for SpaceTraders game mechanics. Updated as we learn more through gameplay.

---

## Agents

Every player controls one agent. An agent has:
- **Callsign** - unique identifier (e.g., `BADGER`)
- **Credits** - currency for buying ships, fuel, goods
- **Headquarters** - starting waypoint in your faction's home system
- **Starting resources** - 1 command ship, 175,000 credits, 1 contract

---

## Factions

NPC organizations that control regions of space. Key factions:

| Faction | Notes |
|---------|-------|
| COSMIC | Default starting faction |
| VOID | TBD |
| GALACTIC | TBD |
| QUANTUM | TBD |
| DOMINION | TBD |

Factions issue contracts and control territories. Better reputation = better contracts.

---

## Systems & Waypoints

The universe is organized hierarchically:

```
Sector (X1) → Systems (X1-AB12) → Waypoints (X1-AB12-C34)
```

- **Sectors** - largest division, named with letter+number
- **Systems** - star systems containing waypoints, identified by sector prefix
- **Waypoints** - locations within a system (planets, asteroids, stations, etc.)

### Waypoint Types

| Type | What's There |
|------|-------------|
| PLANET | Markets, shipyards, faction HQs |
| MOON | Markets, resources |
| ASTEROID | Mining targets |
| ASTEROID_FIELD | Dense mining area |
| ORBITAL_STATION | Markets, services |
| JUMP_GATE | Inter-system travel |
| GAS_GIANT | Fuel sources |
| ENGINEERED_ASTEROID | Special resources |

---

## Navigation

Ships travel between waypoints within a system. Key concepts:

- **Flight modes** affect speed and fuel consumption
- **Fuel** - consumed during travel, refuel at stations/planets
- Ships must be in **ORBIT** to navigate (not docked)
- Inter-system travel requires **jump gates** or **warp drives**

### Flight Modes

| Mode | Speed | Fuel | Use Case |
|------|-------|------|----------|
| CRUISE | Normal | Normal | Default travel |
| DRIFT | Slow | Minimal | Fuel conservation |
| BURN | Fast | High | Time-sensitive |
| STEALTH | Normal | Normal | Avoiding detection |

---

## Markets & Trading

Markets exist at certain waypoints. Prices are dynamic and player-influenced.

**Core loop:**
1. Check market data at waypoint A
2. Find goods that sell for more at waypoint B
3. Buy low, travel, sell high

**Supply/Demand:** Player activity affects prices. Buying drives prices up, selling drives them down.

**Trade goods** include fuel, metals, food, technology, luxury items, and more.

---

## Contracts

Factions issue contracts with deadlines. Types include:

- **Procurement** - deliver specific goods to a location
- **Transport** - move items between waypoints
- **Shuttle** - passenger transport

**Workflow:**
1. View available contracts (`GET /my/contracts`)
2. Accept a contract (`POST /my/contracts/{id}/accept`)
3. Gather/buy required goods
4. Deliver to destination (`POST /my/contracts/{id}/deliver`)
5. Fulfill to collect payment (`POST /my/contracts/{id}/fulfill`)

Missing deadlines costs reputation.

---

## Mining

Extract resources from asteroids.

**Workflow:**
1. Navigate to an asteroid field
2. Survey for deposits (optional, improves yield)
3. Extract resources
4. Sell at market or deliver for contracts

Extraction has a cooldown between attempts.

---

## Ships

Ships are your primary tools. Each has:

- **Frame** - hull type (determines modules/mounts)
- **Reactor** - power generation
- **Engine** - speed and fuel efficiency
- **Modules** - internal equipment (cargo holds, refineries)
- **Mounts** - external equipment (mining lasers, weapons)
- **Crew** - affects performance and morale
- **Cargo** - goods being carried (limited by capacity)
- **Fuel** - consumed during travel

### Ship Types (Partial)

| Type | Role |
|------|------|
| SHIP_PROBE | Cheap scout, no crew |
| SHIP_MINING_DRONE | Basic mining |
| SHIP_LIGHT_HAULER | Small cargo runs |
| SHIP_COMMAND_FRIGATE | Starting ship, versatile |
| SHIP_HEAVY_FREIGHTER | Large cargo capacity |

---

## Game Loop Summary

```
Register → Accept Contract → Mine/Trade → Deliver → Earn Credits → Buy Ships → Scale Up
```

The early game is about fulfilling your first contract to earn credits, then expanding your fleet to run multiple operations simultaneously.

---

## Weekly Resets

- Game resets every Saturday ~13:00 UTC
- All progress wiped (agents, ships, credits)
- Must re-register with a new token
- Supporters can reserve callsigns between resets
