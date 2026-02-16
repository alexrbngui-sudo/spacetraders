"""Rich Live dashboard for SpaceTraders fleet operations.

Read-only — queries operations.db, markets.db, and asteroids.db.
No API calls, no rate limit impact.  Designed to run alongside
mission processes that write to the same SQLite databases.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from spacetraders.config import load_settings
from spacetraders.data.operations_db import OperationsDB
from spacetraders.fleet_registry import FLEET, ship_name


def _format_credits(n: int | float) -> str:
    """Format credits with thousands separators."""
    return f"{int(n):,}"


def _relative_time(iso_str: str) -> str:
    """Convert ISO timestamp to relative time like '2m ago'."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        return f"{secs // 3600}h ago"
    except (ValueError, TypeError):
        return iso_str[:8]


def _short_wp(waypoint: str) -> str:
    """Shorten waypoint symbol: X1-XV5-B7 -> B7."""
    parts = waypoint.rsplit("-", 1)
    return parts[-1] if len(parts) > 1 else waypoint


def _build_header(ops_db: OperationsDB) -> Panel:
    """Agent overview panel with credits and hourly income."""
    snapshots = ops_db.get_agent_history()
    hourly = ops_db.get_hourly_income()

    if snapshots:
        latest = snapshots[-1]
        credits_str = _format_credits(latest.credits)
        ships_str = str(latest.ship_count) if latest.ship_count else "?"
    else:
        credits_str = "?"
        ships_str = "?"

    hourly_sign = "+" if hourly >= 0 else ""
    hourly_str = f"{hourly_sign}{_format_credits(hourly)}/hr"

    text = Text()
    text.append("  Credits: ", style="bold")
    text.append(credits_str, style="bold cyan")
    text.append(f"  ({hourly_str})  ", style="green" if hourly >= 0 else "red")
    text.append("  Ships: ", style="bold")
    text.append(ships_str, style="bold cyan")

    return Panel(text, title="UTMOSTLY", border_style="bright_blue")


def _build_recent_trades(ops_db: OperationsDB) -> Panel:
    """Recent trades table."""
    table = Table(
        show_header=True, header_style="bold", box=None,
        expand=True, pad_edge=False,
    )
    table.add_column("Time", width=7, no_wrap=True)
    table.add_column("Ship", width=13, no_wrap=True)
    table.add_column("Op", width=4, no_wrap=True)
    table.add_column("Good", min_width=12)
    table.add_column("Units", width=5, justify="right")
    table.add_column("Total", width=9, justify="right")

    trades = ops_db.get_trades(limit=12)
    for t in trades:
        op_style = "green" if t.operation == "SELL" else "yellow"
        sign = "+" if t.operation == "SELL" else "-"
        name = ship_name(t.ship_symbol)
        # Truncate good name for display
        good_display = t.good[:16]

        table.add_row(
            _relative_time(t.timestamp),
            name,
            Text(t.operation, style=op_style),
            good_display,
            str(t.units),
            Text(f"{sign}{_format_credits(t.total_price)}", style=op_style),
        )

    if not trades:
        table.add_row("", Text("No trades recorded yet", style="dim"), "", "", "", "")

    return Panel(table, title="Recent Trades", border_style="bright_blue")


def _build_ship_activity(ops_db: OperationsDB) -> Panel:
    """Per-ship profit summary for the last hour."""
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    activity = ops_db.get_ship_activity(since=one_hour_ago)

    table = Table(
        show_header=True, header_style="bold", box=None,
        expand=True, pad_edge=False,
    )
    table.add_column("Ship", width=13, no_wrap=True)
    table.add_column("Role", width=10, no_wrap=True)
    table.add_column("Mission", width=8, no_wrap=True)
    table.add_column("Profit", width=10, justify="right", no_wrap=True)
    table.add_column("Activity", min_width=10, justify="right")

    # Sort by profit descending
    sorted_ships = sorted(
        activity.items(),
        key=lambda x: int(x[1].get("profit", 0)),
        reverse=True,
    )

    for ship_sym, stats in sorted_ships:
        name = ship_name(ship_sym)
        record = FLEET.get(ship_sym)
        role = record.role if record else "?"
        # Truncate role
        if len(role) > 10:
            role = role[:9] + "."

        mission = str(stats.get("mission", "?"))
        profit = int(stats.get("profit", 0))
        profit_style = "green" if profit >= 0 else "red"
        sign = "+" if profit >= 0 else ""

        sell_trades = int(stats.get("sell_trades", 0))
        extractions = int(stats.get("extractions", 0))

        activity_parts = []
        if sell_trades:
            activity_parts.append(f"{sell_trades} trades")
        if extractions:
            activity_parts.append(f"{extractions} extr")
        activity_str = ", ".join(activity_parts) if activity_parts else "idle"

        table.add_row(
            name,
            role,
            mission,
            Text(f"{sign}{_format_credits(profit)}", style=profit_style),
            activity_str,
        )

    if not sorted_ships:
        table.add_row("", Text("No activity in the last hour", style="dim"), "", "", "")

    return Panel(table, title="Ship Activity (last hour)", border_style="bright_blue")


def _build_mining_yields(ops_db: OperationsDB) -> Panel:
    """Mining yield summary for the current session."""
    yields = ops_db.get_extraction_summary()

    if not yields:
        return Panel(
            Text("No mining data yet", style="dim"),
            title="Mining Yields",
            border_style="bright_blue",
        )

    # Two-column layout for yields
    table = Table(
        show_header=False, box=None, expand=True, pad_edge=False,
    )
    table.add_column("Resource", min_width=16)
    table.add_column("Units", width=8, justify="right")
    table.add_column("Resource", min_width=16)
    table.add_column("Units", width=8, justify="right")

    items = list(yields.items())
    half = (len(items) + 1) // 2
    left = items[:half]
    right = items[half:]

    for i in range(half):
        l_name, l_units = left[i]
        if i < len(right):
            r_name, r_units = right[i]
            table.add_row(l_name, f"{l_units:,}", r_name, f"{r_units:,}")
        else:
            table.add_row(l_name, f"{l_units:,}", "", "")

    return Panel(table, title="Mining Yields (session)", border_style="bright_blue")


def _build_credit_trend(ops_db: OperationsDB) -> str:
    """Simple text sparkline of credit history."""
    snapshots = ops_db.get_agent_history()
    if len(snapshots) < 2:
        return ""

    # Take last 20 snapshots
    recent = snapshots[-20:]
    values = [s.credits for s in recent]
    min_v = min(values)
    max_v = max(values)
    range_v = max_v - min_v

    if range_v == 0:
        return " ".join(["_"] * len(values))

    bars = " _.:!|"
    sparkline = ""
    for v in values:
        idx = int((v - min_v) / range_v * (len(bars) - 1))
        sparkline += bars[idx]

    return sparkline


def build_display(ops_db: OperationsDB) -> Layout:
    """Assemble the full dashboard layout."""
    layout = Layout()

    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=1),
    )

    layout["body"].split_column(
        Layout(name="trades", ratio=3),
        Layout(name="bottom", ratio=2),
    )

    layout["bottom"].split_row(
        Layout(name="ships", ratio=3),
        Layout(name="mining", ratio=2),
    )

    layout["header"].update(_build_header(ops_db))
    layout["trades"].update(_build_recent_trades(ops_db))
    layout["ships"].update(_build_ship_activity(ops_db))
    layout["mining"].update(_build_mining_yields(ops_db))

    trend = _build_credit_trend(ops_db)
    trend_display = f"  {trend}  " if trend else ""
    footer_text = Text(
        f"  Refreshing every 10s | Ctrl+C to quit | data from operations.db{trend_display}",
        style="dim",
    )
    layout["footer"].update(footer_text)

    return layout


def run_dashboard(refresh_seconds: int = 10) -> None:
    """Run the live dashboard loop."""
    settings = load_settings()
    db_path = settings.data_dir / "operations.db"

    if not db_path.exists():
        console = Console()
        console.print(
            f"[yellow]No operations database found at {db_path}.[/yellow]\n"
            "Run a mission first to generate data:\n"
            "  python -m spacetraders.missions.trader --ship UTMOSTLY-3 --continuous\n"
            "  python -m spacetraders.fleet",
        )
        return

    ops_db = OperationsDB(db_path=db_path)
    console = Console()

    try:
        with Live(
            build_display(ops_db),
            console=console,
            refresh_per_second=1,
            screen=True,
        ) as live:
            while True:
                time.sleep(refresh_seconds)
                live.update(build_display(ops_db))
    except KeyboardInterrupt:
        pass
    finally:
        ops_db.close()
        console.print("[dim]Dashboard stopped.[/dim]")
