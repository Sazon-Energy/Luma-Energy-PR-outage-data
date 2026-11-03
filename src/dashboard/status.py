"""Renders status.html: how reliably the collector captures LUMA's figures.

Unlinked from the public pages (bookmark it). Coverage is computed from
`outage_snapshots` - a 10-minute slot with no snapshot is a miss - and the
`collection_attempts` log explains each miss: which method ran, how long it
took and what came back. A miss with no attempts at all means the run never
started or died before fetching (pg_cron trigger, GitHub Actions, setup).
"""

from __future__ import annotations

import html
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import plotly.graph_objects as graph_objects
from plotly.subplots import make_subplots

from src.attempt_log import (
    METHOD_BROWSERLESS,
    METHOD_DIRECT,
    OUTCOME_ALREADY_RECORDED,
    OUTCOME_CAPTURE_ERROR,
    OUTCOME_CAPTURED,
    OUTCOME_CONNECTION_ERROR,
    OUTCOME_INVALID_PAYLOAD,
    OUTCOME_TIMEOUT,
    OUTCOME_WAF_BLOCKED,
    OUTCOME_WRITE_ERROR,
    SUCCESSFUL_OUTCOMES,
)
from src.dashboard.build import (
    COLOR_PRIMARY_BAR,
    SAMPLE_INTERVAL_MINUTES,
    _empty_state,
    _figure_to_html,
    _render_page,
    _to_puerto_rico_wall_clock,
    _to_puerto_rico_wall_clocks,
    count_captured_samples_by_day,
    expected_samples_for_day,
)
from src.dashboard.data import CollectionAttempt
from src.metrics.reliability import PUERTO_RICO_TIMEZONE

STATUS_PAGE_FILE_NAME = "status.html"

STATUS_HISTORY_DAYS = 30
MISSED_SLOT_TABLE_DAYS = 7
MISSED_SLOT_TABLE_LIMIT = 100
FAILED_ATTEMPT_TABLE_LIMIT = 50
SLOT_DURATION = timedelta(minutes=SAMPLE_INTERVAL_MINUTES)

# Stack order and colors. The colors are the validated default categorical
# order from the dataviz palette, in that order, so neighbouring segments in
# the stacked bars stay distinguishable under color-vision deficiency.
OUTCOME_DISPLAY = [
    (OUTCOME_CAPTURED, "Captured", "#2a78d6"),
    (OUTCOME_ALREADY_RECORDED, "Already recorded", "#eb6834"),
    (OUTCOME_WAF_BLOCKED, "WAF blocked", "#1baf7a"),
    (OUTCOME_TIMEOUT, "Timeout", "#eda100"),
    (OUTCOME_CONNECTION_ERROR, "Connection error", "#e87ba4"),
    (OUTCOME_INVALID_PAYLOAD, "Invalid payload", "#008300"),
    (OUTCOME_CAPTURE_ERROR, "Capture error", "#6250d6"),
    (OUTCOME_WRITE_ERROR, "Write error", "#e34948"),
]
OUTCOME_LABELS = {outcome: label for outcome, label, _ in OUTCOME_DISPLAY}
OUTCOME_COLORS = {outcome: color for outcome, _, color in OUTCOME_DISPLAY}
METHOD_LABELS = {METHOD_DIRECT: "Direct", METHOD_BROWSERLESS: "Browserless"}

TABLE_TIME_FORMAT = "%b %-d, %H:%M AST"


def _slot_start(moment: datetime) -> datetime:
    """The 10-minute slot (UTC-aligned, same as LUMA's publishes) holding `moment`."""
    moment = moment.astimezone(timezone.utc)
    return moment.replace(
        minute=moment.minute - moment.minute % SAMPLE_INTERVAL_MINUTES,
        second=0,
        microsecond=0,
    )


def _completed_slots(since: datetime, generated_at: datetime) -> list[datetime]:
    """Every slot from `since` up to the last one that has fully ended, so
    the slot still in progress at build time is never counted as missed."""
    slot = _slot_start(since)
    last_completed_slot = _slot_start(generated_at) - SLOT_DURATION
    slots = []
    while slot <= last_completed_slot:
        slots.append(slot)
        slot += SLOT_DURATION
    return slots


def _format_percentage(numerator: int, denominator: int, decimals: int = 1) -> str:
    if denominator == 0:
        return "—"
    return f"{100 * numerator / denominator:.{decimals}f} %"


def _group_attempts_into_runs(
    attempts: list[CollectionAttempt],
) -> list[list[CollectionAttempt]]:
    """Attempts grouped per collection run. Local runs carry no GitHub run
    id, so those fall back to grouping by slot."""
    attempts_by_run: dict[object, list[CollectionAttempt]] = defaultdict(list)
    for attempt in attempts:
        run_key = attempt.github_run_id or ("slot", _slot_start(attempt.started_at))
        attempts_by_run[run_key].append(attempt)
    return list(attempts_by_run.values())


def _kpi_card(name: str, value: str, note: str) -> str:
    return f"""
        <div class="kpi-card">
          <div class="kpi-header"><span class="kpi-name">{html.escape(name)}</span></div>
          <div class="kpi-value">{html.escape(value)}</div>
          <div class="kpi-note">{html.escape(note)}</div>
        </div>"""


def build_status_kpi_cards(
    captured_slots: set[datetime],
    attempts: list[CollectionAttempt],
    generated_at: datetime,
    earliest_snapshot: datetime | None,
) -> str:
    cards = []
    for window_label, window in (
        ("24 h", timedelta(hours=24)),
        ("7 d", timedelta(days=7)),
        ("30 d", timedelta(days=30)),
    ):
        window_start = generated_at - window
        if earliest_snapshot is not None:
            window_start = max(window_start, earliest_snapshot)
        expected_slots = _completed_slots(window_start, generated_at)
        captured_count = sum(1 for slot in expected_slots if slot in captured_slots)
        missed_count = len(expected_slots) - captured_count
        cards.append(
            _kpi_card(
                f"Coverage {window_label}",
                _format_percentage(captured_count, len(expected_slots), 2),
                f"{missed_count:,} of {len(expected_slots):,} slots missed",
            )
        )

    recent_runs = _group_attempts_into_runs(
        [
            attempt
            for attempt in attempts
            if attempt.started_at >= generated_at - timedelta(days=7)
        ]
    )
    first_attempt_successes = sum(
        1
        for run_attempts in recent_runs
        if any(
            attempt.method == METHOD_DIRECT
            and attempt.attempt_number == 1
            and attempt.outcome in SUCCESSFUL_OUTCOMES
            for attempt in run_attempts
        )
    )
    fallback_runs = sum(
        1
        for run_attempts in recent_runs
        if any(attempt.method == METHOD_BROWSERLESS for attempt in run_attempts)
    )
    run_note = f" ({len(recent_runs):,} logged runs, last 7 d)"
    cards.append(
        _kpi_card(
            "First-try success",
            _format_percentage(first_attempt_successes, len(recent_runs)),
            f"Direct attempt 1 succeeded{run_note}"
            if recent_runs
            else "No attempts logged yet",
        )
    )
    cards.append(
        _kpi_card(
            "Browserless fallback",
            _format_percentage(fallback_runs, len(recent_runs)),
            f"Runs that needed the fallback{run_note}"
            if recent_runs
            else "No attempts logged yet",
        )
    )
    return f'<div class="kpi-grid">{"".join(cards)}</div>'


def build_daily_coverage_figure(
    snapshot_timestamps: list[datetime], generated_at: datetime
) -> str:
    captured_by_day = count_captured_samples_by_day(snapshot_timestamps)
    if not captured_by_day:
        return _empty_state("No snapshots yet.")

    today = generated_at.astimezone(PUERTO_RICO_TIMEZONE).date()
    first_day = max(
        min(captured_by_day), today - timedelta(days=STATUS_HISTORY_DAYS - 1)
    )
    days: list[date] = [
        first_day + timedelta(days=day_offset)
        for day_offset in range((today - first_day).days + 1)
    ]
    coverage_percentages = []
    hover_texts = []
    for day in days:
        expected = expected_samples_for_day(day, generated_at)
        captured = min(captured_by_day.get(day, 0), expected)
        coverage_percentages.append(100 * captured / expected if expected else None)
        hover_texts.append(f"{captured} of {expected} slots captured")

    figure = graph_objects.Figure(
        graph_objects.Bar(
            x=days,
            y=coverage_percentages,
            marker=dict(color=COLOR_PRIMARY_BAR),
            customdata=hover_texts,
            hovertemplate="%{y:.2f} %<br>%{customdata}<extra></extra>",
            name="Coverage",
        )
    )
    lowest_coverage = min(
        (percentage for percentage in coverage_percentages if percentage is not None),
        default=100,
    )
    figure.update_layout(
        title="Daily coverage (AST days, today partial)",
        yaxis=dict(
            title="Slots captured (%)",
            range=[max(0, min(95, lowest_coverage - 1)), 100.5],
        ),
        hovermode="closest",
        bargap=0.15,
    )
    return _figure_to_html(figure, height=320)


def build_attempts_by_outcome_figure(attempts: list[CollectionAttempt]) -> str:
    if not attempts:
        return _empty_state("No collection attempts logged yet.")

    counts: dict[tuple[str, str], dict[date, int]] = defaultdict(lambda: defaultdict(int))
    all_days: set[date] = set()
    for attempt in attempts:
        local_day = attempt.started_at.astimezone(PUERTO_RICO_TIMEZONE).date()
        counts[(attempt.method, attempt.outcome)][local_day] += 1
        all_days.add(local_day)
    days = sorted(all_days)

    methods = [METHOD_DIRECT, METHOD_BROWSERLESS]
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.12,
        subplot_titles=[f"{METHOD_LABELS[method]} attempts" for method in methods],
    )
    for row_number, method in enumerate(methods, start=1):
        for outcome, label, color in OUTCOME_DISPLAY:
            outcome_counts = counts.get((method, outcome))
            if not outcome_counts:
                continue
            figure.add_trace(
                graph_objects.Bar(
                    x=days,
                    y=[outcome_counts.get(day, 0) for day in days],
                    name=label,
                    legendgroup=outcome,
                    # One legend entry per outcome, even if it appears in both rows.
                    showlegend=not any(
                        trace.legendgroup == outcome for trace in figure.data
                    ),
                    marker=dict(color=color, line=dict(color="#ffffff", width=2)),
                    hovertemplate=f"{label}: %{{y}}<extra></extra>",
                ),
                row=row_number,
                col=1,
            )
    figure.update_layout(
        title="Attempts per day by outcome",
        barmode="stack",
        legend=dict(orientation="h", y=-0.12, x=0),
    )
    return _figure_to_html(figure, height=480, margin_top=72)


def build_attempt_duration_figure(attempts: list[CollectionAttempt]) -> str:
    if not attempts:
        return _empty_state("No collection attempts logged yet.")

    figure = graph_objects.Figure()
    for outcome, label, color in OUTCOME_DISPLAY:
        for method, symbol in ((METHOD_DIRECT, "circle"), (METHOD_BROWSERLESS, "diamond")):
            matching_attempts = [
                attempt
                for attempt in attempts
                if attempt.outcome == outcome and attempt.method == method
            ]
            if not matching_attempts:
                continue
            figure.add_trace(
                graph_objects.Scatter(
                    x=_to_puerto_rico_wall_clocks(
                        [attempt.started_at for attempt in matching_attempts]
                    ),
                    y=[attempt.duration_milliseconds / 1000 for attempt in matching_attempts],
                    mode="markers",
                    name=f"{label} · {METHOD_LABELS[method]}",
                    marker=dict(
                        color=color,
                        symbol=symbol,
                        size=8,
                        line=dict(color="#ffffff", width=2),
                    ),
                    customdata=[
                        (attempt.error_message or "")[:120] for attempt in matching_attempts
                    ],
                    hovertemplate=(
                        f"{label} · {METHOD_LABELS[method]}<br>"
                        "%{x|%b %-d, %H:%M} AST<br>%{y:.1f} s<br>%{customdata}<extra></extra>"
                    ),
                )
            )
    figure.update_layout(
        title="Attempt duration (circle = direct, diamond = browserless)",
        yaxis=dict(title="Seconds", rangemode="tozero"),
        hovermode="closest",
        legend=dict(orientation="h", y=-0.15, x=0),
    )
    return _figure_to_html(figure, height=400)


def _describe_attempt(attempt: CollectionAttempt) -> str:
    details = [
        f"{METHOD_LABELS.get(attempt.method, attempt.method)} #{attempt.attempt_number}",
        OUTCOME_LABELS.get(attempt.outcome, attempt.outcome),
        f"{attempt.duration_milliseconds / 1000:.1f} s",
    ]
    if attempt.http_status:
        details.append(f"HTTP {attempt.http_status}")
    description = " · ".join(details)
    if attempt.error_message:
        description += f" — {attempt.error_message}"
    return html.escape(description)


def build_missed_slots_table(
    captured_slots: set[datetime],
    attempts: list[CollectionAttempt],
    generated_at: datetime,
    earliest_snapshot: datetime | None,
) -> str:
    window_start = generated_at - timedelta(days=MISSED_SLOT_TABLE_DAYS)
    if earliest_snapshot is not None:
        window_start = max(window_start, earliest_snapshot)
    missed_slots = [
        slot
        for slot in _completed_slots(window_start, generated_at)
        if slot not in captured_slots
    ]
    if not missed_slots:
        return _empty_state(f"No missed slots in the last {MISSED_SLOT_TABLE_DAYS} days.")

    attempts_by_slot: dict[datetime, list[CollectionAttempt]] = defaultdict(list)
    for attempt in attempts:
        attempts_by_slot[_slot_start(attempt.started_at)].append(attempt)

    rows = []
    for slot in sorted(missed_slots, reverse=True)[:MISSED_SLOT_TABLE_LIMIT]:
        slot_attempts = attempts_by_slot.get(slot, [])
        attempt_lines = (
            "<br>".join(_describe_attempt(attempt) for attempt in slot_attempts)
            if slot_attempts
            else "No attempt recorded (run never started or failed before fetching)"
        )
        rows.append(
            f'<tr><td data-label="Slot">'
            f"{_to_puerto_rico_wall_clock(slot):{TABLE_TIME_FORMAT}}</td>"
            f'<td data-label="Attempts">{attempt_lines}</td></tr>'
        )
    shown_note = (
        f" (newest {MISSED_SLOT_TABLE_LIMIT} shown)"
        if len(missed_slots) > MISSED_SLOT_TABLE_LIMIT
        else ""
    )
    return f"""
      <h2>Missed slots, last {MISSED_SLOT_TABLE_DAYS} days: {len(missed_slots):,}{shown_note}</h2>
      <table class="callout-table">
        <thead><tr><th>Slot</th><th>Attempts in that slot</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>"""


def build_failed_attempts_table(attempts: list[CollectionAttempt]) -> str:
    failed_attempts = [
        attempt for attempt in attempts if attempt.outcome not in SUCCESSFUL_OUTCOMES
    ]
    if not failed_attempts:
        return _empty_state("No failed attempts logged.")

    rows = "".join(
        f'<tr><td data-label="Started">'
        f"{_to_puerto_rico_wall_clock(attempt.started_at):{TABLE_TIME_FORMAT}}</td>"
        f'<td data-label="Attempt">{_describe_attempt(attempt)}</td></tr>'
        for attempt in reversed(failed_attempts[-FAILED_ATTEMPT_TABLE_LIMIT:])
    )
    return f"""
      <h2>Recent failed attempts (including ones a retry recovered)</h2>
      <table class="callout-table">
        <thead><tr><th>Started</th><th>Attempt</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>"""


def build_status_page_html(
    snapshot_timestamps: list[datetime],
    attempts: list[CollectionAttempt],
    generated_at: datetime,
) -> str:
    captured_slots = {_slot_start(timestamp) for timestamp in snapshot_timestamps}
    earliest_snapshot = min(snapshot_timestamps) if snapshot_timestamps else None

    body_html = f"""
    <div class="panel">{build_status_kpi_cards(captured_slots, attempts, generated_at, earliest_snapshot)}</div>
    <div class="panel">{build_daily_coverage_figure(snapshot_timestamps, generated_at)}</div>
    <div class="panel">{build_attempts_by_outcome_figure(attempts)}</div>
    <div class="panel">{build_attempt_duration_figure(attempts)}</div>
    <div class="panel">{build_missed_slots_table(captured_slots, attempts, generated_at, earliest_snapshot)}</div>
    <div class="panel">{build_failed_attempts_table(attempts)}</div>"""
    return _render_page(
        page_title="Collection Health",
        heading="Collection Health",
        lead=(
            "How reliably each 10-minute LUMA publish is captured, and what "
            "happened when one was missed."
        ),
        current_page=STATUS_PAGE_FILE_NAME,
        body_html=body_html,
        generated_at=generated_at,
        include_visitor_analytics=False,
    )
