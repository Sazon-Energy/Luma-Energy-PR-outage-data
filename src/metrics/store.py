"""Reading raw snapshots and persisting derived metrics in Supabase."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from supabase import Client

from src.metrics.major_event_days import DailySaidiValue, MajorEventDayClassification
from src.metrics.reliability import (
    ISLAND_SCOPE_NAME,
    OutageSample,
    ReliabilityMetrics,
)
from src.retry import with_retry

# PostgREST caps rows per request, so raw history is pulled in pages.
READ_PAGE_SIZE = 1000
WRITE_BATCH_SIZE = 500


def _parse_timestamp(raw_value: str) -> datetime:
    # PostgREST renders timestamptz as ISO 8601; Python 3.11+ parses the 'Z'
    # suffix directly, older versions need it spelled out as an offset.
    return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))


def load_island_samples(
    supabase_client: Client, since: datetime | None = None
) -> list[OutageSample]:
    """Island-wide series, taken from the totals already stored per snapshot."""
    samples: list[OutageSample] = []
    offset = 0

    while True:
        query = supabase_client.table("outage_snapshots").select(
            "source_timestamp,total_clients,total_clients_without_service,"
            "total_clients_affected_by_planned_outage,"
            "total_clients_affected_by_load_shed"
        )
        if since is not None:
            query = query.gte("source_timestamp", since.isoformat())

        response = with_retry(
            lambda: query.order("source_timestamp")
            .range(offset, offset + READ_PAGE_SIZE - 1)
            .execute(),
            description="select outage_snapshots",
        )
        rows = response.data or []
        for row in rows:
            samples.append(
                OutageSample(
                    source_timestamp=_parse_timestamp(row["source_timestamp"]),
                    customers_served=row.get("total_clients") or 0,
                    customers_without_service=(
                        row.get("total_clients_without_service") or 0
                    ),
                    customers_affected_by_planned_outage=(
                        row.get("total_clients_affected_by_planned_outage") or 0
                    ),
                    customers_affected_by_load_shed=(
                        row.get("total_clients_affected_by_load_shed") or 0
                    ),
                )
            )

        if len(rows) < READ_PAGE_SIZE:
            return samples
        offset += READ_PAGE_SIZE


def load_region_samples(
    supabase_client: Client, since: datetime | None = None
) -> dict[str, list[OutageSample]]:
    """Per-region series, keyed by region name."""
    samples_by_region: dict[str, list[OutageSample]] = {}
    offset = 0

    while True:
        query = supabase_client.table("region_readings").select(
            "region_name,total_clients,total_clients_without_service,"
            "total_clients_affected_by_planned_outage,"
            "total_clients_affected_by_load_shed,"
            "outage_snapshots!inner(source_timestamp)"
        )
        if since is not None:
            query = query.gte("outage_snapshots.source_timestamp", since.isoformat())

        response = with_retry(
            lambda: query.range(offset, offset + READ_PAGE_SIZE - 1).execute(),
            description="select region_readings",
        )
        rows = response.data or []
        for row in rows:
            related_snapshot = row.get("outage_snapshots") or {}
            raw_timestamp = related_snapshot.get("source_timestamp")
            if not raw_timestamp:
                continue

            samples_by_region.setdefault(row["region_name"], []).append(
                OutageSample(
                    source_timestamp=_parse_timestamp(raw_timestamp),
                    customers_served=row.get("total_clients") or 0,
                    customers_without_service=(
                        row.get("total_clients_without_service") or 0
                    ),
                    customers_affected_by_planned_outage=(
                        row.get("total_clients_affected_by_planned_outage") or 0
                    ),
                    customers_affected_by_load_shed=(
                        row.get("total_clients_affected_by_load_shed") or 0
                    ),
                )
            )

        if len(rows) < READ_PAGE_SIZE:
            return samples_by_region
        offset += READ_PAGE_SIZE


def write_reliability_metrics(
    supabase_client: Client, computed_metrics: list[ReliabilityMetrics]
) -> int:
    """Upsert metrics, replacing any previous computation for the same period."""
    rows = [
        {
            "granularity": metric.granularity,
            "period_start": metric.period_start.isoformat(),
            "scope_name": metric.scope_name,
            "cause_basis": metric.cause_basis,
            "customers_served": metric.customers_served,
            "customer_minutes_interrupted": round(
                metric.customer_minutes_interrupted, 4
            ),
            "customers_interrupted_estimated": metric.customers_interrupted_estimated,
            "saidi_minutes": round(metric.saidi_minutes, 6),
            "saifi_estimated": round(metric.saifi_estimated, 6),
            "caidi_minutes": (
                round(metric.caidi_minutes, 6)
                if metric.caidi_minutes is not None
                else None
            ),
            "asai_percentage": (
                round(metric.asai_percentage, 6)
                if metric.asai_percentage is not None
                else None
            ),
            "peak_customers_without_service": metric.peak_customers_without_service,
            "mean_customers_without_service": round(
                metric.mean_customers_without_service, 4
            ),
            "covered_minutes": round(metric.covered_minutes, 4),
            "coverage_ratio": (
                round(metric.coverage_ratio, 6)
                if metric.coverage_ratio is not None
                else None
            ),
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
        for metric in computed_metrics
    ]

    for batch_start in range(0, len(rows), WRITE_BATCH_SIZE):
        batch = rows[batch_start : batch_start + WRITE_BATCH_SIZE]
        with_retry(
            lambda: supabase_client.table("reliability_metrics")
            .upsert(batch, on_conflict="granularity,period_start,scope_name,cause_basis")
            .execute(),
            description="upsert reliability_metrics",
        )

    return len(rows)


def load_daily_saidi_history(
    supabase_client: Client, scope_name: str, cause_basis: str
) -> list[DailySaidiValue]:
    """Full daily SAIDI history for a scope, used to build the MED threshold."""
    daily_values: list[DailySaidiValue] = []
    offset = 0

    while True:
        response = with_retry(
            lambda: supabase_client.table("reliability_metrics")
            .select("period_start,saidi_minutes")
            .eq("granularity", "day")
            .eq("scope_name", scope_name)
            .eq("cause_basis", cause_basis)
            .order("period_start")
            .range(offset, offset + READ_PAGE_SIZE - 1)
            .execute(),
            description="select reliability_metrics",
        )
        rows = response.data or []
        for row in rows:
            daily_values.append(
                DailySaidiValue(
                    event_date=_parse_timestamp(row["period_start"]).date(),
                    saidi_minutes=float(row.get("saidi_minutes") or 0.0),
                )
            )

        if len(rows) < READ_PAGE_SIZE:
            return daily_values
        offset += READ_PAGE_SIZE


def write_major_event_days(
    supabase_client: Client,
    classifications: list[MajorEventDayClassification],
    scope_name: str,
    cause_basis: str,
) -> int:
    rows = [
        {
            "event_date": classification.event_date.isoformat(),
            "scope_name": scope_name,
            "cause_basis": cause_basis,
            "daily_saidi_minutes": round(classification.daily_saidi_minutes, 6),
            "threshold_t_med": (
                round(classification.threshold_t_med, 6)
                if classification.threshold_t_med is not None
                else None
            ),
            "is_major_event_day": classification.is_major_event_day,
            "days_used_in_threshold": classification.days_used_in_threshold,
            "is_provisional": classification.is_provisional,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
        for classification in classifications
    ]

    for batch_start in range(0, len(rows), WRITE_BATCH_SIZE):
        batch = rows[batch_start : batch_start + WRITE_BATCH_SIZE]
        with_retry(
            lambda: supabase_client.table("major_event_days")
            .upsert(batch, on_conflict="event_date,scope_name,cause_basis")
            .execute(),
            description="upsert major_event_days",
        )

    return len(rows)


def earliest_timestamp_for_lookback(lookback_days: int) -> datetime:
    """Start of the window whose metrics get recomputed on this run.

    A lookback is enough because only recent periods can still change; older
    periods are already final. Recomputing an extra day is cheap insurance
    against a late-arriving or backfilled sample.
    """
    return datetime.now(timezone.utc) - timedelta(days=lookback_days)


def local_date_of(moment: datetime) -> date:
    return moment.date()
