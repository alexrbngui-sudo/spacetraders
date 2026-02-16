"""Navigation routes — system map and waypoint detail."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from spacetraders.api import agent as agent_api
from spacetraders.api import contracts as contracts_api
from spacetraders.api import fleet as fleet_api
from spacetraders.api import navigation as nav_api
from spacetraders.models import system_symbol_from_waypoint
from spacetraders.web.app import get_client, render
from spacetraders.web.map_helpers import DeliveryInfo, compute_map_layout

router = APIRouter()


def _active_delivery_waypoints(contracts: list) -> dict[str, list[DeliveryInfo]]:
    """Extract delivery waypoints from accepted, unfulfilled contracts."""
    deliveries: dict[str, list[DeliveryInfo]] = {}
    for c in contracts:
        if not c.accepted or c.fulfilled:
            continue
        for d in c.terms.deliver:
            if d.units_fulfilled >= d.units_required:
                continue
            info = DeliveryInfo(
                contract_id=c.id,
                trade_symbol=d.trade_symbol,
                units_required=d.units_required,
                units_fulfilled=d.units_fulfilled,
            )
            deliveries.setdefault(d.destination_symbol, []).append(info)
    return deliveries


@router.get("/system", response_class=HTMLResponse)
async def system_home(request: Request) -> HTMLResponse:
    """System map for the agent's home system."""
    client = get_client(request)
    agent = await agent_api.get_agent(client)
    system_symbol = system_symbol_from_waypoint(agent.headquarters)
    waypoints = await nav_api.list_waypoints(client, system_symbol)
    waypoints.sort(key=lambda w: (w.type.value, w.symbol))

    ships = await fleet_api.list_ships(client)
    system_ships = [s for s in ships if s.nav.system_symbol == system_symbol]
    contracts = await contracts_api.list_contracts(client)
    delivery_wps = _active_delivery_waypoints(contracts)

    layout = compute_map_layout(
        waypoints, system_ships,
        hq_symbol=agent.headquarters,
        delivery_waypoints=delivery_wps,
    )

    return render(request, "system_map.html", {
        "system_symbol": system_symbol,
        "waypoints": waypoints,
        "layout": layout,
        "last_updated": datetime.now(timezone.utc),
        "active_nav": "system",
    })


@router.get("/system/{system_symbol}", response_class=HTMLResponse)
async def system_detail(request: Request, system_symbol: str) -> HTMLResponse:
    """System map for a specific system."""
    client = get_client(request)
    agent = await agent_api.get_agent(client)
    waypoints = await nav_api.list_waypoints(client, system_symbol)
    waypoints.sort(key=lambda w: (w.type.value, w.symbol))

    ships = await fleet_api.list_ships(client)
    system_ships = [s for s in ships if s.nav.system_symbol == system_symbol]
    contracts = await contracts_api.list_contracts(client)
    delivery_wps = _active_delivery_waypoints(contracts)

    layout = compute_map_layout(
        waypoints, system_ships,
        hq_symbol=agent.headquarters,
        delivery_waypoints=delivery_wps,
    )

    return render(request, "system_map.html", {
        "system_symbol": system_symbol,
        "waypoints": waypoints,
        "layout": layout,
        "last_updated": datetime.now(timezone.utc),
        "active_nav": "system",
    })


@router.get("/system/{system_symbol}/{waypoint_symbol}", response_class=HTMLResponse)
async def waypoint_detail(
    request: Request, system_symbol: str, waypoint_symbol: str
) -> HTMLResponse:
    """Waypoint detail — shows traits, links to market."""
    client = get_client(request)
    waypoint = await nav_api.get_waypoint(client, system_symbol, waypoint_symbol)
    has_market = any(t.symbol == "MARKETPLACE" for t in waypoint.traits)
    has_shipyard = any(t.symbol == "SHIPYARD" for t in waypoint.traits)

    return render(request, "waypoint_detail.html", {
        "waypoint": waypoint,
        "system_symbol": system_symbol,
        "has_market": has_market,
        "has_shipyard": has_shipyard,
        "active_nav": "system",
    })
