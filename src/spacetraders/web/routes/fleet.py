"""Fleet routes — ship list, ship detail, and all ship actions."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from spacetraders.api import fleet as fleet_api
from spacetraders.api import mining as mining_api
from spacetraders.client import ApiError
from spacetraders.web.app import get_client, render

router = APIRouter(prefix="/fleet")

# OOB swap that triggers an agent header refresh after credit-changing actions
_AGENT_OOB = (
    '<div id="agent-header" hx-swap-oob="innerHTML"'
    ' hx-get="/agent" hx-trigger="load"></div>'
)


def _toast(message: str, *, success: bool = True) -> str:
    """Generate an OOB toast HTML fragment."""
    kind = "success" if success else "error"
    return (
        f'<div id="action-toast" hx-swap-oob="innerHTML">'
        f'<div class="toast toast-{kind}">{message}</div>'
        f'</div>'
    )


def _cooldown_html(cooldown: object, ship_symbol: str) -> str:
    """Generate cooldown timer HTML if cooldown is active."""
    remaining = getattr(cooldown, "remaining_seconds", 0)
    total = getattr(cooldown, "total_seconds", 0)
    if remaining <= 0:
        return ""
    pct = (remaining / total * 100) if total > 0 else 0
    return (
        f'<div id="cooldown-slot" hx-swap-oob="innerHTML">'
        f'<div class="cooldown" data-cooldown-seconds="{remaining}" '
        f'data-cooldown-total="{total}" data-ship="{ship_symbol}">'
        f'<span>Cooldown:</span>'
        f'<div class="cooldown-bar">'
        f'<div class="cooldown-fill" style="width: {pct}%"></div>'
        f'</div>'
        f'<span class="cooldown-text">{remaining}s</span>'
        f'</div></div>'
    )


# --- Page routes ---


@router.get("", response_class=HTMLResponse)
async def fleet_list(request: Request) -> HTMLResponse:
    """Full page: all ships."""
    client = get_client(request)
    ships = await fleet_api.list_ships(client)
    return render(request, "fleet.html", {"ships": ships, "active_nav": "fleet"})


@router.get("/{ship_symbol}", response_class=HTMLResponse)
async def ship_detail(request: Request, ship_symbol: str) -> HTMLResponse:
    """Full page: single ship detail."""
    client = get_client(request)
    ship = await fleet_api.get_ship(client, ship_symbol)
    return render(request, "ship_detail.html", {"ship": ship, "active_nav": "fleet"})


# --- Partial routes (htmx targets) ---


@router.get("/{ship_symbol}/status", response_class=HTMLResponse)
async def ship_status(request: Request, ship_symbol: str) -> HTMLResponse:
    """Partial: ship status panel."""
    client = get_client(request)
    ship = await fleet_api.get_ship(client, ship_symbol)
    return render(request, "components/ship_status.html", {"ship": ship})


# --- Action routes ---


@router.post("/{ship_symbol}/orbit", response_class=HTMLResponse)
async def do_orbit(request: Request, ship_symbol: str) -> HTMLResponse:
    """Orbit action — returns status partial + toast."""
    client = get_client(request)
    try:
        await fleet_api.orbit(client, ship_symbol)
        ship = await fleet_api.get_ship(client, ship_symbol)
        status_html = render(request, "components/ship_status.html", {"ship": ship}).body.decode()
        return HTMLResponse(status_html + _toast(f"{ship_symbol} moved to orbit"))
    except ApiError as e:
        ship = await fleet_api.get_ship(client, ship_symbol)
        status_html = render(request, "components/ship_status.html", {"ship": ship}).body.decode()
        return HTMLResponse(status_html + _toast(str(e), success=False))


@router.post("/{ship_symbol}/dock", response_class=HTMLResponse)
async def do_dock(request: Request, ship_symbol: str) -> HTMLResponse:
    """Dock action."""
    client = get_client(request)
    try:
        await fleet_api.dock(client, ship_symbol)
        ship = await fleet_api.get_ship(client, ship_symbol)
        status_html = render(request, "components/ship_status.html", {"ship": ship}).body.decode()
        return HTMLResponse(status_html + _toast(f"{ship_symbol} docked"))
    except ApiError as e:
        ship = await fleet_api.get_ship(client, ship_symbol)
        status_html = render(request, "components/ship_status.html", {"ship": ship}).body.decode()
        return HTMLResponse(status_html + _toast(str(e), success=False))


@router.post("/{ship_symbol}/refuel", response_class=HTMLResponse)
async def do_refuel(request: Request, ship_symbol: str) -> HTMLResponse:
    """Refuel action."""
    client = get_client(request)
    try:
        data = await fleet_api.refuel(client, ship_symbol)
        ship = await fleet_api.get_ship(client, ship_symbol)
        cost = data.get("transaction", {}).get("totalPrice", 0)
        status_html = render(request, "components/ship_status.html", {"ship": ship}).body.decode()
        agent_oob = _AGENT_OOB
        return HTMLResponse(
            status_html + _toast(f"Refueled for {cost:,} credits") + agent_oob
        )
    except ApiError as e:
        ship = await fleet_api.get_ship(client, ship_symbol)
        status_html = render(request, "components/ship_status.html", {"ship": ship}).body.decode()
        return HTMLResponse(status_html + _toast(str(e), success=False))


@router.post("/{ship_symbol}/navigate", response_class=HTMLResponse)
async def do_navigate(
    request: Request, ship_symbol: str, waypoint: str = Form(...)
) -> HTMLResponse:
    """Navigate to a waypoint."""
    client = get_client(request)
    try:
        await fleet_api.navigate(client, ship_symbol, waypoint)
        ship = await fleet_api.get_ship(client, ship_symbol)
        status_html = render(request, "components/ship_status.html", {"ship": ship}).body.decode()
        return HTMLResponse(
            status_html + _toast(f"Navigating to {waypoint}")
        )
    except ApiError as e:
        ship = await fleet_api.get_ship(client, ship_symbol)
        status_html = render(request, "components/ship_status.html", {"ship": ship}).body.decode()
        return HTMLResponse(status_html + _toast(str(e), success=False))


@router.post("/{ship_symbol}/extract", response_class=HTMLResponse)
async def do_extract(request: Request, ship_symbol: str) -> HTMLResponse:
    """Extract resources."""
    client = get_client(request)
    try:
        extraction, cooldown = await mining_api.extract(client, ship_symbol)
        ship = await fleet_api.get_ship(client, ship_symbol)
        status_html = render(request, "components/ship_status.html", {"ship": ship}).body.decode()
        cargo_oob = (
            '<div id="cargo-panel" hx-swap-oob="innerHTML">'
            + render(request, "components/cargo_table.html", {"ship": ship}).body.decode()
            + '</div>'
        )
        cooldown_html = _cooldown_html(cooldown, ship_symbol)
        msg = f"Extracted {extraction.yield_.units} {extraction.yield_.symbol}"
        return HTMLResponse(status_html + _toast(msg) + cargo_oob + cooldown_html)
    except ApiError as e:
        ship = await fleet_api.get_ship(client, ship_symbol)
        status_html = render(request, "components/ship_status.html", {"ship": ship}).body.decode()
        return HTMLResponse(status_html + _toast(str(e), success=False))


@router.post("/{ship_symbol}/survey", response_class=HTMLResponse)
async def do_survey(request: Request, ship_symbol: str) -> HTMLResponse:
    """Create a survey."""
    client = get_client(request)
    try:
        surveys, cooldown = await mining_api.create_survey(client, ship_symbol)
        ship = await fleet_api.get_ship(client, ship_symbol)
        status_html = render(request, "components/ship_status.html", {"ship": ship}).body.decode()
        deposits = ", ".join(d.symbol for s in surveys for d in s.deposits)
        cooldown_html = _cooldown_html(cooldown, ship_symbol)
        msg = f"Survey found: {deposits}"
        return HTMLResponse(status_html + _toast(msg) + cooldown_html)
    except ApiError as e:
        ship = await fleet_api.get_ship(client, ship_symbol)
        status_html = render(request, "components/ship_status.html", {"ship": ship}).body.decode()
        return HTMLResponse(status_html + _toast(str(e), success=False))


@router.post("/{ship_symbol}/jettison", response_class=HTMLResponse)
async def do_jettison(
    request: Request,
    ship_symbol: str,
    symbol: str = Form(...),
    units: int = Form(...),
) -> HTMLResponse:
    """Jettison cargo."""
    client = get_client(request)
    try:
        await fleet_api.jettison_cargo(client, ship_symbol, symbol, units)
        ship = await fleet_api.get_ship(client, ship_symbol)
        cargo_html = render(request, "components/cargo_table.html", {"ship": ship}).body.decode()
        status_oob = (
            '<div id="ship-status-panel" hx-swap-oob="innerHTML">'
            + render(request, "components/ship_status.html", {"ship": ship}).body.decode()
            + '</div>'
        )
        return HTMLResponse(cargo_html + _toast(f"Jettisoned {units} {symbol}") + status_oob)
    except ApiError as e:
        ship = await fleet_api.get_ship(client, ship_symbol)
        cargo_html = render(request, "components/cargo_table.html", {"ship": ship}).body.decode()
        return HTMLResponse(cargo_html + _toast(str(e), success=False))
