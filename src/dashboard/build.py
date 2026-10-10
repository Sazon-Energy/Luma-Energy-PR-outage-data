"""Renders the reliability dashboard as self-contained static HTML pages,
styled to match sazonenergy.com.

Everything is produced from Python via Plotly, so there is almost no
JavaScript to maintain and no server to run - the figures carry their own
data. The one exception is the window-length/start-date controls on the main
chart (see `build_customers_out_section`), which need a small inline script
because Plotly's own UI widgets can't drive both a figure and the KPI cards.
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta, timezone

import plotly.graph_objects as graph_objects
import plotly.io as plotly_io

from src.dashboard.data import MajorEventDayPoint, MetricPoint
from src.metrics.reliability import PUERTO_RICO_TIMEZONE

PLOTLY_TEMPLATE = "plotly_white"
FIGURE_CONFIG = {"displayModeBar": False, "responsive": True}

# Sazon Energy palette (sazonenergy.com): brand orange, slate secondary, and
# the deep rust end of the brand gradient for Major Event Days.
COLOR_PRIMARY = "#ff9933"
COLOR_PRIMARY_FILL = "rgba(255,153,51,0.18)"
COLOR_PRIMARY_BAR = "rgba(255,153,51,0.6)"
COLOR_ACCENT = "#1e293b"
COLOR_MAJOR_EVENT = "#8b2500"
COLOR_MUTED = "#94a3b8"
FONT_FAMILY = "Inter, -apple-system, BlinkMacSystemFont, Segoe UI, system-ui, sans-serif"

# Bottom-to-top stack order for build_customers_out_figure.
CUSTOMERS_OUT_SEGMENTS = [
    ("unplanned", "Unplanned", COLOR_PRIMARY),
    ("planned", "Planned", COLOR_ACCENT),
    ("load_shed", "Load shed", COLOR_MUTED),
]

MINUTES_PER_DAY = 1440

# LUMA publishes a new outage figure every 10 minutes, so a day holds at most
# this many distinct measurements. Sample coverage is captured ÷ possible.
SAMPLE_INTERVAL_MINUTES = 10
EXPECTED_SAMPLES_PER_DAY = MINUTES_PER_DAY // SAMPLE_INTERVAL_MINUTES

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

CUSTOMERS_OUT_FIGURE_DIV_ID = "customers-out-figure"


def _figure_to_html(
    figure: graph_objects.Figure,
    div_id: str | None = None,
    margin_top: int = 48,
    height: int = 380,
    allow_selection: bool = False,
) -> str:
    figure.update_layout(
        template=PLOTLY_TEMPLATE,
        margin=dict(l=56, r=24, t=margin_top, b=48),
        height=height,
        hovermode="x unified",
        font=dict(family=FONT_FAMILY, color="#1f2937"),
        title_font=dict(size=15, color="#1e293b"),
    )
    if not allow_selection:
        # Hover read-outs stay, but legend toggling and zoom are switched off
        # so these charts always show the full period, exactly as published.
        figure.update_layout(
            dragmode=False,
            legend=dict(itemclick=False, itemdoubleclick=False),
        )
        figure.update_xaxes(fixedrange=True)
        figure.update_yaxes(fixedrange=True)
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
        # The chart title lives in the HTML above the figure (see
        # `build_customers_out_section`) so it can sit centered as a label.
        yaxis_title="Customers out (hourly mean)",
        xaxis=dict(title="Hour (Puerto Rico local time)", autorange="reversed"),
        barmode="stack",
        legend=dict(orientation="h", y=1.1, x=0),
    )
    return figure, hourly_axis


def count_captured_samples_by_day(
    snapshot_timestamps: list[datetime],
) -> dict[date, int]:
    """Distinct 10-minute slots holding at least one snapshot, per local day.

    Counting slots rather than raw rows means an off-cadence or duplicate
    publish can never push a day past `EXPECTED_SAMPLES_PER_DAY`.
    """
    captured_slots_by_day: dict[date, set[int]] = {}
    for snapshot_timestamp in snapshot_timestamps:
        local_moment = snapshot_timestamp.astimezone(PUERTO_RICO_TIMEZONE)
        minute_of_day = local_moment.hour * 60 + local_moment.minute
        captured_slots_by_day.setdefault(local_moment.date(), set()).add(
            minute_of_day // SAMPLE_INTERVAL_MINUTES
        )
    return {
        local_date: len(captured_slots)
        for local_date, captured_slots in captured_slots_by_day.items()
    }


def expected_samples_for_day(local_date: date, generated_at: datetime) -> int:
    """How many measurements LUMA could have published on `local_date`.

    A full past day has `EXPECTED_SAMPLES_PER_DAY`; the current day only
    counts the slots that have begun by build time, so it isn't marked down
    for measurements that cannot exist yet.
    """
    local_now = generated_at.astimezone(PUERTO_RICO_TIMEZONE)
    if local_date < local_now.date():
        return EXPECTED_SAMPLES_PER_DAY
    if local_date > local_now.date():
        return 0
    minute_of_day = local_now.hour * 60 + local_now.minute
    return min(
        EXPECTED_SAMPLES_PER_DAY, minute_of_day // SAMPLE_INTERVAL_MINUTES + 1
    )


def build_window_controls_markup(available_days: int) -> str:
    """Date range + coverage, the 48h/30d/60d/90d view buttons, the
    always-visible day slider, and the chart title with its reset button.

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
        <div class="window-summary">
          <span class="window-range-label" id="window-range-label"></span>
          <span class="coverage-stat tooltip-anchor" tabindex="0" aria-describedby="window-coverage-tooltip">
            <span class="coverage-name">Coverage</span>
            <span class="coverage-value" id="window-coverage-value">—</span>
            <span class="hover-tooltip" id="window-coverage-tooltip" role="tooltip"></span>
          </span>
        </div>
      </div>
      <div class="window-slider-row" id="window-slider-row">
        <label for="window-start-slider">Start date</label>
        <input type="range" id="window-start-slider" min="0" max="0" value="0"
               step="1" list="window-start-dates" {slider_disabled}>
        <datalist id="window-start-dates"></datalist>
      </div>
      <div class="chart-title-row">
        <h3 class="chart-title">Customers out of service</h3>
        <button type="button" class="cause-reset-btn" id="cause-focus-reset" hidden>
          Show all outage types
        </button>
      </div>
    </div>"""


def build_window_controls_script(
    daily_axis: list[datetime],
    snapshot_timestamps: list[datetime],
    generated_at: datetime,
) -> str:
    """Inline script wiring the view buttons and slider to the figure, and
    keeping the date range and sample coverage in step with the window shown.
    """
    captured_samples_by_day = count_captured_samples_by_day(snapshot_timestamps)
    local_dates = [
        period_start.astimezone(PUERTO_RICO_TIMEZONE).date() for period_start in daily_axis
    ]

    daily_data = {
        "dates": [period_start.strftime("%Y-%m-%d") for period_start in daily_axis],
        "capturedSamples": [
            captured_samples_by_day.get(local_date, 0) for local_date in local_dates
        ],
        "expectedSamples": [
            expected_samples_for_day(local_date, generated_at) for local_date in local_dates
        ],
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
      var startDateOptions = document.getElementById('window-start-dates');
      var causeResetButton = document.getElementById('cause-focus-reset');
      var rangeLabel = document.getElementById('window-range-label');
      var coverageValue = document.getElementById('window-coverage-value');
      var coverageTooltip = document.getElementById('window-coverage-tooltip');
      var buttons = document.querySelectorAll('.window-btn');
      var availableDays = dailyData.dates.length;
      var activeView = viewOptions[{DEFAULT_VIEW_INDEX}];
      var sampleIntervalMinutes = {SAMPLE_INTERVAL_MINUTES};
      var expectedSamplesPerDay = {EXPECTED_SAMPLES_PER_DAY};
      // The figure holds one trace per outage type for the hourly group,
      // followed by the same types again for the daily group.
      var outageTypeCount = {len(CUSTOMERS_OUT_SEGMENTS)};

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

      // Pooled over the window (total captured ÷ total possible), not an
      // average of daily percentages, so a partial day can't skew it.
      function updateSampleCoverageForRange(startIndex, endIndex) {{
        var capturedTotal = 0;
        var expectedTotal = 0;
        for (var dayIndex = Math.max(startIndex, 0); dayIndex <= endIndex && dayIndex < availableDays; dayIndex++) {{
          capturedTotal += dailyData.capturedSamples[dayIndex];
          expectedTotal += dailyData.expectedSamples[dayIndex];
        }}
        var cadenceNote = 'LUMA publishes outage data every ' + sampleIntervalMinutes +
          ' minutes (up to ' + expectedSamplesPerDay + ' measurements per day).';
        if (expectedTotal === 0) {{
          coverageValue.textContent = '—';
          coverageTooltip.textContent = 'Not yet measurable for the selected period. ' + cadenceNote;
          return;
        }}
        coverageValue.textContent = formatMetric(capturedTotal / expectedTotal * 100, '%', 1);
        coverageTooltip.textContent =
          capturedTotal.toLocaleString() + ' of ' + expectedTotal.toLocaleString() +
          ' possible measurements captured in the selected period. ' + cadenceNote;
      }}

      // Which outage types are shown, read from whichever trace group is
      // currently on screen: true, or 'legendonly' once hidden through the
      // legend (a click hides one type; a double-click focuses on one type).
      function readOutageTypeVisibility() {{
        var visibility = [];
        for (var typeIndex = 0; typeIndex < outageTypeCount; typeIndex++) {{
          var hourlyVisible = figureDiv.data[typeIndex].visible;
          var dailyVisible = figureDiv.data[typeIndex + outageTypeCount].visible;
          var shownVisible = hourlyVisible !== false ? hourlyVisible : dailyVisible;
          visibility.push(shownVisible === 'legendonly' ? 'legendonly' : true);
        }}
        return visibility;
      }}

      function updateCauseResetButton() {{
        causeResetButton.hidden = readOutageTypeVisibility().every(function(visible) {{
          return visible === true;
        }});
      }}

      // Applies the given per-type visibility to the active granularity's
      // trace group and hides the other group entirely, so a legend focus
      // carries across start-date and view changes.
      function showTraceGroup(granularity, outageTypeVisibility) {{
        var hiddenGroup = outageTypeVisibility.map(function() {{ return false; }});
        var visible = granularity === 'hour'
          ? outageTypeVisibility.concat(hiddenGroup)
          : hiddenGroup.concat(outageTypeVisibility);
        Plotly.restyle(figureDiv, {{ visible: visible }});
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

        showTraceGroup(activeView.granularity, readOutageTypeVisibility());
        if (activeView.granularity === 'hour') {{
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
        }} else {{
          Plotly.relayout(figureDiv, {{
            'xaxis.autorange': false,
            'xaxis.range': [endDate, startDate],
            'xaxis.title.text': 'Day (Puerto Rico local time)',
            'yaxis.title.text': 'Customers out (daily peak)',
            'yaxis.autorange': true
          }});
        }}

        rangeLabel.textContent = formatDate(startDate) + ' – ' + formatDate(endDate) + dayCountSuffix;
        updateSampleCoverageForRange(startIndex, endIndex);
        updateCauseResetButton();
      }}

      // Slider bounds depend on the active view's window length - a short
      // window has many valid start dates, a long one has few.
      function configureSliderForView() {{
        var length = activeView.days;
        var maxOffset = Math.max(0, availableDays - length);
        slider.min = 0;
        slider.max = maxOffset;
        slider.value = 0;
        slider.disabled = availableDays === 0;

        startDateOptions.innerHTML = '';
        for (var startOffset = 0; startOffset <= maxOffset; startOffset++) {{
          var option = document.createElement('option');
          option.value = startOffset;
          option.label = formatDate(dailyData.dates[availableDays - 1 - startOffset]);
          startDateOptions.appendChild(option);
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

      causeResetButton.addEventListener('click', function() {{
        showTraceGroup(
          activeView.granularity,
          readOutageTypeVisibility().map(function() {{ return true; }})
        );
        updateCauseResetButton();
      }});

      // Legend clicks and double-clicks surface as restyle events.
      figureDiv.on('plotly_restyle', updateCauseResetButton);

      configureSliderForView();
      applyView(0);
    }})();
    </script>"""


def build_customers_out_section(
    hourly_points_by_cause: dict[str, list[MetricPoint]],
    daily_points_by_cause: dict[str, list[MetricPoint]],
    snapshot_timestamps: list[datetime],
    generated_at: datetime,
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
        figure,
        div_id=CUSTOMERS_OUT_FIGURE_DIV_ID,
        margin_top=40,
        height=400,
        allow_selection=True,
    )

    controls_html = build_window_controls_markup(len(daily_axis))
    script_html = build_window_controls_script(
        daily_axis, snapshot_timestamps, generated_at
    )

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
            fillcolor=COLOR_PRIMARY_FILL,
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
            marker_color=COLOR_PRIMARY_BAR,
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
            point for point in points if point.period_start > cutoff
        ]

    if not selected_points:
        return {
            "saidi": None,
            "saifi": None,
            "caidi": None,
            "asai": None,
            "days": 0,
            "first_period_start": None,
            "last_period_start": None,
        }

    total_saidi = sum(point.saidi_minutes for point in selected_points)
    total_saifi = sum(point.saifi_estimated for point in selected_points)

    return {
        "saidi": total_saidi,
        "saifi": total_saifi,
        "caidi": (total_saidi / total_saifi) if total_saifi > 0 else None,
        "asai": (
            (1.0 - total_saidi / (len(selected_points) * MINUTES_PER_DAY)) * 100.0
        ),
        "days": len(selected_points),
        "first_period_start": selected_points[0].period_start,
        "last_period_start": selected_points[-1].period_start,
    }


def _format_metric(value: float | None, suffix: str, decimals: int = 2) -> str:
    if value is None:
        return '<span class="metric-empty">—</span>'
    return f"{value:,.{decimals}f}<span class='metric-suffix'>{suffix}</span>"


def _format_local_date(moment: datetime) -> str:
    local_date = moment.astimezone(PUERTO_RICO_TIMEZONE).date()
    return f"{local_date:%b} {local_date.day}, {local_date.year}"


def build_kpi_cards(summary: dict) -> str:
    cards = [
        (
            "saidi",
            "SAIDI",
            "System Average Interruption Duration Index",
            _format_metric(summary["saidi"], " min"),
            "Minutes of interruption per customer served",
            "solid",
        ),
        (
            "saifi",
            "SAIFI",
            "System Average Interruption Frequency Index",
            _format_metric(summary["saifi"], "", 4),
            "Interruptions per customer — lower bound",
            "estimated",
        ),
        (
            "caidi",
            "CAIDI",
            "Customer Average Interruption Duration Index",
            _format_metric(summary["caidi"], " min", 1),
            "Average restoration time — upper bound",
            "estimated",
        ),
        (
            "asai",
            "ASAI",
            "Average Service Availability Index",
            _format_metric(summary["asai"], " %", 4),
            "Service availability",
            "solid",
        ),
    ]

    card_markup = "".join(
        f"""
        <div class="kpi-card">
          <div class="kpi-header">
            <span class="kpi-name tooltip-anchor" tabindex="0" aria-describedby="kpi-{slug}-full-name">
              {name}
              <span class="hover-tooltip" id="kpi-{slug}-full-name" role="tooltip">{html.escape(full_name)}</span>
            </span>
            <span class="status-badge status-{quality}"><span class="status-dot"></span>{quality}</span>
          </div>
          <div class="kpi-value">{value}</div>
          <div class="kpi-note">{html.escape(note)}</div>
        </div>"""
        for slug, name, full_name, value, note, quality in cards
    )
    return f'<div class="kpi-grid">{card_markup}</div>'


def _major_event_day_note(major_event_days: list[MajorEventDayPoint]) -> str:
    """Status of the Major Event Day threshold, as trusted HTML."""
    if not major_event_days:
        return "No Major Event Day classification yet."

    latest = major_event_days[-1]
    if latest.threshold_t_med is None:
        return (
            f"Major Event Day threshold not yet computable — "
            f"{latest.days_used_in_threshold} day(s) of non-zero SAIDI so far "
            f"(30 needed, IEEE 1366 calls for five years)."
        )

    flagged_count = sum(1 for point in major_event_days if point.is_major_event_day)
    return (
        f"T<sub>MED</sub> = {latest.threshold_t_med:,.1f} min from "
        f"{latest.days_used_in_threshold} day(s); {flagged_count} day(s) "
        f"classified as Major Event Days."
        + (
            " Provisional — the threshold will move as history accumulates."
            if latest.is_provisional
            else ""
        )
    )


# Shared by both pages. A plain string rather than an f-string so the CSS
# braces need no escaping. Palette and type follow sazonenergy.com.
PAGE_STYLESHEET = """
  :root {
    --primary: #ff9933; --primary-dark: #e55a00; --secondary: #1e293b;
    --text: #1f2937; --muted: #6b7280; --bg: #ffffff; --bg-secondary: #f8fafc;
    --border: #e5e7eb;
    --shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
    --gradient: linear-gradient(135deg, #ff9933 0%, #8b2500 100%);
    --available: #10b981; --coming-soon: #f59e0b;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    line-height: 1.6; color: var(--text); background: var(--bg-secondary);
    overflow-x: hidden;
  }

  /* Header */
  .site-header {
    position: sticky; top: 0; z-index: 1000;
    background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--border);
  }
  .site-nav {
    display: flex; justify-content: space-between; align-items: center; gap: 1rem;
    max-width: 1200px; margin: 0 auto; padding: 1rem 2rem;
  }
  .logo { font-size: 1.5rem; font-weight: 700; color: var(--primary); text-decoration: none; }
  .nav-links { display: flex; list-style: none; gap: 2rem; }
  .nav-links a {
    position: relative; text-decoration: none; color: var(--text); font-weight: 500;
    transition: color 0.3s ease;
  }
  .nav-links a::after {
    content: ''; position: absolute; bottom: -5px; left: 0; width: 0; height: 2px;
    background: var(--primary); transition: width 0.3s ease;
  }
  .nav-links a:hover, .nav-links a[aria-current="page"] { color: var(--primary); }
  .nav-links a:hover::after, .nav-links a[aria-current="page"]::after { width: 100%; }

  /* Hero band */
  .hero { background: var(--gradient); color: #fff; text-align: center; padding: 3.5rem 2rem; }
  .hero h1 { font-size: clamp(1.9rem, 4vw, 2.75rem); font-weight: 700; line-height: 1.2; }
  .hero p {
    max-width: 640px; margin: 0.75rem auto 0; font-size: 1.1rem;
    color: rgba(255, 255, 255, 0.9);
  }

  /* Content */
  .section { max-width: 1200px; margin: 0 auto; padding: 3rem 2rem; }
  .section-title {
    text-align: center; font-size: 2rem; font-weight: 700; color: var(--secondary);
    line-height: 1.25;
  }
  .section-subtitle { text-align: center; color: var(--muted); margin: 0.5rem 0 2rem; }
  .panel {
    background: var(--bg); border: 1px solid var(--border); border-radius: 15px;
    box-shadow: var(--shadow); padding: 1.25rem; margin-top: 2rem;
  }
  .panel:first-child { margin-top: 0; }
  .empty-state { padding: 48px 16px; text-align: center; color: var(--muted); font-size: 0.9rem; }
  .cta-row { text-align: center; margin-top: 2.5rem; }
  .cta-button {
    display: inline-block; background: var(--gradient); color: #fff;
    padding: 0.9rem 1.9rem; border-radius: 50px; font-weight: 600; text-decoration: none;
    transition: transform 0.3s ease, box-shadow 0.3s ease;
  }
  .cta-button:hover { transform: translateY(-2px); box-shadow: 0 10px 20px rgba(0, 0, 0, 0.2); }
  .back-link {
    display: inline-block; margin-bottom: 1.5rem; color: var(--primary-dark);
    font-weight: 600; text-decoration: none;
  }
  .back-link:hover { text-decoration: underline; text-underline-offset: 4px; }

  /* Main chart controls */
  .window-controls-row {
    display: flex; flex-wrap: wrap; align-items: center; gap: 12px 16px;
  }
  .window-btn-group { display: flex; gap: 6px; }
  .window-btn {
    font: inherit; font-size: 0.85rem; font-weight: 600; cursor: pointer;
    min-width: 3.75rem; text-align: center;
    background: var(--bg); color: var(--text); border: 1px solid var(--border);
    border-radius: 50px; padding: 6px 14px;
    transition: border-color 0.3s ease, color 0.3s ease;
  }
  .window-btn:hover { border-color: var(--primary); color: var(--primary-dark); }
  .window-btn.active { background: var(--secondary); border-color: var(--secondary); color: #fff; }
  .window-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
  /* Fixed width so nothing after it shifts as the date range text changes. */
  .window-range-label {
    font-size: 0.85rem; color: var(--muted); font-weight: 600;
    font-variant-numeric: tabular-nums; min-width: 17rem;
  }
  .coverage-stat {
    display: inline-flex; align-items: baseline; gap: 8px;
    border: 1px solid var(--border); border-radius: 50px; padding: 4px 14px;
    background: var(--bg-secondary);
  }
  .coverage-name {
    font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.08em;
    font-weight: 700; color: var(--muted);
  }
  .coverage-value {
    font-size: 0.9rem; font-weight: 700; color: var(--secondary);
    font-variant-numeric: tabular-nums;
  }
  .window-slider-row { display: flex; align-items: center; gap: 12px; margin-top: 14px; }
  .window-slider-row label { font-size: 0.8rem; color: var(--muted); white-space: nowrap; }
  .window-slider-row input[type="range"] { flex: 1; min-width: 0; accent-color: var(--primary); }
  .window-slider-row input[type="range"]:disabled { opacity: 0.5; }
  .chart-title-row {
    display: grid; grid-template-columns: 1fr auto 1fr; align-items: center;
    gap: 8px; margin-top: 1.25rem; min-height: 32px;
  }
  .chart-title {
    grid-column: 2; text-align: center; font-size: 0.85rem; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.1em; color: var(--secondary);
  }
  .cause-reset-btn {
    grid-column: 3; justify-self: end;
    font: inherit; font-size: 0.78rem; font-weight: 600; cursor: pointer;
    background: var(--bg); color: var(--primary-dark); border: 1px solid var(--primary);
    border-radius: 50px; padding: 4px 12px;
  }
  .cause-reset-btn:hover { background: rgba(255, 153, 51, 0.1); }
  .cause-reset-btn[hidden] { display: none; }

  /* KPI cards */
  .kpi-grid { display: grid; gap: 1.5rem; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); }
  .kpi-card {
    background: var(--bg); border: 1px solid var(--border); border-radius: 15px;
    box-shadow: var(--shadow); padding: 1.5rem;
  }
  .kpi-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .kpi-name { font-weight: 700; font-size: 1rem; color: var(--secondary); }
  .kpi-value {
    font-size: 2rem; font-weight: 700; color: var(--secondary); margin: 0.5rem 0 0.25rem;
    letter-spacing: -0.02em; font-variant-numeric: tabular-nums;
  }
  .metric-suffix { font-size: 0.95rem; font-weight: 500; color: var(--muted); margin-left: 3px; }
  .metric-empty { color: var(--muted); }
  .kpi-note { font-size: 0.85rem; color: var(--muted); }
  .status-badge {
    display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.3rem 0.65rem;
    border-radius: 20px; font-size: 0.68rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.05em;
  }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; }
  .status-solid {
    background: rgba(16, 185, 129, 0.1); color: var(--available);
    border: 1px solid rgba(16, 185, 129, 0.2);
  }
  .status-solid .status-dot { background: var(--available); }
  .status-estimated {
    background: rgba(245, 158, 11, 0.1); color: #b45309;
    border: 1px solid rgba(245, 158, 11, 0.2);
  }
  .status-estimated .status-dot { background: var(--coming-soon); }

  /* Helper panel */
  .callout {
    background: var(--bg); border: 1px solid var(--border); border-left: 4px solid var(--primary);
    border-radius: 15px; padding: 1.5rem 1.75rem; margin-top: 2rem;
    color: var(--muted); font-size: 0.95rem; line-height: 1.7;
  }
  .callout h3 { color: var(--secondary); font-size: 1.1rem; margin-bottom: 0.5rem; }
  .callout ul { margin: 0.75rem 0 0; padding-left: 1.25rem; }
  .callout li { margin-bottom: 0.35rem; }
  .callout strong { color: var(--text); }
  .callout .callout-note { margin-top: 0.75rem; }

  /* Tooltips: put `tooltip-anchor` on the element and a `hover-tooltip`
     child inside it. Opens below by default; `tooltip-above` flips it. */
  .tooltip-anchor { position: relative; }
  .tooltip-anchor:focus { outline: 2px solid var(--primary); outline-offset: 3px; border-radius: 6px; }
  .tooltip-anchor:focus:not(:focus-visible) { outline: none; }
  .hover-tooltip {
    position: absolute; top: calc(100% + 8px); left: 0; z-index: 1100;
    width: max-content; max-width: min(300px, 80vw); padding: 10px 12px; border-radius: 10px;
    background: var(--secondary); color: #fff; text-align: left;
    font-size: 0.78rem; font-weight: 500; line-height: 1.5;
    text-transform: none; letter-spacing: normal;
    box-shadow: 0 10px 25px rgba(15, 23, 42, 0.25);
    opacity: 0; visibility: hidden; pointer-events: none;
    transition: opacity 150ms ease;
  }
  .tooltip-anchor:hover .hover-tooltip,
  .tooltip-anchor:focus .hover-tooltip { opacity: 1; visibility: visible; }
  .tooltip-above { top: auto; bottom: calc(100% + 10px); left: 50%; transform: translateX(-50%); }
  .tooltip-light { background: var(--bg); color: var(--text); max-width: min(420px, 88vw); }
  .tooltip-line { display: block; }
  .tooltip-line + .tooltip-line { margin-top: 0.5rem; }

  /* Footer */
  .site-footer {
    background: var(--secondary); color: rgba(255, 255, 255, 0.85);
    text-align: center; padding: 2.5rem 2rem; margin-top: 3rem; font-size: 0.9rem;
  }
  .footer-generated { display: inline-flex; align-items: center; gap: 8px; cursor: default; }
  .info-icon {
    display: inline-flex; align-items: center; justify-content: center;
    width: 18px; height: 18px; border-radius: 50%;
    border: 1.5px solid rgba(255, 255, 255, 0.6); font-size: 0.7rem; font-weight: 700;
    font-style: normal;
  }

  @media (max-width: 768px) {
    .site-nav { padding: 0.85rem 1rem; }
    .logo { font-size: 1.2rem; }
    .nav-links { gap: 1rem; font-size: 0.9rem; }
    .hero { padding: 2.5rem 1rem; }
    .section { padding: 2rem 1rem; }
    .section-title { font-size: 1.6rem; }
    .panel { padding: 1rem 0.75rem; }
    .chart-title-row { grid-template-columns: 1fr; justify-items: center; }
    .chart-title, .cause-reset-btn { grid-column: 1; justify-self: center; }
  }
"""


def _render_page(
    page_title: str,
    heading: str,
    lead: str,
    current_page: str,
    body_html: str,
    generated_at: datetime,
) -> str:
    """Wrap a page body in the shared Sazon-styled header, hero and footer."""
    current_marker = ' aria-current="page"'
    navigation_links = "".join(
        f'<li><a href="{file_name}"{current_marker if file_name == current_page else ""}>'
        f"{html.escape(label)}</a></li>"
        for file_name, label in SITE_PAGES
    )
    source_lines = "".join(
        f'<span class="tooltip-line">{html.escape(line)}</span>'
        for line in SOURCE_NOTES
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(page_title)} — Sazon Energy</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js" charset="utf-8"></script>
<style>{PAGE_STYLESHEET}</style>
</head>
<body>
<header class="site-header">
  <nav class="site-nav">
    <a href="{MAIN_PAGE_FILE_NAME}" class="logo">Sazon Energy</a>
    <ul class="nav-links">{navigation_links}</ul>
  </nav>
</header>
<main>
  <section class="hero">
    <h1>{html.escape(heading)}</h1>
    <p>{html.escape(lead)}</p>
  </section>
  <div class="section">{body_html}</div>
</main>
<footer class="site-footer">
  <span class="footer-generated tooltip-anchor" tabindex="0" aria-describedby="source-notes">
    Generated {generated_at:%Y-%m-%d %H:%M} UTC
    <i class="info-icon" aria-hidden="true">i</i>
    <span class="hover-tooltip tooltip-above tooltip-light" id="source-notes" role="tooltip">{source_lines}</span>
  </span>
</footer>
</body>
</html>"""


MAIN_PAGE_FILE_NAME = "index.html"
INDICES_PAGE_FILE_NAME = "indices.html"
SITE_PAGES = [
    (MAIN_PAGE_FILE_NAME, "Grid reliability"),
    (INDICES_PAGE_FILE_NAME, "Reliability indices"),
]

# Shown in the footer's "Generated" tooltip on every page.
SOURCE_NOTES = [
    "Source: MiLUMA outage feed published by LUMA Energy, sampled every 10 minutes.",
    "IEEE 1366 reliability indices derived from that feed. All causes are "
    "counted: planned, unplanned and load shed.",
    "Indices computed per IEEE 1366; Major Event Days classified with the "
    "2.5 Beta Method. Figures over short histories are indicative rather than "
    "representative of long-run reliability.",
]


def build_main_page_html(
    hourly_points_by_cause: dict[str, list[MetricPoint]],
    daily_points_by_cause: dict[str, list[MetricPoint]],
    snapshot_timestamps: list[datetime],
    generated_at: datetime,
) -> str:
    """The interactive customers-out page, linking to the indices page."""
    body_html = f"""
    <div class="panel">{build_customers_out_section(hourly_points_by_cause, daily_points_by_cause, snapshot_timestamps, generated_at)}</div>
    <div class="cta-row">
      <a class="cta-button" href="{INDICES_PAGE_FILE_NAME}">View reliability indices →</a>
    </div>"""
    return _render_page(
        page_title="Puerto Rico Grid Reliability",
        heading="Puerto Rico Grid Reliability",
        lead="Count of LUMA PR customers without service",
        current_page=MAIN_PAGE_FILE_NAME,
        body_html=body_html,
        generated_at=generated_at,
    )


def build_indices_page_html(
    hourly_points: list[MetricPoint],
    daily_points: list[MetricPoint],
    region_daily_points: list[MetricPoint],
    major_event_days: list[MajorEventDayPoint],
    generated_at: datetime,
) -> str:
    """Static page: headline indices, supporting charts and how to read them,
    always over all data available up to one year - no selectors."""
    summary = summarize_window(daily_points, window_days=MAXIMUM_HISTORY_DAYS)
    if summary["days"]:
        coverage_sentence = (
            f"All data available, up to one year: "
            f"{_format_local_date(summary['first_period_start'])} – "
            f"{_format_local_date(summary['last_period_start'])} "
            f"({summary['days']} day{'s' if summary['days'] != 1 else ''})"
        )
    else:
        coverage_sentence = "Awaiting the first full day of data."

    body_html = f"""
    <a class="back-link" href="{MAIN_PAGE_FILE_NAME}">← Back to grid reliability</a>
    <h2 class="section-title">Island-wide reliability</h2>
    <p class="section-subtitle">{html.escape(coverage_sentence)}</p>
    {build_kpi_cards(summary)}
    <div class="panel">{build_outage_level_figure(hourly_points)}</div>
    <div class="panel">{build_customer_minutes_escalation_figure(hourly_points)}</div>
    <div class="panel">{build_daily_saidi_figure(daily_points, major_event_days)}</div>
    <div class="panel">{build_index_trend_figure(daily_points)}</div>
    <div class="panel">{build_region_comparison_figure(region_daily_points)}</div>

    <div class="callout">
      <h3>How to read these numbers</h3>
      <p>
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
      <p class="callout-note">{_major_event_day_note(major_event_days)}</p>
    </div>"""
    return _render_page(
        page_title="Reliability Indices",
        heading="Reliability Indices",
        lead="IEEE 1366 measures of how often and how long Puerto Rico's grid leaves customers without power.",
        current_page=INDICES_PAGE_FILE_NAME,
        body_html=body_html,
        generated_at=generated_at,
    )


def build_site_pages(
    hourly_points: list[MetricPoint],
    daily_points: list[MetricPoint],
    region_daily_points: list[MetricPoint],
    major_event_days: list[MajorEventDayPoint],
    hourly_points_by_cause: dict[str, list[MetricPoint]],
    daily_points_by_cause: dict[str, list[MetricPoint]],
    snapshot_timestamps: list[datetime],
) -> dict[str, str]:
    """Every page of the site, keyed by file name."""
    generated_at = datetime.now(timezone.utc)
    return {
        MAIN_PAGE_FILE_NAME: build_main_page_html(
            hourly_points_by_cause, daily_points_by_cause, snapshot_timestamps, generated_at
        ),
        INDICES_PAGE_FILE_NAME: build_indices_page_html(
            hourly_points, daily_points, region_daily_points, major_event_days, generated_at
        ),
    }
