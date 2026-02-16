"""Entry point: python -m spacetraders.fleet

Runs the Fleet Commander — all ships in one process.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from spacetraders.config import load_settings

# Import mission adapters to register them
from spacetraders.fleet.missions import MissionType  # noqa: F401
import spacetraders.fleet._adapters  # noqa: F401


def setup_logging(log_dir: Path) -> None:
    """Configure logging for the fleet commander."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "fleet_commander.log"

    formatter = logging.Formatter(
        "[%(asctime)s] %(message)s", datefmt="%H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, mode="a")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger("spacetraders")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    logging.getLogger(__name__).info("Logging to %s", log_file)


def parse_overrides(raw: list[str] | None) -> dict[str, str]:
    """Parse --assign SHIP:mission pairs into a dict."""
    if not raw:
        return {}
    overrides: dict[str, str] = {}
    for item in raw:
        if ":" not in item:
            print(f"Invalid --assign format: '{item}' (expected SHIP:mission)")
            sys.exit(1)
        ship, mission = item.split(":", 1)
        overrides[ship.upper()] = mission.lower()
    return overrides


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fleet Commander — run all ships in one process",
    )
    parser.add_argument(
        "--assign", nargs="*", metavar="SHIP:MISSION",
        help="Override mission assignment (e.g. UTMOSTLY-3:trade)",
    )
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(settings.data_dir / "logs")

    overrides = parse_overrides(args.assign)

    from spacetraders.fleet.commander import FleetCommander

    commander = FleetCommander(settings, overrides=overrides)
    asyncio.run(commander.run())


if __name__ == "__main__":
    main()
