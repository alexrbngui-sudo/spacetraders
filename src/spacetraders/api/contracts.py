"""Contract API operations."""

from __future__ import annotations

from typing import Any

from spacetraders.client import SpaceTradersClient
from spacetraders.models import Contract


async def list_contracts(client: SpaceTradersClient) -> list[Contract]:
    """Fetch all contracts."""
    items, _ = await client.get_paginated("/my/contracts")
    return [Contract.model_validate(c) for c in items]


async def get_contract(client: SpaceTradersClient, contract_id: str) -> Contract:
    """Fetch a single contract."""
    body = await client.get(f"/my/contracts/{contract_id}")
    return Contract.model_validate(body["data"])


async def accept_contract(client: SpaceTradersClient, contract_id: str) -> Contract:
    """Accept a contract."""
    body = await client.post(f"/my/contracts/{contract_id}/accept")
    return Contract.model_validate(body["data"]["contract"])


async def deliver_contract(
    client: SpaceTradersClient,
    contract_id: str,
    ship_symbol: str,
    trade_symbol: str,
    units: int,
) -> dict[str, Any]:
    """Deliver goods for a contract."""
    body = await client.post(
        f"/my/contracts/{contract_id}/deliver",
        json={
            "shipSymbol": ship_symbol,
            "tradeSymbol": trade_symbol,
            "units": units,
        },
    )
    return body["data"]


async def fulfill_contract(client: SpaceTradersClient, contract_id: str) -> Contract:
    """Fulfill a completed contract to collect payment."""
    body = await client.post(f"/my/contracts/{contract_id}/fulfill")
    return Contract.model_validate(body["data"]["contract"])


async def negotiate_contract(
    client: SpaceTradersClient, ship_symbol: str,
) -> Contract:
    """Negotiate a new contract. Ship must be docked at a faction HQ."""
    body = await client.post(f"/my/ships/{ship_symbol}/negotiate/contract")
    return Contract.model_validate(body["data"]["contract"])
