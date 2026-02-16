"""FastAPI application factory with lifespan management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from spacetraders.client import SpaceTradersClient
from spacetraders.config import load_settings
from spacetraders.data.asteroid_db import AsteroidDatabase

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create and tear down the shared SpaceTradersClient."""
    settings = load_settings()
    client = SpaceTradersClient(settings)
    asteroid_db = AsteroidDatabase(db_path=settings.data_dir / "asteroids.db")
    app.state.client = client
    app.state.settings = settings
    app.state.asteroid_db = asteroid_db
    yield
    asteroid_db.close()
    await client.close()


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    app = FastAPI(title="SpaceTraders", lifespan=lifespan)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    app.state.templates = templates

    # Register routes
    from spacetraders.web.routes import contracts, dashboard, fleet, intel, market, navigation

    app.include_router(dashboard.router)
    app.include_router(fleet.router)
    app.include_router(navigation.router)
    app.include_router(market.router)
    app.include_router(contracts.router)
    app.include_router(intel.router)

    return app


def get_client(request: Request) -> SpaceTradersClient:
    """Extract the shared client from app state."""
    return request.app.state.client


def render(
    request: Request,
    template: str,
    context: dict | None = None,
    *,
    block: str | None = None,
) -> HTMLResponse:
    """Render a Jinja2 template. If block is given, render only that block (for partials)."""
    ctx = {"request": request}
    if context:
        ctx.update(context)
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(request, template, ctx)
