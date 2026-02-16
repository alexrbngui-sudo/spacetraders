"""Mining and extraction API operations."""

from __future__ import annotations

from spacetraders.client import SpaceTradersClient
from spacetraders.models import Cooldown, Extraction, Survey


async def extract(
    client: SpaceTradersClient, ship_symbol: str, survey: Survey | None = None
) -> tuple[Extraction, Cooldown]:
    """Extract resources at current location. Optionally use a survey for better yields."""
    json_body = None
    if survey is not None:
        json_body = {
            "survey": {
                "signature": survey.signature,
                "symbol": survey.symbol,
                "deposits": [{"symbol": d.symbol} for d in survey.deposits],
                "expiration": survey.expiration.isoformat(),
                "size": survey.size,
            }
        }

    body = await client.post(f"/my/ships/{ship_symbol}/extract", json=json_body)
    data = body["data"]
    return (
        Extraction.model_validate(data["extraction"]),
        Cooldown.model_validate(data["cooldown"]),
    )


async def create_survey(
    client: SpaceTradersClient, ship_symbol: str
) -> tuple[list[Survey], Cooldown]:
    """Survey the current location for resource deposits."""
    body = await client.post(f"/my/ships/{ship_symbol}/survey")
    data = body["data"]
    surveys = [Survey.model_validate(s) for s in data["surveys"]]
    cooldown = Cooldown.model_validate(data["cooldown"])
    return surveys, cooldown
