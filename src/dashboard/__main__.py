"""Generate the static reliability dashboard.

Run with `python -m src.dashboard`. Writes the site's self-contained HTML pages
(the grid reliability page, the reliability indices page and the unlinked
collection-health status page) into one directory, which the GitHub Actions
workflow publishes to GitHub Pages.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.dashboard.build import MAXIMUM_HISTORY_DAYS, build_site_pages
from src.dashboard.data import (
    load_collection_attempts,
    load_major_event_days,
    load_metric_series,
    load_snapshot_timestamps,
)
from src.dashboard.status import (
    STATUS_HISTORY_DAYS,
    STATUS_PAGE_FILE_NAME,
    build_status_page_html,
)
from src.supabase_writer import build_supabase_client

DEFAULT_OUTPUT_DIRECTORY = Path("site")
DASHBOARD_CAUSE_BASIS = "all"


def main() -> int:
    argument_parser = argparse.ArgumentParser(
        description="Render the LUMA reliability dashboard to static HTML."
    )
    argument_parser.add_argument(
        "--output-directory",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
        help=f"Where to write the pages (default: {DEFAULT_OUTPUT_DIRECTORY}).",
    )
    arguments = argument_parser.parse_args()

    supabase_client = build_supabase_client()

    hourly_points = load_metric_series(
        supabase_client, "hour", cause_basis=DASHBOARD_CAUSE_BASIS
    )
    daily_points = load_metric_series(
        supabase_client, "day", cause_basis=DASHBOARD_CAUSE_BASIS
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

    snapshot_timestamps = load_snapshot_timestamps(
        supabase_client,
        since=datetime.now(timezone.utc) - timedelta(days=MAXIMUM_HISTORY_DAYS + 1),
    )

    hourly_points_by_cause = {
        "unplanned": load_metric_series(supabase_client, "hour", cause_basis="unplanned"),
        "planned": load_metric_series(supabase_client, "hour", cause_basis="planned"),
        "load_shed": load_metric_series(supabase_client, "hour", cause_basis="load_shed"),
    }
    daily_points_by_cause = {
        "unplanned": load_metric_series(supabase_client, "day", cause_basis="unplanned"),
        "planned": load_metric_series(supabase_client, "day", cause_basis="planned"),
        "load_shed": load_metric_series(supabase_client, "day", cause_basis="load_shed"),
    }

    pages_by_file_name = build_site_pages(
        hourly_points=hourly_points,
        daily_points=daily_points,
        region_daily_points=region_daily_points,
        major_event_days=major_event_days,
        hourly_points_by_cause=hourly_points_by_cause,
        daily_points_by_cause=daily_points_by_cause,
        snapshot_timestamps=snapshot_timestamps,
    )

    generated_at = datetime.now(timezone.utc)
    status_window_start = generated_at - timedelta(days=STATUS_HISTORY_DAYS)
    try:
        collection_attempts = load_collection_attempts(
            supabase_client, since=status_window_start
        )
    except Exception as load_error:
        # e.g. db/schema.sql not re-run yet, so the table doesn't exist. The
        # public pages must still publish; the status page shows coverage only.
        print(f"warning: could not load collection_attempts ({load_error})")
        collection_attempts = []
    pages_by_file_name[STATUS_PAGE_FILE_NAME] = build_status_page_html(
        snapshot_timestamps=[
            timestamp
            for timestamp in snapshot_timestamps
            if timestamp >= status_window_start
        ],
        attempts=collection_attempts,
        generated_at=generated_at,
    )

    arguments.output_directory.mkdir(parents=True, exist_ok=True)
    for file_name, page_html in pages_by_file_name.items():
        (arguments.output_directory / file_name).write_text(page_html, encoding="utf-8")

    print(
        f"wrote {', '.join(pages_by_file_name)} to {arguments.output_directory} "
        f"({len(hourly_points)} hourly, {len(daily_points)} daily, "
        f"{len(region_daily_points)} regional rows)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
