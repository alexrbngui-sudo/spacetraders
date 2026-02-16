"""Agent API operations."""

from __future__ import annotations

from spacetraders.client import SpaceTradersClient
from spacetraders.models import Agent


async def get_agent(client: SpaceTradersClient) -> Agent:
    """Fetch the current agent's info."""
    body = await client.get("/my/agent")
    return Agent.model_validate(body["data"])
