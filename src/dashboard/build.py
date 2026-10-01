"""Renders the reliability dashboard as a self-contained static HTML page.

Everything is produced from Python via Plotly, so there is almost no
JavaScript to maintain and no server to run - the figures carry their own
data. The one exception is the window-length/start-date controls on the main
chart (see `build_customers_out_section`), which need a small inline script
because Plotly's own UI widgets can't drive both a figure and the KPI cards.
"""

from __future__ import annotations

import html
import json
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

# The main chart's daily view can look back this far; the start-date slider
# never covers more than this many days even if more history exists.
MAXIMUM_HISTORY_DAYS = 365
HOURLY_HISTORY_LIMIT_HOURS = 24 * MAXIMUM_HISTORY_DAYS

# Views for the main chart. All are day-lengths - "48 h" is a 2-day window
# rendered from the finer hourly trace; the rest render from the daily trace.
# The slider always moves the window in single-day steps, regardless of view.
VIEW_OPTIONS = [
    {"label": "48 h", "days": 2, "granularity": "hour"},
    {"label": "30 d", "days": 30, "granularity": "day"},
    {"label": "60 d", "days": 60, "granularity": "day"},
    {"label": "90 d", "days": 90, "granularity": "day"},
]
DEFAULT_VIEW_INDEX = 0

# The KPI cards always summarize this many trailing days, independent of the
# main chart's slider position, except when a day-granularity view is active,
# in which case they track that view's window instead.
DEFAULT_WINDOW_LENGTH_DAYS = 30

CUSTOMERS_OUT_FIGURE_DIV_ID = "customers-out-figure"


def _figure_to_html(
    figure: graph_objects.Figure,
    div_id: str | None = None,
    margin_top: int = 48,
    height: int = 380,
) -> str:
    figure.update_layout(
        template=PLOTLY_TEMPLATE,
        margin=dict(l=56, r=24, t=margin_top, b=48),
        height=height,
        hovermode="x unified",
        font=dict(family="system-ui, -apple-system, Segoe UI, Roboto, sans-serif"),
    )
    extra_kwargs = {"div_id": div_id} if div_id else {}
    return plotly_io.to_html(
        figure,
        full_html=False,
        include_plotlyjs=False,
        config=FIGURE_CONFIG,
        **extra_kwargs,
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


def _limit_to_recent_days(
    points: list[MetricPoint], max_days: int
) -> list[MetricPoint]:
    """Every point within `max_days` of the most recent one, in whatever order

    given. These charts are independent of the main chart's window controls -
    each always shows all the history it has, capped at `max_days`.
    """
    if not points:
        return points
    cutoff = max(point.period_start for point in points) - timedelta(days=max_days)
    return [point for point in points if point.period_start >= cutoff]


def _customers_out_series(
    points: list[MetricPoint], axis: list[datetime], use_peak: bool = False
) -> list[float]:
    """Per-period customers-out values, aligned to `axis`.

    `use_peak` picks the period's peak instead of its mean. The daily trace
    uses the peak: a day's mean would average a multi-hour spike down toward
    zero, understating how bad the worst moment of that day actually was. The
    hourly trace keeps the mean, since an hour is already fine-grained enough
    that mean and peak rarely differ much.
    """
    value_by_period_start = {
        point.period_start: (
            point.peak_customers_without_service
            if use_peak
            else point.mean_customers_without_service
        )
        for point in points
    }
    return [value_by_period_start.get(period_start, 0.0) for period_start in axis]


def _build_customers_out_figure(
    hourly_points_by_cause: dict[str, list[MetricPoint]],
    daily_axis: list[datetime],
    daily_points_by_cause: dict[str, list[MetricPoint]],
) -> tuple[graph_objects.Figure, list[datetime]]:
    """Build the figure with hourly and daily trace groups baked in.

    Both granularities live in one figure as separate trace groups; the
    inline script in `build_customers_out_section` flips which group is
    visible and adjusts the axis range, rather than reloading the figure.
    """
    hourly_axis = _customers_out_axis(hourly_points_by_cause, HOURLY_HISTORY_LIMIT_HOURS)

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
                y=_customers_out_series(
                    daily_points_by_cause[cause_basis], daily_axis, use_peak=True
                ),
                name=label,
                marker_color=color,
                visible=False,
                hovertemplate="%{y:,.0f} customers (peak)<extra>" + label + "</extra>",
            )
        )

    figure.update_layout(
        title=dict(text="Customers out of service", y=0.97, yanchor="top"),
        yaxis_title="Customers out (hourly mean)",
        xaxis=dict(title="Hour (Puerto Rico local time)", autorange="reversed"),
        barmode="stack",
        legend=dict(orientation="h", y=1.1, x=0),
    )
    return figure, hourly_axis


def build_window_controls_markup(available_days: int) -> str:
    """The 48h/30d/60d/90d view buttons and the always-visible day slider.

    `available_days` only decides whether the slider starts disabled; the
    inline script recomputes its min/max/ticks in JavaScript whenever the
    view changes, since that depends on the view's window length too.
    """
    view_buttons = "".join(
        f'<button type="button" class="window-btn{" active" if index == DEFAULT_VIEW_INDEX else ""}">'
        f'{html.escape(option["label"])}</button>'
        for index, option in enumerate(VIEW_OPTIONS)
    )
    slider_disabled = "disabled" if available_days == 0 else ""

    return f"""
    <div class="window-controls">
      <div class="window-controls-row">
        <div class="window-btn-group" role="group" aria-label="Chart window">
          {view_buttons}
        </div>
        <span class="window-range-group">
          <span class="window-granularity-label" id="window-granularity-label"></span>
          <span class="window-range-label" id="window-range-label"></span>
        </span>
      </div>
      <div class="window-slider-row" id="window-slider-row">
        <label for="window-start-slider">Start date</label>
        <input type="range" id="window-start-slider" min="0" max="0" value="0"
               step="1" list="window-tick-marks" {slider_disabled}>
        <datalist id="window-tick-marks"></datalist>
      </div>
      <div class="window-slider-hint" id="window-slider-hint"></div>
    </div>"""


def build_window_controls_script(
    daily_axis: list[datetime], daily_points: list[MetricPoint]
) -> str:
    """Inline script wiring the controls to the figure and the KPI cards.

    Recomputes SAIDI/SAIFI/CAIDI/ASAI client-side using the same formulas as
    `summarize_window`, over whichever window the slider currently selects.
    """
    saidi_by_date = {point.period_start.date(): point.saidi_minutes for point in daily_points}
    saifi_by_date = {point.period_start.date(): point.saifi_estimated for point in daily_points}

    daily_data = {
        "dates": [period_start.strftime("%Y-%m-%d") for period_start in daily_axis],
        "saidi": [saidi_by_date.get(period_start.date(), 0.0) for period_start in daily_axis],
        "saifi": [saifi_by_date.get(period_start.date(), 0.0) for period_start in daily_axis],
    }
    payload = json.dumps(daily_data)
    view_options_payload = json.dumps(VIEW_OPTIONS)

    return f"""
    <script id="window-controls-data" type="application/json">{payload}</script>
    <script id="window-view-options" type="application/json">{view_options_payload}</script>
    <script>
    (function() {{
      var dailyData = JSON.parse(document.getElementById('window-controls-data').textContent);
      var viewOptions = JSON.parse(document.getElementById('window-view-options').textContent);
      var figureDiv = document.getElementById('{CUSTOMERS_OUT_FIGURE_DIV_ID}');
      var slider = document.getElementById('window-start-slider');
      var sliderHint = document.getElementById('window-slider-hint');
      var tickMarks = document.getElementById('window-tick-marks');
      var rangeLabel = document.getElementById('window-range-label');
      var granularityLabel = document.getElementById('window-granularity-label');
      var buttons = document.querySelectorAll('.window-btn');
      var availableDays = dailyData.dates.length;
      var activeView = viewOptions[{DEFAULT_VIEW_INDEX}];
      var minutesPerDay = {MINUTES_PER_DAY};
      var defaultKpiWindowDays = {DEFAULT_WINDOW_LENGTH_DAYS};

      function formatDate(isoDate) {{
        if (!isoDate) return '';
        var parts = isoDate.split('-');
        var date = new Date(Date.UTC(+parts[0], +parts[1] - 1, +parts[2]));
        return date.toLocaleDateString(undefined, {{
          month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC'
        }});
      }}

      function addDays(isoDate, deltaDays) {{
        var parts = isoDate.split('-');
        var date = new Date(Date.UTC(+parts[0], +parts[1] - 1, +parts[2]));
        date.setUTCDate(date.getUTCDate() + deltaDays);
        return date.toISOString().slice(0, 10);
      }}

      function formatMetric(value, suffix, decimals) {{
        if (value === null || value === undefined || isNaN(value)) return '—';
        return value.toLocaleString(undefined, {{
          minimumFractionDigits: decimals, maximumFractionDigits: decimals
        }}) + suffix;
      }}

      function setActiveButton(target) {{
        buttons.forEach(function(button) {{ button.classList.remove('active'); }});
        target.classList.add('active');
      }}

      function updateKpiCardsForRange(startIndex, endIndex) {{
        var slice = {{ saidi: [], saifi: [] }};
        for (var i = Math.max(startIndex, 0); i <= endIndex && i < availableDays; i++) {{
          slice.saidi.push(dailyData.saidi[i]);
          slice.saifi.push(dailyData.saifi[i]);
        }}
        var days = slice.saidi.length;
        var totalSaidi = slice.saidi.reduce(function(a, b) {{ return a + b; }}, 0);
        var totalSaifi = slice.saifi.reduce(function(a, b) {{ return a + b; }}, 0);
        var caidi = totalSaifi > 0 ? totalSaidi / totalSaifi : null;
        var asai = days > 0 ? (1 - totalSaidi / (days * minutesPerDay)) * 100 : null;

        var saidiEl = document.getElementById('kpi-saidi-value');
        var saifiEl = document.getElementById('kpi-saifi-value');
        var caidiEl = document.getElementById('kpi-caidi-value');
        var asaiEl = document.getElementById('kpi-asai-value');
        if (saidiEl) saidiEl.innerHTML = formatMetric(days ? totalSaidi : null, "<span class='metric-suffix'> min</span>", 2);
        if (saifiEl) saifiEl.innerHTML = formatMetric(days ? totalSaifi : null, "<span class='metric-suffix'></span>", 4);
        if (caidiEl) caidiEl.innerHTML = formatMetric(caidi, "<span class='metric-suffix'> min</span>", 1);
        if (asaiEl) asaiEl.innerHTML = formatMetric(asai, "<span class='metric-suffix'> %</span>", 4);

        var windowLabelEl = document.getElementById('kpi-window-label');
        if (windowLabelEl) {{
          windowLabelEl.textContent = days
            ? 'last ' + days + ' day(s) with data'
            : 'awaiting data';
        }}
      }}

      // The KPI cards are a stable headline figure - they track the exact
      // slider window for day-length views, but stay on the standard 30-day
      // summary for the 48h view, since 2 days isn't a meaningful sample.
      function updateKpiCardsDefault() {{
        var endIndex = availableDays - 1;
        var startIndex = Math.max(0, availableDays - defaultKpiWindowDays);
        updateKpiCardsForRange(startIndex, endIndex);
      }}

      // The window always spans exactly the active view's number of days -
      // the slider only pans which days are shown, it never changes that
      // span. offset counts days back from the most recent available day: 0
      // is today (slider at the left); larger values move the window's near
      // edge - the "first day" shown at the chart's left edge - into the
      // past (slider moves right). Because the window length is fixed, the
      // slider's own range depends on the active view (a short window has
      // more valid positions than a long one).
      function applyView(offset) {{
        var length = activeView.days;
        var maxOffset = Math.max(0, availableDays - length);
        offset = Math.min(Math.max(offset, 0), maxOffset);
        if (String(slider.value) !== String(offset)) slider.value = offset;

        var endIndex = availableDays - 1 - offset;
        var startIndex = Math.max(0, endIndex - length + 1);
        if (endIndex < 0) return;

        var startDate = dailyData.dates[startIndex];
        var endDate = dailyData.dates[endIndex];
        var actualDays = endIndex - startIndex + 1;
        var dayCountSuffix = ' (' + actualDays + (actualDays === 1 ? ' day)' : ' days)');

        if (activeView.granularity === 'hour') {{
          Plotly.restyle(figureDiv, {{ visible: [true, true, true, false, false, false] }});
          Plotly.relayout(figureDiv, {{
            'xaxis.autorange': false,
            // range given newest-first so the most recent hour renders on the
            // left; the upper bound is midnight after endDate so that day's
            // hours are fully included.
            'xaxis.range': [addDays(endDate, 1), startDate],
            'xaxis.title.text': 'Hour (Puerto Rico local time)',
            'yaxis.title.text': 'Customers out (hourly mean)',
            'yaxis.autorange': true
          }});
          granularityLabel.textContent = 'Hourly data (mean per hour)';
          updateKpiCardsDefault();
        }} else {{
          Plotly.restyle(figureDiv, {{ visible: [false, false, false, true, true, true] }});
          Plotly.relayout(figureDiv, {{
            'xaxis.autorange': false,
            'xaxis.range': [endDate, startDate],
            'xaxis.title.text': 'Day (Puerto Rico local time)',
            'yaxis.title.text': 'Customers out (daily peak)',
            'yaxis.autorange': true
          }});
          granularityLabel.textContent = 'Daily data (peak per day)';
          updateKpiCardsForRange(startIndex, endIndex);
        }}

        rangeLabel.textContent = formatDate(startDate) + ' – ' + formatDate(endDate) + dayCountSuffix;
      }}

      // Ticks and slider bounds depend on the active view's window length -
      // a short window has many valid start dates, a long one has few.
      function configureSliderForView() {{
        var length = activeView.days;
        var maxOffset = Math.max(0, availableDays - length);
        slider.min = 0;
        slider.max = maxOffset;
        slider.value = 0;
        slider.disabled = availableDays === 0;

        tickMarks.innerHTML = '';
        for (var i = 0; i <= maxOffset; i++) {{
          var option = document.createElement('option');
          option.value = i;
          option.label = formatDate(dailyData.dates[availableDays - 1 - i]);
          tickMarks.appendChild(option);
        }}

        if (availableDays < length) {{
          sliderHint.textContent = 'Only ' + availableDays + ' day(s) of data available - showing full history.';
        }} else if (maxOffset === 0) {{
          sliderHint.textContent = 'Exactly ' + length + ' day(s) of data available.';
        }} else {{
          sliderHint.textContent = 'Drag to any of the ' + (maxOffset + 1) + ' available views (one tick per day).';
        }}
      }}

      buttons.forEach(function(button, index) {{
        button.addEventListener('click', function() {{
          setActiveButton(button);
          activeView = viewOptions[index];
          configureSliderForView();
          applyView(0);
        }});
      }});

      slider.addEventListener('input', function() {{
        applyView(parseInt(slider.value, 10));
      }});

      configureSliderForView();
      applyView(0);
    }})();
    </script>"""


def build_customers_out_section(
    hourly_points_by_cause: dict[str, list[MetricPoint]],
    daily_points_by_cause: dict[str, list[MetricPoint]],
    daily_points: list[MetricPoint],
) -> str:
    """Controls + figure for customers-out-of-service: 48h / 30d / 60d / 90d views.

    The view buttons and start-date slider are plain HTML/JS (see
    `build_window_controls_markup`/`build_window_controls_script`) driving one
    Plotly figure that carries both granularities as trace groups.
    """
    daily_axis = _customers_out_axis(daily_points_by_cause, MAXIMUM_HISTORY_DAYS)
    if not daily_axis and not _customers_out_axis(
        hourly_points_by_cause, HOURLY_HISTORY_LIMIT_HOURS
    ):
        return _empty_state("No outage data collected yet.")

    figure, _hourly_axis = _build_customers_out_figure(
        hourly_points_by_cause, daily_axis, daily_points_by_cause
    )
    figure_html = _figure_to_html(
        figure, div_id=CUSTOMERS_OUT_FIGURE_DIV_ID, margin_top=72, height=400
    )

    controls_html = build_window_controls_markup(len(daily_axis))
    script_html = build_window_controls_script(daily_axis, daily_points)

    return f"{controls_html}{figure_html}{script_html}"


def build_outage_level_figure(hourly_points: list[MetricPoint]) -> str:
    """Customers without service over time - the raw signal everything derives from."""
    if not hourly_points:
        return _empty_state("No hourly data collected yet.")

    recent_points = _limit_to_recent_days(hourly_points, MAXIMUM_HISTORY_DAYS)
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

    daily_points = _limit_to_recent_days(daily_points, MAXIMUM_HISTORY_DAYS)
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

    daily_points = _limit_to_recent_days(daily_points, MAXIMUM_HISTORY_DAYS)
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
    """Cumulative customer-minutes, over as much history as is available.

    This is the emergency-response view: how fast customer-minutes are piling
    up during an active storm or heatwave.
    """
    if not hourly_points:
        return _empty_state("No hourly data collected yet.")

    recent_points = _limit_to_recent_days(hourly_points, MAXIMUM_HISTORY_DAYS)
    span_days = (recent_points[-1].period_start - recent_points[0].period_start).days
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
        title=f"Customer-minutes escalation (last {span_days} day(s))",
        yaxis=dict(title="Per hour"),
        yaxis2=dict(title="Cumulative", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h", y=1.12, x=0),
    )
    return _figure_to_html(figure)


def build_region_comparison_figure(region_daily_points: list[MetricPoint]) -> str:
    """SAIDI by region, over as much history as is available (up to 1 year)."""
    if not region_daily_points:
        return _empty_state("No regional data yet.")

    region_daily_points = _limit_to_recent_days(region_daily_points, MAXIMUM_HISTORY_DAYS)
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
        title="Cumulative SAIDI by region (last year of data)",
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
            "saidi",
            "SAIDI",
            _format_metric(summary["saidi"], " min"),
            "Minutes of interruption per customer served",
            "solid",
        ),
        (
            "saifi",
            "SAIFI",
            _format_metric(summary["saifi"], "", 4),
            "Interruptions per customer — lower bound",
            "estimated",
        ),
        (
            "caidi",
            "CAIDI",
            _format_metric(summary["caidi"], " min", 1),
            "Average restoration time — upper bound",
            "estimated",
        ),
        (
            "asai",
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
          <div class="kpi-value" id="kpi-{slug}-value">{value}</div>
          <div class="kpi-note">{html.escape(note)}</div>
        </div>"""
        for slug, name, value, note, quality in cards
    )

    return f"""
    <section class="kpi-section">
      <div class="section-heading">
        <h2>Island-wide reliability</h2>
        <span class="window-label" id="kpi-window-label">{html.escape(window_label)}</span>
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
  .window-controls {{ padding: 6px 8px 0; }}
  .window-controls-row {{
    display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
    gap: 10px;
  }}
  .window-btn-group {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .window-btn {{
    font: inherit; font-size: 0.82rem; font-weight: 600; cursor: pointer;
    background: var(--surface); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 6px 12px;
  }}
  .window-btn:hover {{ border-color: var(--primary); }}
  .window-btn.active {{ background: var(--primary); border-color: var(--primary); color: #fff; }}
  .window-range-group {{ display: flex; align-items: center; gap: 8px; }}
  .window-range-label {{ font-size: 0.82rem; color: var(--muted); font-weight: 600; }}
  .window-granularity-label {{
    font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.04em;
    font-weight: 650; color: var(--primary); background: color-mix(in srgb, var(--primary) 14%, transparent);
    border-radius: 999px; padding: 3px 8px;
  }}
  .window-slider-row {{
    display: flex; align-items: center; gap: 12px; margin-top: 10px; padding: 0 2px;
  }}
  .window-slider-row label {{ font-size: 0.78rem; color: var(--muted); white-space: nowrap; }}
  .window-slider-row input[type="range"] {{ flex: 1; accent-color: var(--muted); }}
  .window-slider-row input[type="range"]:disabled {{ opacity: 0.5; }}
  .window-slider-hint {{
    font-size: 0.74rem; color: var(--muted); margin-top: 4px; padding: 0 2px;
    min-height: 1.1em;
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

  <div class="card">{build_customers_out_section(hourly_points_by_cause, daily_points_by_cause, daily_points)}</div>

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
