"""Reads the data the dashboard renders.

The reliability indices are never recomputed here - the dashboard only
displays what `python -m src.metrics` already wrote. The customers-out chart
is the exception: it is a plain view of outage counts, so it is bucketed
straight from the raw `outage_snapshots` rows (see `peak_customers_out_by_period`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from supabase import Client

from src.metrics.reliability import ISLAND_SCOPE_NAME, OutageSample, truncate_to_period
from src.retry import with_retry

READ_PAGE_SIZE = 1000


@dataclass
class MetricPoint:
    period_start: datetime
    scope_name: str
    saidi_minutes: float
    saifi_estimated: float
    caidi_minutes: float | None
    asai_percentage: float | None
    customer_minutes_interrupted: float
    peak_customers_without_service: int
    mean_customers_without_service: float
    coverage_ratio: float | None
    customers_served: int


@dataclass
class CustomersOutPoint:
    """Peak customers out during one hour or day, from raw snapshots."""

    period_start: datetime
    peak_customers_without_service: int


@dataclass
class MajorEventDayPoint:
    event_date: datetime
    daily_saidi_minutes: float
    threshold_t_med: float | None
    is_major_event_day: bool
    days_used_in_threshold: int
    is_provisional: bool


def _parse_timestamp(raw_value: str) -> datetime:
    return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))


def _to_metric_point(row: dict) -> MetricPoint:
    return MetricPoint(
        period_start=_parse_timestamp(row["period_start"]),
        scope_name=row["scope_name"],
        saidi_minutes=float(row.get("saidi_minutes") or 0.0),
        saifi_estimated=float(row.get("saifi_estimated") or 0.0),
        caidi_minutes=(
            float(row["caidi_minutes"]) if row.get("caidi_minutes") is not None else None
        ),
        asai_percentage=(
            float(row["asai_percentage"])
            if row.get("asai_percentage") is not None
            else None
        ),
        customer_minutes_interrupted=float(
            row.get("customer_minutes_interrupted") or 0.0
        ),
        peak_customers_without_service=int(
            row.get("peak_customers_without_service") or 0
        ),
        mean_customers_without_service=float(
            row.get("mean_customers_without_service") or 0.0
        ),
        coverage_ratio=(
            float(row["coverage_ratio"]) if row.get("coverage_ratio") is not None else None
        ),
        customers_served=int(row.get("customers_served") or 0),
    )


def load_metric_series(
    supabase_client: Client,
    granularity: str,
    cause_basis: str = "all",
    scope_name: str | None = ISLAND_SCOPE_NAME,
) -> list[MetricPoint]:
    """Ordered metric series for one granularity/cause basis (all scopes if None)."""
    points: list[MetricPoint] = []
    offset = 0

    while True:
        query = (
            supabase_client.table("reliability_metrics")
            .select("*")
            .eq("granularity", granularity)
            .eq("cause_basis", cause_basis)
        )
        if scope_name is not None:
            query = query.eq("scope_name", scope_name)

        response = with_retry(
            lambda: query.order("period_start")
            .range(offset, offset + READ_PAGE_SIZE - 1)
            .execute(),
            description="select reliability_metrics",
        )
        rows = response.data or []
        points.extend(_to_metric_point(row) for row in rows)

        if len(rows) < READ_PAGE_SIZE:
            return points
        offset += READ_PAGE_SIZE


def peak_customers_out_by_period(
    samples: list[OutageSample], granularity: str, cause_basis: str
) -> list[CustomersOutPoint]:
    """Highest customers-out count per local hour or day, oldest first.

    Hourly and daily peaks are both maxima over the same raw samples, so a
    day's peak always equals the highest of its hourly peaks.
    """
    peak_by_period_start: dict[datetime, int] = {}
    for sample in samples:
        period_start = truncate_to_period(sample.source_timestamp, granularity)
        customers_out = sample.customers_out_for_basis(cause_basis)
        if customers_out > peak_by_period_start.get(period_start, -1):
            peak_by_period_start[period_start] = customers_out
    return [
        CustomersOutPoint(period_start=period_start, peak_customers_without_service=peak)
        for period_start, peak in sorted(peak_by_period_start.items())
    ]


def load_major_event_days(
    supabase_client: Client,
    cause_basis: str = "all",
    scope_name: str = ISLAND_SCOPE_NAME,
) -> list[MajorEventDayPoint]:
    response = with_retry(
        lambda: supabase_client.table("major_event_days")
        .select("*")
        .eq("scope_name", scope_name)
        .eq("cause_basis", cause_basis)
        .order("event_date")
        .execute(),
        description="select major_event_days",
    )
    return [
        MajorEventDayPoint(
            event_date=datetime.fromisoformat(row["event_date"]),
            daily_saidi_minutes=float(row.get("daily_saidi_minutes") or 0.0),
            threshold_t_med=(
                float(row["threshold_t_med"])
                if row.get("threshold_t_med") is not None
                else None
            ),
            is_major_event_day=bool(row.get("is_major_event_day")),
            days_used_in_threshold=int(row.get("days_used_in_threshold") or 0),
            is_provisional=bool(row.get("is_provisional", True)),
        )
        for row in (response.data or [])
    ]


@dataclass
class CollectionAttempt:
    github_run_id: int | None
    method: str
    attempt_number: int
    started_at: datetime
    duration_milliseconds: int
    outcome: str
    http_status: int | None
    error_message: str | None


def load_collection_attempts(
    supabase_client: Client, since: datetime
) -> list[CollectionAttempt]:
    """Every logged collection attempt since `since`, oldest first."""
    attempts: list[CollectionAttempt] = []
    offset = 0

    while True:
        query = (
            supabase_client.table("collection_attempts")
            .select("*")
            .gte("started_at", since.isoformat())
        )
        response = with_retry(
            lambda: query.order("started_at")
            .range(offset, offset + READ_PAGE_SIZE - 1)
            .execute(),
            description="select collection_attempts",
        )
        rows = response.data or []
        attempts.extend(
            CollectionAttempt(
                github_run_id=row.get("github_run_id"),
                method=row["method"],
                attempt_number=int(row["attempt_number"]),
                started_at=_parse_timestamp(row["started_at"]),
                duration_milliseconds=int(row["duration_milliseconds"]),
                outcome=row["outcome"],
                http_status=row.get("http_status"),
                error_message=row.get("error_message"),
            )
            for row in rows
        )

        if len(rows) < READ_PAGE_SIZE:
            return attempts
        offset += READ_PAGE_SIZE
