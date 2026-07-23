"""Major Event Day classification using the IEEE 1366 2.5 Beta Method.

The standard procedure:

  1. Collect daily SAIDI values over the study period (the standard calls for
     five years of history).
  2. Discard days with zero SAIDI - the method works on natural logs, and
     ln(0) is undefined.
  3. Take the natural log of each remaining daily SAIDI value.
  4. alpha = mean of those logs, beta = sample standard deviation of them.
  5. T_MED = exp(alpha + 2.5 * beta).
  6. Any day whose SAIDI exceeds T_MED is a Major Event Day.

Major Event Days are reported separately and excluded from "normalized"
reliability figures, because a single hurricane would otherwise dominate years
of ordinary performance.

Until five years of history exist the threshold is still computed, but flagged
provisional: with a short window it is volatile and will move as data
accumulates.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date

# The standard specifies five years of daily history. Below that the threshold
# is reported as provisional rather than withheld, so the dashboard can show
# something useful from the start.
STANDARD_HISTORY_DAYS = 365 * 5

# Sample standard deviation needs at least two points, but a threshold built
# from a handful of days is meaningless. This is the floor for computing one
# at all.
MINIMUM_DAYS_FOR_THRESHOLD = 30


@dataclass
class DailySaidiValue:
    event_date: date
    saidi_minutes: float


@dataclass
class MajorEventDayClassification:
    event_date: date
    daily_saidi_minutes: float
    threshold_t_med: float | None
    is_major_event_day: bool
    days_used_in_threshold: int
    is_provisional: bool


def compute_major_event_day_threshold(
    daily_values: list[DailySaidiValue],
) -> tuple[float | None, int]:
    """Return (T_MED, number of days used).

    Returns (None, count) when there is too little history to compute a
    meaningful threshold.
    """
    # Step 2: only days with non-zero SAIDI participate; ln(0) is undefined.
    positive_saidi_values = [
        daily_value.saidi_minutes
        for daily_value in daily_values
        if daily_value.saidi_minutes > 0
    ]

    days_used = len(positive_saidi_values)
    if days_used < MINIMUM_DAYS_FOR_THRESHOLD:
        return None, days_used

    # Steps 3-5.
    natural_logs = [math.log(value) for value in positive_saidi_values]
    alpha = statistics.fmean(natural_logs)
    beta = statistics.stdev(natural_logs)
    threshold_t_med = math.exp(alpha + 2.5 * beta)

    return threshold_t_med, days_used


def classify_major_event_days(
    daily_values: list[DailySaidiValue],
) -> list[MajorEventDayClassification]:
    """Classify each day against the 2.5 Beta threshold."""
    threshold_t_med, days_used = compute_major_event_day_threshold(daily_values)
    is_provisional = days_used < STANDARD_HISTORY_DAYS

    return [
        MajorEventDayClassification(
            event_date=daily_value.event_date,
            daily_saidi_minutes=daily_value.saidi_minutes,
            threshold_t_med=threshold_t_med,
            is_major_event_day=(
                threshold_t_med is not None
                and daily_value.saidi_minutes > threshold_t_med
            ),
            days_used_in_threshold=days_used,
            is_provisional=is_provisional,
        )
        for daily_value in daily_values
    ]
