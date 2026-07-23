"""Reliability index computation from sampled customers-without-service data.

The LUMA feed reports a *level* (how many customers are out right now), not an
event log. That distinction drives everything here:

  * SAIDI is the integral of the customers-out curve divided by customers
    served. Integrating a sampled level signal is well posed, so SAIDI (and
    ASAI, which derives from it) is solidly supported.

  * SAIFI needs the number of customers *interrupted*, which a level signal
    cannot reveal directly - seeing 1,000 out at 10:00 and 1,000 at 10:10 does
    not say whether that is one interruption or two disjoint ones. It is
    estimated from the rising edges of the curve, which makes it a LOWER BOUND:
    any outage that begins and ends between two samples is invisible, and a
    restoration coinciding with a new interruption nets out.

  * CAIDI = SAIDI / SAIFI therefore inherits that bias and is an UPPER BOUND.

Intervals longer than MAXIMUM_INTERVAL_MINUTES are treated as collection gaps
and excluded from the integral rather than bridged, so a missed run cannot
silently invent customer-minutes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PUERTO_RICO_TIMEZONE = ZoneInfo("America/Puerto_Rico")

# The source feed refreshes roughly every 10 minutes. Anything materially
# longer than a couple of refresh cycles means we missed collections, and
# interpolating across it would fabricate customer-minutes.
MAXIMUM_INTERVAL_MINUTES = 35.0

ISLAND_SCOPE_NAME = "Island-wide"

CAUSE_BASES = ("all", "unplanned", "planned", "load_shed")

GRANULARITY_HOUR = "hour"
GRANULARITY_DAY = "day"
GRANULARITY_MONTH = "month"


@dataclass
class OutageSample:
    """One observation of the outage level for a single scope."""

    source_timestamp: datetime
    customers_served: int
    customers_without_service: int
    customers_affected_by_planned_outage: int
    customers_affected_by_load_shed: int

    def customers_out_for_basis(self, cause_basis: str) -> int:
        """Customers out attributable to `cause_basis`.

        `planned` and `load_shed` are subsets of `customers_without_service`
        in the LUMA feed (verified: planned + load_shed <= without_service in
        every region), so `unplanned` is the remainder.
        """
        if cause_basis == "all":
            return self.customers_without_service
        if cause_basis == "planned":
            return self.customers_affected_by_planned_outage
        if cause_basis == "load_shed":
            return self.customers_affected_by_load_shed
        if cause_basis == "unplanned":
            return max(
                0,
                self.customers_without_service
                - self.customers_affected_by_planned_outage
                - self.customers_affected_by_load_shed,
            )
        raise ValueError(f"unknown cause basis: {cause_basis}")


@dataclass
class IntervalContribution:
    """What one gap-free interval between two samples contributes."""

    period_timestamp: datetime
    interval_minutes: float
    customer_minutes_interrupted: float
    customers_newly_interrupted: int
    customers_served: int
    customers_out_at_end: int


@dataclass
class ReliabilityMetrics:
    granularity: str
    period_start: datetime
    scope_name: str
    cause_basis: str
    customers_served: int
    customer_minutes_interrupted: float
    customers_interrupted_estimated: int
    saidi_minutes: float
    saifi_estimated: float
    caidi_minutes: float | None
    asai_percentage: float | None
    peak_customers_without_service: int
    mean_customers_without_service: float
    covered_minutes: float
    coverage_ratio: float | None


def build_interval_contributions(
    samples: list[OutageSample], cause_basis: str
) -> list[IntervalContribution]:
    """Turn consecutive samples into per-interval contributions.

    Uses trapezoidal integration for customer-minutes (the level is assumed to
    move linearly between observations) and the rising edge for newly
    interrupted customers.
    """
    ordered_samples = sorted(samples, key=lambda sample: sample.source_timestamp)
    contributions: list[IntervalContribution] = []

    for previous_sample, current_sample in zip(ordered_samples, ordered_samples[1:]):
        interval_minutes = (
            current_sample.source_timestamp - previous_sample.source_timestamp
        ).total_seconds() / 60.0

        if interval_minutes <= 0 or interval_minutes > MAXIMUM_INTERVAL_MINUTES:
            # Duplicate/out-of-order timestamp, or a collection gap. Skipping
            # keeps the integral honest; the lost time shows up as reduced
            # coverage rather than as fabricated customer-minutes.
            continue

        customers_out_previous = previous_sample.customers_out_for_basis(cause_basis)
        customers_out_current = current_sample.customers_out_for_basis(cause_basis)

        contributions.append(
            IntervalContribution(
                period_timestamp=current_sample.source_timestamp,
                interval_minutes=interval_minutes,
                customer_minutes_interrupted=(
                    (customers_out_previous + customers_out_current)
                    / 2.0
                    * interval_minutes
                ),
                customers_newly_interrupted=max(
                    0, customers_out_current - customers_out_previous
                ),
                customers_served=current_sample.customers_served,
                customers_out_at_end=customers_out_current,
            )
        )

    return contributions


def truncate_to_period(moment: datetime, granularity: str) -> datetime:
    """Bucket a moment into its local-time period start.

    Bucketing happens in Puerto Rico local time so that a "day" is a local
    calendar day, which is what daily SAIDI and Major Event Day classification
    are defined against.
    """
    local_moment = moment.astimezone(PUERTO_RICO_TIMEZONE)

    if granularity == GRANULARITY_HOUR:
        local_period_start = local_moment.replace(minute=0, second=0, microsecond=0)
    elif granularity == GRANULARITY_DAY:
        local_period_start = local_moment.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif granularity == GRANULARITY_MONTH:
        local_period_start = local_moment.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
    else:
        raise ValueError(f"unknown granularity: {granularity}")

    return local_period_start


def nominal_period_minutes(period_start: datetime, granularity: str) -> float:
    """Full length of the period, used for ASAI and coverage."""
    if granularity == GRANULARITY_HOUR:
        return 60.0
    if granularity == GRANULARITY_DAY:
        next_period_start = (period_start + timedelta(days=1, hours=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif granularity == GRANULARITY_MONTH:
        next_period_start = (period_start + timedelta(days=32)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
    else:
        raise ValueError(f"unknown granularity: {granularity}")

    # Computed from real timestamps so daylight-saving transitions (and short
    # months) produce the true elapsed length. Puerto Rico does not observe
    # DST today, but this keeps the arithmetic correct regardless.
    return (next_period_start - period_start).total_seconds() / 60.0


def summarize_period(
    contributions: list[IntervalContribution],
    granularity: str,
    period_start: datetime,
    scope_name: str,
    cause_basis: str,
) -> ReliabilityMetrics:
    """Roll interval contributions up into the reliability indices."""
    customer_minutes_interrupted = sum(
        contribution.customer_minutes_interrupted for contribution in contributions
    )
    customers_interrupted_estimated = sum(
        contribution.customers_newly_interrupted for contribution in contributions
    )
    covered_minutes = sum(
        contribution.interval_minutes for contribution in contributions
    )

    # "Average number of customers served during the period" is the standard
    # denominator; it is stable in practice but averaged here so a change in
    # the reported customer base cannot produce a step artifact.
    customers_served = round(
        sum(contribution.customers_served for contribution in contributions)
        / len(contributions)
    )

    saidi_minutes = (
        customer_minutes_interrupted / customers_served if customers_served else 0.0
    )
    saifi_estimated = (
        customers_interrupted_estimated / customers_served if customers_served else 0.0
    )
    caidi_minutes = saidi_minutes / saifi_estimated if saifi_estimated > 0 else None

    period_minutes = nominal_period_minutes(period_start, granularity)
    asai_percentage = (
        (1.0 - saidi_minutes / period_minutes) * 100.0 if period_minutes else None
    )

    # Customers-out is time-weighted so that a brief spike between two widely
    # spaced samples cannot outweigh a long steady outage.
    mean_customers_without_service = (
        customer_minutes_interrupted / covered_minutes if covered_minutes else 0.0
    )

    return ReliabilityMetrics(
        granularity=granularity,
        period_start=period_start,
        scope_name=scope_name,
        cause_basis=cause_basis,
        customers_served=customers_served,
        customer_minutes_interrupted=customer_minutes_interrupted,
        customers_interrupted_estimated=customers_interrupted_estimated,
        saidi_minutes=saidi_minutes,
        saifi_estimated=saifi_estimated,
        caidi_minutes=caidi_minutes,
        asai_percentage=asai_percentage,
        peak_customers_without_service=max(
            (contribution.customers_out_at_end for contribution in contributions),
            default=0,
        ),
        mean_customers_without_service=mean_customers_without_service,
        covered_minutes=covered_minutes,
        coverage_ratio=(
            min(1.0, covered_minutes / period_minutes) if period_minutes else None
        ),
    )


def compute_metrics_for_scope(
    samples: list[OutageSample],
    scope_name: str,
    granularities: tuple[str, ...] = (
        GRANULARITY_HOUR,
        GRANULARITY_DAY,
        GRANULARITY_MONTH,
    ),
    cause_bases: tuple[str, ...] = CAUSE_BASES,
) -> list[ReliabilityMetrics]:
    """Compute every index, for every granularity and cause basis, for a scope."""
    computed_metrics: list[ReliabilityMetrics] = []

    for cause_basis in cause_bases:
        contributions = build_interval_contributions(samples, cause_basis)
        if not contributions:
            continue

        for granularity in granularities:
            contributions_by_period: dict[datetime, list[IntervalContribution]] = {}
            for contribution in contributions:
                period_start = truncate_to_period(
                    contribution.period_timestamp, granularity
                )
                contributions_by_period.setdefault(period_start, []).append(
                    contribution
                )

            for period_start, period_contributions in contributions_by_period.items():
                computed_metrics.append(
                    summarize_period(
                        period_contributions,
                        granularity,
                        period_start,
                        scope_name,
                        cause_basis,
                    )
                )

    return computed_metrics


def is_finite_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0
