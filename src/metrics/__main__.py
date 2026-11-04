"""Recompute reliability metrics and Major Event Day classification.

Run with `python -m src.metrics`. Recent days are recomputed in place on
every run; Major Event Days are re-derived from the full daily history, which
stays small enough to process in one pass.

Only daily rows are computed: IEEE 1366 Major Event Days are defined on daily
SAIDI, and the dashboard's indices and charts are all built from day rows.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from src.metrics.major_event_days import classify_major_event_days
from src.metrics.reliability import (
    GRANULARITY_DAY,
    ISLAND_SCOPE_NAME,
    MAXIMUM_INTERVAL_MINUTES,
    compute_metrics_for_scope,
)
from src.metrics.store import (
    earliest_timestamp_for_lookback,
    load_daily_saidi_history,
    load_island_samples,
    load_region_samples,
    write_major_event_days,
    write_reliability_metrics,
)
from src.supabase_writer import build_supabase_client

DEFAULT_LOOKBACK_DAYS = 3
METRIC_GRANULARITIES = (GRANULARITY_DAY,)

# Which series the Major Event Day threshold is built from. IEEE 1366 applies
# MED classification at the system level, so the island-wide series is the
# standard-conforming scope.
MAJOR_EVENT_DAY_SCOPE = ISLAND_SCOPE_NAME
MAJOR_EVENT_DAY_CAUSE_BASES = ("all", "unplanned")


def main() -> int:
    argument_parser = argparse.ArgumentParser(
        description="Recompute LUMA reliability metrics into Supabase."
    )
    argument_parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=(
            "How far back to recompute period metrics. Use --full to rebuild "
            "the entire history instead."
        ),
    )
    argument_parser.add_argument(
        "--full",
        action="store_true",
        help="Recompute every period from the beginning of the raw history.",
    )
    arguments = argument_parser.parse_args()

    supabase_client = build_supabase_client()

    since = (
        None
        if arguments.full
        else earliest_timestamp_for_lookback(arguments.lookback_days)
    )
    window_description = "full history" if since is None else f"since {since:%Y-%m-%d}"
    print(f"recomputing reliability metrics ({window_description})")

    # Load a little before `since` so the window's first interval still has
    # its preceding sample, exactly as a full rebuild would see it. Intervals
    # ending before `since` only land in days that are dropped below.
    load_start = (
        None if since is None else since - timedelta(minutes=MAXIMUM_INTERVAL_MINUTES)
    )
    island_samples = load_island_samples(supabase_client, since=load_start)
    samples_by_region = load_region_samples(supabase_client, since=load_start)

    if not island_samples:
        print("no snapshots found in the requested window; nothing to compute")
        return 0

    computed_metrics = compute_metrics_for_scope(
        island_samples, ISLAND_SCOPE_NAME, granularities=METRIC_GRANULARITIES
    )
    for region_name, region_samples in sorted(samples_by_region.items()):
        computed_metrics.extend(
            compute_metrics_for_scope(
                region_samples, region_name, granularities=METRIC_GRANULARITIES
            )
        )

    # Never write a day that starts before the window: its data is only
    # partly loaded, and writing it would overwrite a complete row.
    if since is not None:
        computed_metrics = [
            metric for metric in computed_metrics if metric.period_start >= since
        ]

    written_count = write_reliability_metrics(supabase_client, computed_metrics)
    print(
        f"wrote {written_count} metric rows across "
        f"{1 + len(samples_by_region)} scopes"
    )

    # Major Event Days are derived from the stored daily SAIDI series (not the
    # in-memory window), so the threshold always reflects the full history even
    # on an incremental run.
    for cause_basis in MAJOR_EVENT_DAY_CAUSE_BASES:
        daily_history = load_daily_saidi_history(
            supabase_client, MAJOR_EVENT_DAY_SCOPE, cause_basis
        )
        if not daily_history:
            continue

        classifications = classify_major_event_days(daily_history)
        write_major_event_days(
            supabase_client, classifications, MAJOR_EVENT_DAY_SCOPE, cause_basis
        )

        threshold = classifications[0].threshold_t_med
        flagged_days = sum(
            1 for classification in classifications if classification.is_major_event_day
        )
        if threshold is None:
            print(
                f"  {cause_basis}: {len(daily_history)} days of history - too "
                f"little to compute a Major Event Day threshold yet"
            )
        else:
            provisional_note = (
                " (provisional)" if classifications[0].is_provisional else ""
            )
            print(
                f"  {cause_basis}: T_MED = {threshold:.2f} min{provisional_note}, "
                f"{flagged_days} of {len(daily_history)} days flagged"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
