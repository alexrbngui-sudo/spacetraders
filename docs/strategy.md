# Strategy Notes

Living document for trading strategies, observations, and gameplay notes.

---

## Early Game Plan

1. ~~Register agent, inspect starting ship and contract~~ ✓
2. ~~Understand local system layout (waypoints, markets, asteroids)~~ ✓
3. Accept and fulfill first contract ← **current objective**
4. Identify profitable trade routes in home system
5. Save for second ship

---

## Trade Route Log

| Route | Buy | Sell | Profit/Unit | Notes |
|-------|-----|------|-------------|-------|
| _TBD_ | | | | |

---

## Mining Spots

| Location | Resources | Yield | Notes |
|----------|-----------|-------|-------|
| _TBD_ | | | |

---

## Observations

_Record interesting findings during gameplay here._

---

## Needs Testing

_Built but not yet run against live API._

- **Mining mission runner** — full mine→deliver→refuel loop (`python -m spacetraders.missions`)
- **Sell junk at marketplace** — sells byproducts instead of jettisoning at asteroid bases with markets
- **Market intelligence cache** — records prices at every marketplace visit to `data/markets.db`
- **Probe market scanner** — sends UTMOSTLY-2 to all marketplaces (`python -m spacetraders.missions.probe_scanner`)
- **Trade route calculator** — offline profit finder from cached data (`python -m spacetraders.missions.trade_routes`)
- **Contract auto-negotiation** — runner auto-negotiates next contract after fulfilling current one

## Automation Backlog

- Multi-ship fleet coordination — orchestrator above runner.py
- Second ship purchase — mining drone stationed at asteroid belt
- Fuel siphoning from gas giants — free fuel via gas siphon mount → sell at markets
