"""Market routes — trade goods, buy/sell."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from spacetraders.api import fleet as fleet_api
from spacetraders.api import navigation as nav_api
from spacetraders.client import ApiError
from spacetraders.web.app import get_client, render

router = APIRouter(prefix="/market")

_AGENT_OOB = (
    '<div id="agent-header" hx-swap-oob="innerHTML"'
    ' hx-get="/agent" hx-trigger="load"></div>'
)


def _toast(message: str, *, success: bool = True) -> str:
    kind = "success" if success else "error"
    return (
        f'<div id="action-toast" hx-swap-oob="innerHTML">'
        f'<div class="toast toast-{kind}">{message}</div>'
        f'</div>'
    )


@router.get("/{system_symbol}/{waypoint_symbol}", response_class=HTMLResponse)
async def market_view(
    request: Request, system_symbol: str, waypoint_symbol: str
) -> HTMLResponse:
    """Full page: market data at a waypoint."""
    client = get_client(request)
    market = await nav_api.get_market(client, system_symbol, waypoint_symbol)
    ships = await fleet_api.list_ships(client)
    # Only ships docked at this waypoint can trade
    local_ships = [s for s in ships if s.nav.waypoint_symbol == waypoint_symbol]

    return render(request, "market.html", {
        "market": market,
        "system_symbol": system_symbol,
        "waypoint_symbol": waypoint_symbol,
        "local_ships": local_ships,
        "active_nav": "system",
    })


@router.post("/{system_symbol}/{waypoint_symbol}/buy", response_class=HTMLResponse)
async def do_buy(
    request: Request,
    system_symbol: str,
    waypoint_symbol: str,
    ship_symbol: str = Form(...),
    trade_symbol: str = Form(...),
    units: int = Form(...),
) -> HTMLResponse:
    """Buy goods at market."""
    client = get_client(request)
    try:
        data = await fleet_api.purchase_cargo(client, ship_symbol, trade_symbol, units)
        total = data.get("transaction", {}).get("totalPrice", 0)
        agent_oob = _AGENT_OOB
        return HTMLResponse(
            _toast(f"Bought {units} {trade_symbol} for {total:,} credits") + agent_oob
        )
    except ApiError as e:
        return HTMLResponse(_toast(str(e), success=False))


@router.post("/{system_symbol}/{waypoint_symbol}/sell", response_class=HTMLResponse)
async def do_sell(
    request: Request,
    system_symbol: str,
    waypoint_symbol: str,
    ship_symbol: str = Form(...),
    trade_symbol: str = Form(...),
    units: int = Form(...),
) -> HTMLResponse:
    """Sell goods at market."""
    client = get_client(request)
    try:
        data = await fleet_api.sell_cargo(client, ship_symbol, trade_symbol, units)
        total = data.get("transaction", {}).get("totalPrice", 0)
        agent_oob = _AGENT_OOB
        return HTMLResponse(
            _toast(f"Sold {units} {trade_symbol} for {total:,} credits") + agent_oob
        )
    except ApiError as e:
        return HTMLResponse(_toast(str(e), success=False))
