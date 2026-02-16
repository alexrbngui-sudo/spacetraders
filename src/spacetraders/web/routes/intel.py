"""Intel route — asteroid yield stats from the shared database."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from spacetraders.data.asteroid_db import AsteroidDatabase
from spacetraders.web.app import render

router = APIRouter(prefix="/intel", tags=["intel"])


def _get_db(request: Request) -> AsteroidDatabase:
    return request.app.state.asteroid_db


@router.get("", response_class=HTMLResponse)
async def intel_overview(request: Request) -> HTMLResponse:
    """Full yield stats table — all asteroids, all resources."""
    db = _get_db(request)
    records = db.get_all_stats()
    resources = db.get_resources()
    return render(request, "intel.html", {
        "records": records,
        "resources": resources,
        "active_nav": "intel",
        "selected_resource": None,
    })


@router.get("/resource/{symbol}", response_class=HTMLResponse)
async def intel_by_resource(request: Request, symbol: str) -> HTMLResponse:
    """Yield stats filtered by resource."""
    db = _get_db(request)
    records = db.get_resource_stats(symbol)
    resources = db.get_resources()
    return render(request, "intel.html", {
        "records": records,
        "resources": resources,
        "active_nav": "intel",
        "selected_resource": symbol,
    })
