# API Reference

Base URL: `https://api.spacetraders.io/v2`

All authenticated endpoints require: `Authorization: Bearer <token>`

Full OpenAPI spec: [api.spacetraders.io/v2/documentation/json](https://api.spacetraders.io/v2/documentation/json)

---

## Status

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/` | No | Server status, stats, leaderboard |

---

## Registration

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/register` | Account token | Register a new agent |

**Body:** `{ "symbol": "CALLSIGN", "faction": "COSMIC" }`

**Returns:** Agent details + bearer token for all future requests.

---

## Agent

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/my/agent` | Yes | Your agent info, credits, HQ |

---

## Contracts

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/my/contracts` | Yes | List your contracts |
| GET | `/my/contracts/{id}` | Yes | Contract details |
| POST | `/my/contracts/{id}/accept` | Yes | Accept a contract |
| POST | `/my/contracts/{id}/deliver` | Yes | Deliver contract goods |
| POST | `/my/contracts/{id}/fulfill` | Yes | Complete a contract |

---

## Fleet (Ships)

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/my/ships` | Yes | List your ships |
| GET | `/my/ships/{symbol}` | Yes | Ship details |
| POST | `/my/ships/{symbol}/orbit` | Yes | Move ship to orbit |
| POST | `/my/ships/{symbol}/dock` | Yes | Dock at waypoint |
| POST | `/my/ships/{symbol}/navigate` | Yes | Travel to waypoint |
| POST | `/my/ships/{symbol}/refuel` | Yes | Refuel ship |
| GET | `/my/ships/{symbol}/cargo` | Yes | View cargo |

---

## Mining & Extraction

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/my/ships/{symbol}/extract` | Yes | Extract resources |
| POST | `/my/ships/{symbol}/survey` | Yes | Survey for deposits |
| POST | `/my/ships/{symbol}/jettison` | Yes | Dump cargo |

---

## Trading

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/systems/{sys}/waypoints/{wp}/market` | Yes | Market data |
| POST | `/my/ships/{symbol}/purchase` | Yes | Buy goods |
| POST | `/my/ships/{symbol}/sell` | Yes | Sell goods |

---

## Navigation & Systems

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/systems` | Yes | List systems |
| GET | `/systems/{symbol}` | Yes | System details |
| GET | `/systems/{sys}/waypoints` | Yes | Waypoints in system |
| GET | `/systems/{sys}/waypoints/{wp}` | Yes | Waypoint details |

---

## Shipyard

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/systems/{sys}/waypoints/{wp}/shipyard` | Yes | Available ships |
| POST | `/my/ships` | Yes | Purchase a ship |

---

## Pagination

Paginated endpoints accept `page` (default 1) and `limit` (default 10, max 20) query params.

Response includes:
```json
{
  "data": [...],
  "meta": { "total": 100, "page": 1, "limit": 10 }
}
```

---

## Error Format

```json
{
  "error": {
    "message": "Description of what went wrong",
    "code": 1234,
    "data": {}
  }
}
```

---

## Notes

- Rate limits: ~2 req/sec (confirm exact limits from API headers)
- Game resets weekly - all tokens invalidated
- Market prices are dynamic and player-influenced
