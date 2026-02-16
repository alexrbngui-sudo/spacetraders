"""Local ship registry — names, roles, and specs for the fleet.

The API can be unreliable, so this provides a local source of truth for
ship metadata. Nicknames are used in log output instead of formal designations.

Update this file when ships are purchased or the game resets.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShipRecord:
    """Local record of a ship in the fleet."""

    symbol: str
    name: str           # nickname for logs
    role: str           # functional role
    category: str = "ship"  # probe | ship | sentinel | disabled
    cargo: int = 0      # cargo capacity
    fuel: int = 0       # fuel capacity (0 = solar)
    notes: str = ""


FLEET: dict[str, ShipRecord] = {
    "UTMOSTLY-1": ShipRecord(
        symbol="UTMOSTLY-1",
        name="Frigate",
        role="Command Frigate",
        category="disabled",
        cargo=40, fuel=300,
        notes="Mining laser, gas siphon, surveyor, sensors. Captained by Rafiq Thomas.",
    ),
    "UTMOSTLY-2": ShipRecord(
        symbol="UTMOSTLY-2",
        name="Probe 1",
        role="Probe",
        category="probe",
        cargo=0, fuel=0,
        notes="Solar-powered scout. Unmanned.",
    ),
    "UTMOSTLY-4": ShipRecord(
        symbol="UTMOSTLY-4",
        name="Probe 2",
        role="Probe",
        category="probe",
        cargo=0, fuel=0,
        notes="Solar-powered scout. Unmanned.",
    ),
    "UTMOSTLY-5": ShipRecord(
        symbol="UTMOSTLY-5",
        name="Drone 1",
        role="Mining Drone",
        category="sentinel",
        cargo=15, fuel=80,
        notes="Mining laser. Purchased 2026-02-15 at H59.",
    ),
    "UTMOSTLY-6": ShipRecord(
        symbol="UTMOSTLY-6",
        name="Drone 2",
        role="Mining Drone",
        category="sentinel",
        cargo=15, fuel=80,
        notes="Mining laser. Purchased 2026-02-15 at H59.",
    ),
    "UTMOSTLY-7": ShipRecord(
        symbol="UTMOSTLY-7",
        name="Drone 3",
        role="Mining Drone",
        category="sentinel",
        cargo=15, fuel=80,
        notes="Mining laser. Purchased 2026-02-15 at H59.",
    ),
    "UTMOSTLY-8": ShipRecord(
        symbol="UTMOSTLY-8",
        name="Drone 4",
        role="Mining Drone",
        category="sentinel",
        cargo=15, fuel=80,
        notes="Mining laser. Purchased 2026-02-15 at H59.",
    ),
    "UTMOSTLY-A": ShipRecord(
        symbol="UTMOSTLY-A",
        name="Probe 3",
        role="Probe",
        category="probe",
        cargo=0, fuel=0,
        notes="Solar-powered scout. Unmanned. Purchased 2026-02-16 at A2.",
    ),
    "UTMOSTLY-C": ShipRecord(
        symbol="UTMOSTLY-C",
        name="Hauler",
        role="Light Hauler",
        category="ship",
        cargo=80, fuel=600,
        notes="Interstellar candidate. 2 free module slots. Purchased 2026-02-16 at A2.",
    ),
    "UTMOSTLY-D": ShipRecord(
        symbol="UTMOSTLY-D",
        name="Surveyor",
        role="Surveyor",
        category="sentinel",
        cargo=0, fuel=80,
        notes="Drone frame, Surveyor I mount, 80 fuel. Purchased 2026-02-16 at H59.",
    ),
    "UTMOSTLY-E": ShipRecord(
        symbol="UTMOSTLY-E",
        name="Hauler 2",
        role="Light Hauler",
        category="ship",
        cargo=80, fuel=600,
        notes="Long-distance trader. 2 free module slots. Purchased 2026-02-16 at A2.",
    ),
}


def ship_name(symbol: str) -> str:
    """Return the nickname for a ship, or the symbol if unknown."""
    rec = FLEET.get(symbol)
    return rec.name if rec else symbol
