"""Entry point: python -m spacetraders.dashboard

Read-only CLI dashboard — no API calls, pure DB reads.
"""

from __future__ import annotations

import argparse
import sys

from spacetraders.dashboard.app import run_dashboard


def main() -> None:
    parser = argparse.ArgumentParser(description="SpaceTraders operations dashboard")
    parser.add_argument(
        "--refresh", type=int, default=10,
        help="Refresh interval in seconds (default: 10)",
    )
    args = parser.parse_args()

    try:
        run_dashboard(refresh_seconds=args.refresh)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
