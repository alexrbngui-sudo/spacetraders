"""Dashboard route — GET / shows agent overview, fleet summary, contracts."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from spacetraders.api import agent as agent_api
from spacetraders.api import contracts as contracts_api
from spacetraders.api import fleet as fleet_api
from spacetraders.client import ApiError
from spacetraders.web.app import get_client, render

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Main dashboard with agent info, fleet, and contracts."""
    client = get_client(request)
    agent = await agent_api.get_agent(client)
    ships = await fleet_api.list_ships(client)
    contracts = await contracts_api.list_contracts(client)

    return render(request, "dashboard.html", {
        "agent": agent,
        "ships": ships,
        "contracts": contracts,
        "active_nav": "dashboard",
    })


@router.get("/agent", response_class=HTMLResponse)
async def agent_header(request: Request) -> HTMLResponse:
    """Partial: agent header bar (htmx target)."""
    client = get_client(request)
    try:
        agent = await agent_api.get_agent(client)
    except ApiError:
        return HTMLResponse('<div class="agent-bar text-red">Failed to load agent data</div>')

    return render(request, "components/agent_header.html", {"agent": agent})
