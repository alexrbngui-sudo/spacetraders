"""Contract routes — detail, accept, deliver, fulfill."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from spacetraders.api import contracts as contracts_api
from spacetraders.api import fleet as fleet_api
from spacetraders.client import ApiError
from spacetraders.web.app import get_client, render

router = APIRouter(prefix="/contracts")

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


@router.get("/{contract_id}", response_class=HTMLResponse)
async def contract_detail(request: Request, contract_id: str) -> HTMLResponse:
    """Full page: contract detail."""
    client = get_client(request)
    contract = await contracts_api.get_contract(client, contract_id)
    ships = await fleet_api.list_ships(client)

    return render(request, "contract_detail.html", {
        "contract": contract,
        "ships": ships,
    })


@router.post("/{contract_id}/accept", response_class=HTMLResponse)
async def do_accept(request: Request, contract_id: str) -> HTMLResponse:
    """Accept a contract."""
    client = get_client(request)
    try:
        contract = await contracts_api.accept_contract(client, contract_id)
        ships = await fleet_api.list_ships(client)
        page_html = render(request, "contract_detail.html", {
            "contract": contract,
            "ships": ships,
        }).body.decode()
        agent_oob = _AGENT_OOB
        payment = contract.terms.payment.on_accepted
        return HTMLResponse(
            page_html + _toast(f"Contract accepted! +{payment:,} credits") + agent_oob
        )
    except ApiError as e:
        return HTMLResponse(_toast(str(e), success=False))


@router.post("/{contract_id}/deliver", response_class=HTMLResponse)
async def do_deliver(
    request: Request,
    contract_id: str,
    ship_symbol: str = Form(...),
    trade_symbol: str = Form(...),
    units: int = Form(...),
) -> HTMLResponse:
    """Deliver goods for a contract."""
    client = get_client(request)
    try:
        await contracts_api.deliver_contract(
            client, contract_id, ship_symbol, trade_symbol, units
        )
        contract = await contracts_api.get_contract(client, contract_id)
        ships = await fleet_api.list_ships(client)
        page_html = render(request, "contract_detail.html", {
            "contract": contract,
            "ships": ships,
        }).body.decode()
        return HTMLResponse(
            page_html + _toast(f"Delivered {units} {trade_symbol}")
        )
    except ApiError as e:
        return HTMLResponse(_toast(str(e), success=False))


@router.post("/{contract_id}/fulfill", response_class=HTMLResponse)
async def do_fulfill(request: Request, contract_id: str) -> HTMLResponse:
    """Fulfill a completed contract."""
    client = get_client(request)
    try:
        contract = await contracts_api.fulfill_contract(client, contract_id)
        ships = await fleet_api.list_ships(client)
        page_html = render(request, "contract_detail.html", {
            "contract": contract,
            "ships": ships,
        }).body.decode()
        agent_oob = _AGENT_OOB
        payment = contract.terms.payment.on_fulfilled
        return HTMLResponse(
            page_html + _toast(f"Contract fulfilled! +{payment:,} credits") + agent_oob
        )
    except ApiError as e:
        return HTMLResponse(_toast(str(e), success=False))
