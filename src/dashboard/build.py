"""Renders the reliability dashboard as a self-contained static HTML page.

Everything is produced from Python via Plotly, so there is no JavaScript to
maintain, no server to run, and no database credentials in the browser - the
figures carry their own data.
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

import plotly.graph_objects as graph_objects
import plotly.io as plotly_io

from src.dashboard.data import MajorEventDayPoint, MetricPoint

PLOTLY_TEMPLATE = "plotly_white"
FIGURE_CONFIG = {"displayModeBar": False, "responsive": True}

COLOR_PRIMARY = "#0f6fc5"
COLOR_ACCENT = "#e8833a"
COLOR_MAJOR_EVENT = "#c0392b"
COLOR_MUTED = "#94a3b8"

# Bottom-to-top stack order for build_customers_out_figure.
CUSTOMERS_OUT_SEGMENTS = [
    ("unplanned", "Unplanned", COLOR_PRIMARY),
    ("planned", "Planned", COLOR_ACCENT),
    ("load_shed", "Load shed", COLOR_MUTED),
]

MINUTES_PER_DAY = 1440


def _figure_to_html(figure: graph_objects.Figure) -> str:
    figure.update_layout(
        template=PLOTLY_TEMPLATE,
        margin=dict(l=56, r=24, t=48, b=48),
        height=380,
        hovermode="x unified",
        font=dict(family="system-ui, -apple-system, Segoe UI, Roboto, sans-serif"),
    )
    return plotly_io.to_html(
        figure, full_html=False, include_plotlyjs=False, config=FIGURE_CONFIG
    )


def _empty_state(message: str) -> str:
    return f'<div class="empty-state">{html.escape(message)}</div>'


def _customers_out_axis(
    points_by_cause: dict[str, list[MetricPoint]], limit: int
) -> list[datetime]:
    all_period_starts = {
        point.period_start for points in points_by_cause.values() for point in points
    }
    return sorted(all_period_starts)[-limit:]


def _customers_out_series(
    points: list[MetricPoint], axis: list[datetime]
) -> list[float]:
    mean_by_period_start = {
        point.period_start: point.mean_customers_without_service for point in points
    }
    return [mean_by_period_start.get(period_start, 0.0) for period_start in axis]


def build_customers_out_figure(
    hourly_points_by_cause: dict[str, list[MetricPoint]],
    daily_points_by_cause: dict[str, list[MetricPoint]],
) -> str:
    """Customers out of service, stacked by cause, hourly (36h) or daily (30d).

    Both granularities are baked into one figure as separate trace groups;
    the hourly/daily buttons flip which group is visible, so the toggle needs
    no JavaScript beyond what Plotly already ships.
    """
    hourly_axis = _customers_out_axis(hourly_points_by_cause, 36)
    daily_axis = _customers_out_axis(daily_points_by_cause, 30)
    if not hourly_axis and not daily_axis:
        return _empty_state("No outage data collected yet.")

    figure = graph_objects.Figure()
    for cause_basis, label, color in CUSTOMERS_OUT_SEGMENTS:
        figure.add_trace(
            graph_objects.Bar(
                x=hourly_axis,
                y=_customers_out_series(hourly_points_by_cause[cause_basis], hourly_axis),
                name=label,
                marker_color=color,
                visible=True,
                hovertemplate="%{y:,.0f} customers<extra>" + label + "</extra>",
            )
        )
    for cause_basis, label, color in CUSTOMERS_OUT_SEGMENTS:
        figure.add_trace(
            graph_objects.Bar(
                x=daily_axis,
                y=_customers_out_series(daily_points_by_cause[cause_basis], daily_axis),
                name=label,
                marker_color=color,
                visible=False,
                hovertemplate="%{y:,.0f} customers<extra>" + label + "</extra>",
            )
        )

    figure.update_layout(
        title="Customers out of service",
        yaxis_title="Customers out (period average)",
        xaxis_title="Hour (Puerto Rico local time)",
        barmode="stack",
        legend=dict(orientation="h", y=1.12, x=0),
        updatemenus=[
            dict(
                type="buttons",
                direction="right",
                showactive=True,
                active=0,
                x=1.0,
                y=1.18,
                xanchor="right",
                yanchor="bottom",
                buttons=[
                    dict(
                        label="Hourly · 36 h",
                        method="update",
                        args=[
                            {"visible": [True, True, True, False, False, False]},
                            {"xaxis.title.text": "Hour (Puerto Rico local time)"},
                        ],
                    ),
                    dict(
                        label="Daily · 30 d",
                        method="update",
                        args=[
                            {"visible": [False, False, False, True, True, True]},
                            {"xaxis.title.text": "Day (Puerto Rico local time)"},
                        ],
                    ),
                ],
            )
        ],
    )
    return _figure_to_html(figure)


def build_outage_level_figure(hourly_points: list[MetricPoint]) -> str:
    """Customers without service over time - the raw signal everything derives from."""
    if not hourly_points:
        return _empty_state("No hourly data collected yet.")

    recent_points = hourly_points[-336:]  # up to 14 days of hourly detail
    figure = graph_objects.Figure()
    figure.add_trace(
        graph_objects.Scatter(
            x=[point.period_start for point in recent_points],
            y=[point.mean_customers_without_service for point in recent_points],
            name="Mean customers out",
            mode="lines",
            line=dict(color=COLOR_PRIMARY, width=2),
            fill="tozeroy",
            fillcolor="rgba(15,111,197,0.12)",
            hovertemplate="%{y:,.0f} customers<extra>mean</extra>",
        )
    )
    figure.add_trace(
        graph_objects.Scatter(
            x=[point.period_start for point in recent_points],
            y=[point.peak_customers_without_service for point in recent_points],
            name="Peak customers out",
            mode="lines",
            line=dict(color=COLOR_ACCENT, width=1, dash="dot"),
            hovertemplate="%{y:,.0f} customers<extra>peak</extra>",
        )
    )
    figure.update_layout(
        title="Customers without service (hourly)",
        yaxis_title="Customers",
        xaxis_title=None,
        legend=dict(orientation="h", y=1.12, x=0),
    )
    return _figure_to_html(figure)


def build_daily_saidi_figure(
    daily_points: list[MetricPoint], major_event_days: list[MajorEventDayPoint]
) -> str:
    """Daily SAIDI with Major Event Days called out against the T_MED threshold."""
    if not daily_points:
        return _empty_state("No daily data yet - SAIDI appears after a full day.")

    major_event_dates = {
        point.event_date.date()
        for point in major_event_days
        if point.is_major_event_day
    }
    bar_colors = [
        COLOR_MAJOR_EVENT
        if point.period_start.date() in major_event_dates
        else COLOR_PRIMARY
        for point in daily_points
    ]

    figure = graph_objects.Figure()
    figure.add_trace(
        graph_objects.Bar(
            x=[point.period_start for point in daily_points],
            y=[point.saidi_minutes for point in daily_points],
            marker_color=bar_colors,
            name="Daily SAIDI",
            hovertemplate="%{y:.2f} min/customer<extra></extra>",
        )
    )

    threshold = next(
        (
            point.threshold_t_med
            for point in reversed(major_event_days)
            if point.threshold_t_med is not None
        ),
        None,
    )
    if threshold is not None:
        figure.add_hline(
            y=threshold,
            line=dict(color=COLOR_MAJOR_EVENT, width=1.5, dash="dash"),
            annotation_text=f"T_MED {threshold:,.1f} min",
            annotation_position="top left",
        )

    figure.update_layout(
        title="Daily SAIDI — red bars are Major Event Days (IEEE 1366)",
        yaxis_title="Minutes per customer",
        showlegend=False,
    )
    return _figure_to_html(figure)


def build_index_trend_figure(daily_points: list[MetricPoint]) -> str:
    """SAIFI and CAIDI together, with their opposing estimation biases labelled."""
    if not daily_points:
        return _empty_state("No daily data yet.")

    figure = graph_objects.Figure()
    figure.add_trace(
        graph_objects.Scatter(
            x=[point.period_start for point in daily_points],
            y=[point.saifi_estimated for point in daily_points],
            name="SAIFI (lower bound)",
            mode="lines+markers",
            line=dict(color=COLOR_PRIMARY, width=2),
            hovertemplate="%{y:.4f} interruptions/customer<extra></extra>",
        )
    )
    figure.add_trace(
        graph_objects.Scatter(
            x=[point.period_start for point in daily_points],
            y=[point.caidi_minutes for point in daily_points],
            name="CAIDI (upper bound)",
            mode="lines+markers",
            line=dict(color=COLOR_ACCENT, width=2),
            yaxis="y2",
            hovertemplate="%{y:.1f} min<extra></extra>",
        )
    )
    figure.update_layout(
        title="SAIFI and CAIDI (daily)",
        yaxis=dict(title="SAIFI"),
        yaxis2=dict(title="CAIDI (min)", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h", y=1.12, x=0),
    )
    return _figure_to_html(figure)


def build_customer_minutes_escalation_figure(hourly_points: list[MetricPoint]) -> str:
    """Cumulative customer-minutes over the recent window.

    This is the emergency-response view: how fast customer-minutes are piling
    up during an active storm or heatwave.
    """
    if not hourly_points:
        return _empty_state("No hourly data collected yet.")

    recent_points = hourly_points[-72:]  # last three days
    running_total = 0.0
    cumulative_values = []
    for point in recent_points:
        running_total += point.customer_minutes_interrupted
        cumulative_values.append(running_total)

    figure = graph_objects.Figure()
    figure.add_trace(
        graph_objects.Bar(
            x=[point.period_start for point in recent_points],
            y=[point.customer_minutes_interrupted for point in recent_points],
            name="Customer-minutes per hour",
            marker_color="rgba(15,111,197,0.55)",
            hovertemplate="%{y:,.0f} customer-min<extra>this hour</extra>",
        )
    )
    figure.add_trace(
        graph_objects.Scatter(
            x=[point.period_start for point in recent_points],
            y=cumulative_values,
            name="Cumulative",
            mode="lines",
            line=dict(color=COLOR_MAJOR_EVENT, width=2.5),
            yaxis="y2",
            hovertemplate="%{y:,.0f} customer-min<extra>cumulative</extra>",
        )
    )
    figure.update_layout(
        title="Customer-minutes escalation (last 72 hours)",
        yaxis=dict(title="Per hour"),
        yaxis2=dict(title="Cumulative", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h", y=1.12, x=0),
    )
    return _figure_to_html(figure)


def build_region_comparison_figure(region_daily_points: list[MetricPoint]) -> str:
    """SAIDI by region over the collected window."""
    if not region_daily_points:
        return _empty_state("No regional data yet.")

    saidi_by_region: dict[str, float] = {}
    for point in region_daily_points:
        saidi_by_region[point.scope_name] = (
            saidi_by_region.get(point.scope_name, 0.0) + point.saidi_minutes
        )

    ordered_regions = sorted(saidi_by_region.items(), key=lambda item: item[1])

    figure = graph_objects.Figure()
    figure.add_trace(
        graph_objects.Bar(
            x=[total for _, total in ordered_regions],
            y=[region_name for region_name, _ in ordered_regions],
            orientation="h",
            marker_color=COLOR_PRIMARY,
            hovertemplate="%{x:.1f} min/customer<extra>%{y}</extra>",
        )
    )
    figure.update_layout(
        title="Cumulative SAIDI by region (collected window)",
        xaxis_title="Minutes per customer",
        showlegend=False,
        height=420,
    )
    return _figure_to_html(figure)


def summarize_window(points: list[MetricPoint], window_days: int | None = None) -> dict:
    """Aggregate daily rows into headline figures.

    SAIDI and SAIFI are additive across periods (same denominator), so summing
    daily values yields the window total; CAIDI is then their ratio.
    """
    selected_points = points
    if window_days is not None and points:
        cutoff = points[-1].period_start - timedelta(days=window_days)
        selected_points = [
            point for point in points if point.period_start >= cutoff
        ]

    if not selected_points:
        return {
            "saidi": None,
            "saifi": None,
            "caidi": None,
            "asai": None,
            "days": 0,
            "coverage": None,
        }

    total_saidi = sum(point.saidi_minutes for point in selected_points)
    total_saifi = sum(point.saifi_estimated for point in selected_points)
    coverage_values = [
        point.coverage_ratio
        for point in selected_points
        if point.coverage_ratio is not None
    ]

    return {
        "saidi": total_saidi,
        "saifi": total_saifi,
        "caidi": (total_saidi / total_saifi) if total_saifi > 0 else None,
        "asai": (
            (1.0 - total_saidi / (len(selected_points) * MINUTES_PER_DAY)) * 100.0
        ),
        "days": len(selected_points),
        "coverage": (
            sum(coverage_values) / len(coverage_values) if coverage_values else None
        ),
    }


def _format_metric(value: float | None, suffix: str, decimals: int = 2) -> str:
    if value is None:
        return '<span class="metric-empty">—</span>'
    return f"{value:,.{decimals}f}<span class='metric-suffix'>{suffix}</span>"


def build_kpi_cards(summary: dict, window_label: str) -> str:
    cards = [
        (
            "SAIDI",
            _format_metric(summary["saidi"], " min"),
            "Minutes of interruption per customer served",
            "solid",
        ),
        (
            "SAIFI",
            _format_metric(summary["saifi"], "", 4),
            "Interruptions per customer — lower bound",
            "estimated",
        ),
        (
            "CAIDI",
            _format_metric(summary["caidi"], " min", 1),
            "Average restoration time — upper bound",
            "estimated",
        ),
        (
            "ASAI",
            _format_metric(summary["asai"], " %", 4),
            "Service availability",
            "solid",
        ),
    ]

    card_markup = "".join(
        f"""
        <div class="kpi-card">
          <div class="kpi-header">
            <span class="kpi-name">{name}</span>
            <span class="badge badge-{quality}">{quality}</span>
          </div>
          <div class="kpi-value">{value}</div>
          <div class="kpi-note">{html.escape(note)}</div>
        </div>"""
        for name, value, note, quality in cards
    )

    return f"""
    <section class="kpi-section">
      <div class="section-heading">
        <h2>Island-wide reliability</h2>
        <span class="window-label">{html.escape(window_label)}</span>
      </div>
      <div class="kpi-grid">{card_markup}</div>
    </section>"""


def build_dashboard_html(
    hourly_points: list[MetricPoint],
    daily_points: list[MetricPoint],
    monthly_points: list[MetricPoint],
    region_daily_points: list[MetricPoint],
    major_event_days: list[MajorEventDayPoint],
    hourly_points_by_cause: dict[str, list[MetricPoint]],
    daily_points_by_cause: dict[str, list[MetricPoint]],
) -> str:
    generated_at = datetime.now(timezone.utc)

    window_summary = summarize_window(daily_points, window_days=30)
    all_time_summary = summarize_window(daily_points)

    window_label = (
        f"last {window_summary['days']} day(s) with data"
        if window_summary["days"]
        else "awaiting data"
    )

    provisional_note = ""
    if major_event_days:
        latest = major_event_days[-1]
        flagged_count = sum(
            1 for point in major_event_days if point.is_major_event_day
        )
        if latest.threshold_t_med is None:
            provisional_note = (
                f"Major Event Day threshold not yet computable — "
                f"{latest.days_used_in_threshold} day(s) of non-zero SAIDI so far "
                f"(30 needed, IEEE 1366 calls for five years)."
            )
        else:
            provisional_note = (
                f"T<sub>MED</sub> = {latest.threshold_t_med:,.1f} min from "
                f"{latest.days_used_in_threshold} day(s); {flagged_count} day(s) "
                f"classified as Major Event Days."
                + (
                    " Provisional — the threshold will move as history accumulates."
                    if latest.is_provisional
                    else ""
                )
            )
    else:
        provisional_note = "No Major Event Day classification yet."

    coverage_note = (
        f"Mean sample coverage {window_summary['coverage'] * 100:,.1f}%"
        if window_summary["coverage"] is not None
        else "Coverage not yet measurable"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LUMA Puerto Rico — Grid Reliability</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js" charset="utf-8"></script>
<style>
  :root {{
    --bg: #f6f8fb; --surface: #ffffff; --text: #0f172a; --muted: #64748b;
    --border: #e2e8f0; --primary: {COLOR_PRIMARY};
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    line-height: 1.5;
  }}
  .page {{ max-width: 1180px; margin: 0 auto; padding: 32px 20px 64px; }}
  header.masthead {{ margin-bottom: 28px; }}
  header.masthead h1 {{ margin: 0 0 6px; font-size: 1.75rem; letter-spacing: -0.02em; }}
  .subtitle {{ color: var(--muted); margin: 0; font-size: 0.95rem; }}
  .meta-row {{
    display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: 14px;
    font-size: 0.82rem; color: var(--muted);
  }}
  .section-heading {{
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 12px; margin: 0 0 14px;
  }}
  .section-heading h2 {{ font-size: 1.05rem; margin: 0; letter-spacing: -0.01em; }}
  .window-label {{ font-size: 0.8rem; color: var(--muted); }}
  .kpi-grid {{
    display: grid; gap: 14px;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  }}
  .kpi-card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 16px 18px;
  }}
  .kpi-header {{ display: flex; align-items: center; justify-content: space-between; }}
  .kpi-name {{ font-weight: 650; font-size: 0.9rem; }}
  .kpi-value {{ font-size: 1.9rem; font-weight: 680; margin: 6px 0 2px; letter-spacing: -0.02em; }}
  .metric-suffix {{ font-size: 0.95rem; font-weight: 500; color: var(--muted); margin-left: 3px; }}
  .metric-empty {{ color: var(--muted); }}
  .kpi-note {{ font-size: 0.78rem; color: var(--muted); }}
  .badge {{
    font-size: 0.64rem; text-transform: uppercase; letter-spacing: 0.05em;
    padding: 3px 7px; border-radius: 999px; font-weight: 650;
  }}
  .badge-solid {{ background: #dcfce7; color: #166534; }}
  .badge-estimated {{ background: #fef3c7; color: #92400e; }}
  .card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 8px 10px; margin-top: 18px;
  }}
  .empty-state {{
    padding: 48px 16px; text-align: center; color: var(--muted);
    font-size: 0.9rem;
  }}
  .callout {{
    background: #fffbeb; border: 1px solid #fde68a; border-radius: 10px;
    padding: 14px 16px; margin-top: 26px; font-size: 0.85rem; color: #78350f;
  }}
  .callout h3 {{ margin: 0 0 6px; font-size: 0.9rem; }}
  .callout ul {{ margin: 8px 0 0; padding-left: 18px; }}
  .callout li {{ margin-bottom: 4px; }}
  footer {{ margin-top: 34px; font-size: 0.78rem; color: var(--muted); }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0b1120; --surface: #131c2e; --text: #e2e8f0; --muted: #94a3b8;
      --border: #1e293b;
    }}
    .badge-solid {{ background: #14532d; color: #bbf7d0; }}
    .badge-estimated {{ background: #78350f; color: #fde68a; }}
    .callout {{ background: #2a2010; border-color: #78350f; color: #fde68a; }}
  }}
</style>
</head>
<body>
<div class="page">
  <header class="masthead">
    <h1>LUMA Puerto Rico — Grid Reliability</h1>
    <p class="subtitle">
      IEEE 1366 reliability indices derived from the MiLUMA outage feed,
      sampled every 10 minutes.
    </p>
    <div class="meta-row">
      <span>Generated {generated_at:%Y-%m-%d %H:%M} UTC</span>
      <span>{html.escape(coverage_note)}</span>
      <span>All causes (planned, unplanned and load shed)</span>
    </div>
  </header>

  <div class="card">{build_customers_out_figure(hourly_points_by_cause, daily_points_by_cause)}</div>

  {build_kpi_cards(window_summary, window_label)}

  <div class="card">{build_outage_level_figure(hourly_points)}</div>
  <div class="card">{build_customer_minutes_escalation_figure(hourly_points)}</div>
  <div class="card">{build_daily_saidi_figure(daily_points, major_event_days)}</div>
  <div class="card">{build_index_trend_figure(daily_points)}</div>
  <div class="card">{build_region_comparison_figure(region_daily_points)}</div>

  <div class="callout">
    <h3>How to read these numbers</h3>
    <p style="margin:0">
      The source publishes how many customers are out at a point in time, not an
      interruption event log. That makes some indices exact and others bounded:
    </p>
    <ul>
      <li><strong>SAIDI, ASAI and customer-minutes are well supported</strong> —
          they integrate the customers-out curve, which sampled data measures directly.</li>
      <li><strong>SAIFI is a lower bound.</strong> It is inferred from rising
          edges, so any outage that begins and ends between two samples is invisible.</li>
      <li><strong>CAIDI is an upper bound</strong>, since it divides SAIDI by the
          understated SAIFI. It is the customer-weighted average restoration time.</li>
      <li><strong>MAIFI (momentary interruptions) is not reported</strong> — it is
          undetectable at a 10-minute sampling interval.</li>
    </ul>
    <p style="margin:8px 0 0">{provisional_note}</p>
  </div>

  <footer>
    Source: MiLUMA outage feed. Indices computed per IEEE 1366; Major Event Days
    classified with the 2.5 Beta Method. Figures over short histories are
    indicative rather than representative of long-run reliability.
  </footer>
</div>
</body>
</html>"""
