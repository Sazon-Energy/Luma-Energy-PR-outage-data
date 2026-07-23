"""Generate the static reliability dashboard.

Run with `python -m src.dashboard`. Writes a self-contained HTML page that the
GitHub Actions workflow publishes to GitHub Pages.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.dashboard.build import build_dashboard_html
from src.dashboard.data import load_major_event_days, load_metric_series
from src.supabase_writer import build_supabase_client

DEFAULT_OUTPUT_PATH = Path("site/index.html")
DASHBOARD_CAUSE_BASIS = "all"


def main() -> int:
    argument_parser = argparse.ArgumentParser(
        description="Render the LUMA reliability dashboard to static HTML."
    )
    argument_parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help=f"Where to write the page (default: {DEFAULT_OUTPUT_PATH}).",
    )
    arguments = argument_parser.parse_args()

    supabase_client = build_supabase_client()

    hourly_points = load_metric_series(
        supabase_client, "hour", cause_basis=DASHBOARD_CAUSE_BASIS
    )
    daily_points = load_metric_series(
        supabase_client, "day", cause_basis=DASHBOARD_CAUSE_BASIS
    )
    monthly_points = load_metric_series(
        supabase_client, "month", cause_basis=DASHBOARD_CAUSE_BASIS
    )
    all_daily_points = load_metric_series(
        supabase_client, "day", cause_basis=DASHBOARD_CAUSE_BASIS, scope_name=None
    )
    region_daily_points = [
        point for point in all_daily_points if point.scope_name != "Island-wide"
    ]
    major_event_days = load_major_event_days(
        supabase_client, cause_basis=DASHBOARD_CAUSE_BASIS
    )

    page_html = build_dashboard_html(
        hourly_points=hourly_points,
        daily_points=daily_points,
        monthly_points=monthly_points,
        region_daily_points=region_daily_points,
        major_event_days=major_event_days,
    )

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(page_html, encoding="utf-8")

    print(
        f"wrote {arguments.output} "
        f"({len(hourly_points)} hourly, {len(daily_points)} daily, "
        f"{len(monthly_points)} monthly, {len(region_daily_points)} regional rows)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
