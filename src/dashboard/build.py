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
import math
import os
from datetime import date, datetime, timedelta, timezone

import plotly.graph_objects as graph_objects
import plotly.io as plotly_io
from plotly.subplots import make_subplots

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

# Segments left off the chart when every value is zero. LUMA's feed carries a
# load-shed count, but it has only ever reported 0; the segment reappears on
# its own once the feed starts reporting it.
CAUSE_BASES_HIDDEN_WHEN_EMPTY = {"load_shed"}

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
# The page always opens on "60 d" showing the most recent 60 days; view and
# start-date choices are never remembered between visits.
DEFAULT_VIEW_INDEX = 2
DEFAULT_GRANULARITY = VIEW_OPTIONS[DEFAULT_VIEW_INDEX]["granularity"]

CUSTOMERS_OUT_FIGURE_DIV_ID = "customers-out-figure"


# One-to-three-word meanings shown in the key under each chart, so readers new
# to reliability indices can tell what each term measures. Charts pick the
# entries they use; the wording stays the same everywhere.
TERM_SUMMARIES = {
    "SAIDI": "Minutes without power",
    "SAIFI": "Outages per customer",
    "CAIDI": "Average restoration time",
    "Major Event Day": "Extreme outage day",
    "T_MED": "Major-event threshold",
    "Mean": "Hourly average",
    "Peak": "Hourly maximum",
    "Unplanned": "Faults and damage",
    "Planned": "Scheduled work",
    "Load shed": "Deliberate supply cuts",
}


def _chart_key(terms: list[str]) -> str:
    """A compact "term — meaning" row placed under a chart."""
    key_items = "".join(
        f'<span class="chart-key-item"><strong>{html.escape(term)}</strong> '
        f"{html.escape(TERM_SUMMARIES[term])}</span>"
        for term in terms
    )
    return f'<div class="chart-key" role="note" aria-label="Chart key">{key_items}</div>'


def _figure_to_html(
    figure: graph_objects.Figure,
    div_id: str | None = None,
    margin_top: int = 48,
    height: int = 380,
    allow_legend_toggle: bool = False,
) -> str:
    figure.update_layout(
        template=PLOTLY_TEMPLATE,
        margin=dict(l=56, r=24, t=margin_top, b=48),
        height=height,
        hovermode="x unified",
        font=dict(family=FONT_FAMILY, color="#1f2937"),
        title_font=dict(size=15, color="#1e293b"),
    )
    # Hover read-outs stay, but zoom is switched off so charts always show
    # the period chosen for them. Ranges set from code (Plotly.relayout) still
    # apply; fixedrange only blocks drag and scroll zoom.
    figure.update_layout(dragmode=False)
    figure.update_xaxes(fixedrange=True)
    figure.update_yaxes(fixedrange=True)
    if not allow_legend_toggle:
        figure.update_layout(
            legend=dict(itemclick=False, itemdoubleclick=False),
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
    points: list[MetricPoint], axis: list[datetime]
) -> list[float]:
    """Per-period peak customers-out values, aligned to `axis`.

    Both the hourly and daily traces use the period's peak rather than its
    mean: a mean averages a spike down toward zero, understating how bad the
    worst moment of the period actually was, and using peak for both keeps
    the 48h and multi-day views comparable.
    """
    value_by_period_start = {
        point.period_start: point.peak_customers_without_service
        for point in points
    }
    return [value_by_period_start.get(period_start, 0.0) for period_start in axis]


def _customers_out_segments_with_data(
    hourly_points_by_cause: dict[str, list[MetricPoint]],
    daily_points_by_cause: dict[str, list[MetricPoint]],
) -> list[tuple[str, str, str]]:
    """`CUSTOMERS_OUT_SEGMENTS` minus any hide-when-empty segment that has no
    nonzero value at either granularity."""
    shown_segments = []
    for segment in CUSTOMERS_OUT_SEGMENTS:
        cause_basis = segment[0]
        if cause_basis in CAUSE_BASES_HIDDEN_WHEN_EMPTY and not any(
            point.peak_customers_without_service > 0
            for points_by_cause in (hourly_points_by_cause, daily_points_by_cause)
            for point in points_by_cause.get(cause_basis, [])
        ):
            continue
        shown_segments.append(segment)
    return shown_segments


def _build_customers_out_figure(
    hourly_points_by_cause: dict[str, list[MetricPoint]],
    daily_axis: list[datetime],
    daily_points_by_cause: dict[str, list[MetricPoint]],
    shown_segments: list[tuple[str, str, str]],
) -> tuple[graph_objects.Figure, list[datetime]]:
    """Build the figure with hourly and daily trace groups baked in.

    Both granularities live in one figure as separate trace groups; the
    inline script in `build_customers_out_section` flips which group is
    visible and adjusts the axis range, rather than reloading the figure.
    """
    hourly_axis = _customers_out_axis(hourly_points_by_cause, HOURLY_HISTORY_LIMIT_HOURS)

    figure = graph_objects.Figure()
    for cause_basis, label, color in shown_segments:
        figure.add_trace(
            graph_objects.Bar(
                x=hourly_axis,
                y=_customers_out_series(hourly_points_by_cause[cause_basis], hourly_axis),
                name=label,
                marker_color=color,
                visible=DEFAULT_GRANULARITY == "hour",
                hovertemplate="%{y:,.0f} customers (peak)<extra>" + label + "</extra>",
            )
        )
    for cause_basis, label, color in shown_segments:
        figure.add_trace(
            graph_objects.Bar(
                x=daily_axis,
                y=_customers_out_series(daily_points_by_cause[cause_basis], daily_axis),
                name=label,
                marker_color=color,
                visible=DEFAULT_GRANULARITY == "day",
                hovertemplate="%{y:,.0f} customers (peak)<extra>" + label + "</extra>",
            )
        )

    figure.update_layout(
        # The chart title lives in the HTML above the figure (see
        # `build_customers_out_section`) so it can sit centered as a label.
        yaxis_title=(
            "Customers out (hourly peak)"
            if DEFAULT_GRANULARITY == "hour"
            else "Customers out (daily peak)"
        ),
        xaxis=dict(
            title=(
                "Hour (Puerto Rico local time)"
                if DEFAULT_GRANULARITY == "hour"
                else "Day (Puerto Rico local time)"
            )
        ),
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
            <span class="hover-tooltip tooltip-align-right" id="window-coverage-tooltip" role="tooltip"></span>
          </span>
        </div>
      </div>
      <div class="window-slider-row" id="window-slider-row">
        <label for="window-start-slider">Start date</label>
        <input type="range" id="window-start-slider" min="0" max="0" value="0"
               step="1" list="window-start-dates" autocomplete="off" {slider_disabled}>
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
    outage_type_count: int,
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
      var outageTypeCount = {outage_type_count};

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
      // is today (slider at the right end, matching the chart, which reads
      // oldest to newest left to right); larger values move the window into
      // the past (slider moves left). Because the window length is fixed, the
      // slider's own range depends on the active view (a short window has
      // more valid positions than a long one).
      function applyView(offset) {{
        var length = activeView.days;
        var maxOffset = Math.max(0, availableDays - length);
        offset = Math.min(Math.max(offset, 0), maxOffset);
        var sliderPosition = maxOffset - offset;
        if (String(slider.value) !== String(sliderPosition)) slider.value = sliderPosition;

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
            // the upper bound is midnight after endDate so that day's hours
            // are fully included.
            'xaxis.range': [startDate, addDays(endDate, 1)],
            'xaxis.title.text': 'Hour (Puerto Rico local time)',
            'yaxis.title.text': 'Customers out (hourly peak)',
            'yaxis.autorange': true
          }});
        }} else {{
          Plotly.relayout(figureDiv, {{
            'xaxis.autorange': false,
            'xaxis.range': [startDate, endDate],
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
        slider.value = maxOffset;
        slider.disabled = availableDays === 0;

        // Each position is labelled with the first day its window shows.
        startDateOptions.innerHTML = '';
        for (var sliderPosition = 0; sliderPosition <= maxOffset; sliderPosition++) {{
          var windowEndIndex = availableDays - 1 - (maxOffset - sliderPosition);
          var option = document.createElement('option');
          option.value = sliderPosition;
          option.label = formatDate(dailyData.dates[Math.max(0, windowEndIndex - length + 1)]);
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
        applyView(parseInt(slider.max, 10) - parseInt(slider.value, 10));
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

      // Always the default view over the most recent days - nothing chosen
      // on an earlier visit carries over.
      function resetToDefaultView() {{
        activeView = viewOptions[{DEFAULT_VIEW_INDEX}];
        setActiveButton(buttons[{DEFAULT_VIEW_INDEX}]);
        configureSliderForView();
        applyView(0);
      }}

      // Back/forward navigation can restore this page from the browser's
      // cache with the previous selection intact; reset it there too.
      window.addEventListener('pageshow', function(event) {{
        if (event.persisted) resetToDefaultView();
      }});

      resetToDefaultView();
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

    shown_segments = _customers_out_segments_with_data(
        hourly_points_by_cause, daily_points_by_cause
    )
    figure, _hourly_axis = _build_customers_out_figure(
        hourly_points_by_cause, daily_axis, daily_points_by_cause, shown_segments
    )
    figure_html = _figure_to_html(
        figure,
        div_id=CUSTOMERS_OUT_FIGURE_DIV_ID,
        margin_top=40,
        height=400,
        allow_legend_toggle=True,
    )

    controls_html = build_window_controls_markup(len(daily_axis))
    script_html = build_window_controls_script(
        daily_axis, snapshot_timestamps, generated_at, len(shown_segments)
    )

    key_html = _chart_key([label for _, label, _ in shown_segments])

    return f"{controls_html}{figure_html}{key_html}{script_html}"


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
    return _figure_to_html(figure) + _chart_key(["Mean", "Peak"])


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
    key_terms = ["SAIDI", "Major Event Day"] + (["T_MED"] if threshold is not None else [])
    return _figure_to_html(figure) + _chart_key(key_terms)


def build_index_trend_figure(daily_points: list[MetricPoint]) -> str:
    """SAIFI and CAIDI in two stacked panels sharing the date axis.

    Their scales differ by orders of magnitude (a fraction of an outage versus
    minutes), so overlaying them on twin axes made both lines look erratic.
    Each panel starts at zero so a quiet day reads as small, not as a swing.
    """
    if not daily_points:
        return _empty_state("No daily data yet.")

    daily_points = _limit_to_recent_days(daily_points, MAXIMUM_HISTORY_DAYS)
    period_starts = [point.period_start for point in daily_points]
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.14,
        subplot_titles=("SAIFI (lower bound)", "CAIDI (upper bound)"),
    )
    figure.add_trace(
        graph_objects.Scatter(
            x=period_starts,
            y=[point.saifi_estimated for point in daily_points],
            name="SAIFI (lower bound)",
            mode="lines+markers",
            line=dict(color=COLOR_PRIMARY, width=2),
            hovertemplate="%{y:.4f} interruptions/customer<extra>SAIFI</extra>",
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        graph_objects.Scatter(
            x=period_starts,
            y=[point.caidi_minutes for point in daily_points],
            name="CAIDI (upper bound)",
            mode="lines+markers",
            line=dict(color=COLOR_ACCENT, width=2),
            hovertemplate="%{y:.1f} min<extra>CAIDI</extra>",
        ),
        row=2,
        col=1,
    )
    figure.update_yaxes(title_text="Per customer", rangemode="tozero", row=1, col=1)
    figure.update_yaxes(title_text="Minutes", rangemode="tozero", row=2, col=1)
    figure.update_annotations(font=dict(size=13, color="#1e293b"))
    figure.update_layout(title="SAIFI and CAIDI (daily)", showlegend=False)
    return _figure_to_html(figure, margin_top=72, height=520) + _chart_key(
        ["SAIFI", "CAIDI"]
    )


def build_cumulative_saidi_figure(daily_points: list[MetricPoint]) -> str:
    """Running total of daily SAIDI - minutes without power per customer so far.

    Utilities track year-to-date SAIDI this way against an annual target. The
    days counted match `summarize_window`, so the line ends on the SAIDI card.
    """
    if not daily_points:
        return _empty_state("No daily data yet - SAIDI appears after a full day.")

    cutoff = daily_points[-1].period_start - timedelta(days=MAXIMUM_HISTORY_DAYS)
    counted_points = [point for point in daily_points if point.period_start > cutoff]

    running_saidi = 0.0
    running_customer_minutes = 0.0
    cumulative_saidi_values = []
    hover_details = []
    for point in counted_points:
        running_saidi += point.saidi_minutes
        running_customer_minutes += point.customer_minutes_interrupted
        cumulative_saidi_values.append(running_saidi)
        hover_details.append([point.saidi_minutes, running_customer_minutes])

    figure = graph_objects.Figure()
    figure.add_trace(
        graph_objects.Scatter(
            x=[point.period_start for point in counted_points],
            y=cumulative_saidi_values,
            customdata=hover_details,
            name="Cumulative SAIDI",
            mode="lines",
            line=dict(color=COLOR_PRIMARY, width=2.5),
            fill="tozeroy",
            fillcolor=COLOR_PRIMARY_FILL,
            hovertemplate=(
                "%{y:,.1f} min/customer so far"
                "<br>%{customdata[0]:,.2f} min/customer this day"
                "<br>%{customdata[1]:,.0f} customer-minutes so far"
                "<extra></extra>"
            ),
        )
    )
    figure.update_layout(
        title="Cumulative SAIDI (running total)",
        yaxis_title="Minutes per customer",
        showlegend=False,
    )
    return _figure_to_html(figure) + _chart_key(["SAIDI"])


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
    return _figure_to_html(figure) + _chart_key(["SAIDI"])


def _most_recent_consecutive_days(daily_points: list[MetricPoint]) -> list[datetime]:
    """Period starts of the unbroken run of days (Puerto Rico local dates)
    ending at the latest day with data."""
    local_period_starts = [
        (point.period_start.astimezone(PUERTO_RICO_TIMEZONE).date(), point.period_start)
        for point in daily_points
    ]
    consecutive_run: list[datetime] = []
    for local_date, period_start in reversed(local_period_starts):
        if consecutive_run:
            earliest_local_date = consecutive_run[0].astimezone(PUERTO_RICO_TIMEZONE).date()
            if earliest_local_date - local_date != timedelta(days=1):
                break
        consecutive_run.insert(0, period_start)
    return consecutive_run


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


def _metric_descriptor(text: str) -> str:
    """Smaller line beneath a KPI value, saying what the number counts."""
    return f'<span class="metric-secondary">{html.escape(text)}</span>'


def _round_down(value: float | None, decimals: int) -> float | None:
    """Truncate toward zero, so an availability figure is never shown rounded
    up (99.996% must not display as 100.00%)."""
    if value is None:
        return None
    scale = 10 ** decimals
    return math.floor(value * scale) / scale


def _round_up(value: float | None, decimals: int) -> float | None:
    """Round away from zero at `decimals`, so outage figures are never shown
    understated. The inner round() absorbs float noise (2.5 * 10 can come out
    as 25.000000000000004, which would otherwise ceil to 2.6)."""
    if value is None:
        return None
    scale = 10 ** decimals
    return math.ceil(round(value * scale, 9)) / scale


def _format_minutes_with_hours(value: float | None) -> str:
    """Whole minutes, rounded up so outage time is never understated, with
    the same value in hours beneath it."""
    if value is None:
        return _format_metric(None, " min")
    return (
        _format_metric(math.ceil(value), " min", 0)
        + _metric_descriptor(f"{value / 60:,.1f} hours")
    )


def _format_local_date(moment: datetime) -> str:
    local_date = moment.astimezone(PUERTO_RICO_TIMEZONE).date()
    return f"{local_date:%b} {local_date.day}, {local_date.year}"


def build_kpi_cards(summary: dict) -> str:
    cards = [
        (
            "saidi",
            "SAIDI",
            "System Average Interruption Duration Index",
            _format_minutes_with_hours(summary["saidi"]),
            "Minutes of interruption per customer served",
            False,
        ),
        (
            "saifi",
            "SAIFI",
            "System Average Interruption Frequency Index",
            _format_metric(_round_up(summary["saifi"], 1), "", 1) + _metric_descriptor("interruptions"),
            "Number of interruptions per customer — lower bound",
            True,
        ),
        (
            "caidi",
            "CAIDI",
            "Customer Average Interruption Duration Index",
            _format_minutes_with_hours(summary["caidi"]),
            "Average restoration time — upper bound",
            True,
        ),
        (
            "asai",
            "ASAI",
            "Average Service Availability Index",
            _format_metric(_round_down(summary["asai"], 2), " %", 2)
            + _metric_descriptor("effective uptime"),
            "Percentage of time that power is available.",
            False,
        ),
    ]

    # Snapshot sampling can only bound these, however much history accumulates;
    # see the "How to read these numbers" panel.
    estimated_marker = ' <em class="kpi-estimated">Estimated</em>'
    card_markup = "".join(
        f"""
        <div class="kpi-card">
          <div class="kpi-header">
            <span class="kpi-name tooltip-anchor" tabindex="0" aria-describedby="kpi-{slug}-full-name">
              {name}
              <span class="hover-tooltip" id="kpi-{slug}-full-name" role="tooltip">{html.escape(full_name)}</span>
            </span>
          </div>
          <div class="kpi-value">{value}</div>
          <div class="kpi-note">{html.escape(note)}{estimated_marker if is_estimated else ""}</div>
        </div>"""
        for slug, name, full_name, value, note, is_estimated in cards
    )
    return f'<div class="kpi-grid">{card_markup}</div>'


def _status_badge(quality: str, label: str) -> str:
    """Small pill matching the KPI cards' status badges, as trusted HTML."""
    return (
        f'<span class="status-badge status-{quality}">'
        f'<span class="status-dot"></span>{html.escape(label)}</span>'
    )


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
  .panel {
    background: var(--bg); border: 1px solid var(--border); border-radius: 15px;
    box-shadow: var(--shadow); padding: 1.25rem; margin-top: 2rem;
  }
  .panel:first-child { margin-top: 0; }
  .chart-key {
    display: flex; flex-wrap: wrap; justify-content: center; gap: 0.35rem 1.25rem;
    margin-top: 0.5rem; padding-top: 0.75rem; border-top: 1px solid var(--border);
    font-size: 0.8rem; color: var(--muted);
  }
  .chart-key-item strong { color: var(--text); font-weight: 600; margin-right: 0.3rem; }
  .empty-state { padding: 48px 16px; text-align: center; color: var(--muted); font-size: 0.9rem; }

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
  .window-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; flex: 1; }
  /* Fixed width so nothing after it shifts as the date range text changes. */
  .window-range-label {
    font-size: 0.85rem; color: var(--muted); font-weight: 600;
    font-variant-numeric: tabular-nums; min-width: 17rem;
  }
  .coverage-stat {
    display: inline-flex; align-items: baseline; gap: 6px; margin-left: auto;
    cursor: default;
  }
  .coverage-name {
    font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.08em;
    font-weight: 600; color: var(--muted);
  }
  .coverage-value {
    font-size: 0.75rem; font-weight: 600; color: var(--muted);
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
  .data-range-note {
    text-align: center; font-size: 0.85rem; color: var(--muted); margin-bottom: 1.25rem;
  }
  .kpi-grid { display: grid; gap: 1.5rem; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); }
  .kpi-card {
    display: flex; flex-direction: column; align-items: center; text-align: center;
    background: var(--bg); border: 1px solid var(--border); border-top: 4px solid var(--primary);
    border-radius: 15px; box-shadow: var(--shadow); padding: 1.5rem 1.25rem 1.25rem;
  }
  .kpi-header { display: flex; justify-content: center; }
  .kpi-name {
    font-weight: 700; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.12em;
    color: var(--muted);
  }
  /* Centered under the name rather than hanging off its left edge. */
  .kpi-name .hover-tooltip { left: 50%; transform: translateX(-50%); }
  .kpi-value {
    font-size: 2.75rem; font-weight: 700; color: var(--secondary); line-height: 1.1;
    margin: 0.6rem 0 0.75rem; letter-spacing: -0.03em; font-variant-numeric: tabular-nums;
  }
  .metric-suffix { font-size: 1rem; font-weight: 500; color: var(--muted); margin-left: 4px; letter-spacing: 0; }
  .metric-empty { color: var(--muted); }
  .metric-secondary {
    display: block; margin-top: 0.3rem; font-size: 0.95rem; font-weight: 500;
    color: var(--muted); letter-spacing: 0;
  }
  /* Pinned to the bottom so notes line up across cards of different heights. */
  .kpi-note { margin-top: auto; font-size: 0.85rem; color: var(--muted); line-height: 1.45; }
  .kpi-estimated { display: block; color: #b45309; font-weight: 600; }
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

  /* Draft notice */
  .draft-banner {
    display: flex; align-items: flex-start; gap: 0.75rem;
    background: rgba(245, 158, 11, 0.1); color: #92400e;
    border: 1px solid rgba(245, 158, 11, 0.35); border-left: 4px solid var(--coming-soon);
    border-radius: 15px; padding: 1rem 1.25rem; margin-bottom: 2rem;
    font-size: 0.95rem; line-height: 1.6;
  }
  .draft-banner-label {
    flex-shrink: 0; background: var(--coming-soon); color: #fff;
    font-size: 0.75rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
    border-radius: 999px; padding: 0.15rem 0.6rem; margin-top: 0.15rem;
  }
  .draft-banner strong { color: #78350f; }

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
  .callout h4 {
    display: flex; flex-wrap: wrap; align-items: center; gap: 0.6rem;
    color: var(--secondary); font-size: 1rem; margin: 1.5rem 0 0.35rem;
  }
  .callout .expert-note {
    margin-top: 0.4rem; padding-left: 0.85rem; border-left: 2px solid var(--border);
    font-size: 0.85rem; line-height: 1.6;
  }
  .callout-table {
    width: 100%; border-collapse: collapse; margin-top: 0.75rem; font-size: 0.88rem;
  }
  .callout-table th, .callout-table td {
    text-align: left; padding: 0.45rem 0.75rem; border-bottom: 1px solid var(--border);
  }
  .callout-table th {
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--secondary);
  }
  .callout-table td:first-child { color: var(--text); font-weight: 600; }
  .status-unavailable {
    background: rgba(107, 114, 128, 0.1); color: var(--muted);
    border: 1px solid rgba(107, 114, 128, 0.2);
  }
  .status-unavailable .status-dot { background: var(--muted); }

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
  .tooltip-align-right { left: auto; right: 0; }
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
    .panel { padding: 1rem 0.75rem; }
    .chart-title-row { grid-template-columns: 1fr; justify-items: center; }
    .chart-title, .cause-reset-btn { grid-column: 1; justify-self: center; }
    .callout { padding: 1.25rem 1rem; }
    /* Stack each table row into a labelled block on narrow screens. */
    .callout-table thead { display: none; }
    .callout-table, .callout-table tbody, .callout-table tr, .callout-table td { display: block; }
    .callout-table tr { padding: 0.5rem 0; border-bottom: 1px solid var(--border); }
    .callout-table td { border-bottom: none; padding: 0.15rem 0; }
    .callout-table td::before {
      content: attr(data-label) ": "; font-weight: 600; color: var(--secondary);
    }
    .callout-table td:first-child::before { content: none; }
  }
"""


def _render_page(
    page_title: str,
    heading: str,
    lead: str | None,
    current_page: str,
    body_html: str,
    generated_at: datetime,
) -> str:
    """Wrap a page body in the shared Sazon-styled header, hero and footer.
    The hero subtitle is omitted when `lead` is None."""
    current_marker = ' aria-current="page"'
    navigation_links = "".join(
        f'<li><a href="{file_name}"{current_marker if file_name == current_page else ""}>'
        f"{html.escape(label)}</a></li>"
        for file_name, label in SITE_PAGES
    )
    lead_markup = f"<p>{html.escape(lead)}</p>" if lead else ""
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
{_render_analytics_snippets()}
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
    {lead_markup}
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
{_render_linkedin_insight_tag()}
</body>
</html>"""


def _render_analytics_snippets() -> str:
    """Visitor-analytics tags for the shared <head>. Each tag is emitted only
    when its ID is configured, so local builds stay free of tracking."""
    snippets = []
    if CLOUDFLARE_ANALYTICS_TOKEN:
        beacon_settings = html.escape(json.dumps({"token": CLOUDFLARE_ANALYTICS_TOKEN}))
        snippets.append(
            '<script defer src="https://static.cloudflareinsights.com/beacon.min.js" '
            f'data-cf-beacon="{beacon_settings}"></script>'
        )
    return "\n".join(snippets)



def _render_linkedin_insight_tag() -> str:
    """LinkedIn's standard Insight Tag, placed just before </body> as LinkedIn
    recommends (its <noscript> image is not valid inside <head>). Emitted only
    when a numeric partner ID is configured."""
    if not LINKEDIN_PARTNER_ID.isdigit():
        return ""
    return (
        "<script>\n"
        f'_linkedin_partner_id = "{LINKEDIN_PARTNER_ID}";\n'
        "window._linkedin_data_partner_ids = window._linkedin_data_partner_ids || [];\n"
        "window._linkedin_data_partner_ids.push(_linkedin_partner_id);\n"
        "</script>\n"
        "<script>\n"
        "(function(lintrk) {\n"
        "  if (!lintrk) { window.lintrk = function(action, data) { window.lintrk.q.push([action, data]); };\n"
        "    window.lintrk.q = []; }\n"
        '  var firstScript = document.getElementsByTagName("script")[0];\n'
        '  var insightScript = document.createElement("script");\n'
        "  insightScript.async = true;\n"
        '  insightScript.src = "https://snap.licdn.com/li.lms-analytics/insight.min.js";\n'
        "  firstScript.parentNode.insertBefore(insightScript, firstScript);\n"
        "})(window.lintrk);\n"
        "</script>\n"
        "<noscript><img height=\"1\" width=\"1\" style=\"display:none;\" alt=\"\" "
        f'src="https://px.ads.linkedin.com/collect/?pid={LINKEDIN_PARTNER_ID}&amp;fmt=gif"></noscript>'
    )

# Cloudflare Web Analytics site token (cookie-free page-view counts). Public
# once rendered into the page, so it is a repository variable, not a secret.
CLOUDFLARE_ANALYTICS_TOKEN = os.environ.get("CLOUDFLARE_ANALYTICS_TOKEN", "").strip()

# LinkedIn Insight Tag partner ID (aggregate company / job-title demographics of
# LinkedIn-member visitors, shown in Campaign Manager). Public once rendered, so
# it is a repository variable, not a secret.
LINKEDIN_PARTNER_ID = os.environ.get("LINKEDIN_PARTNER_ID", "").strip()

MAIN_PAGE_FILE_NAME = "index.html"
INDICES_PAGE_FILE_NAME = "indices.html"
SITE_PAGES = [
    (MAIN_PAGE_FILE_NAME, "Customer Outage"),
    (INDICES_PAGE_FILE_NAME, "Reliability Indices"),
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
    """The interactive customers-out page."""
    body_html = f"""
    <div class="panel">{build_customers_out_section(hourly_points_by_cause, daily_points_by_cause, snapshot_timestamps, generated_at)}</div>"""
    return _render_page(
        page_title="Customer Outage Statistics",
        heading="Customer Outage Statistics",
        lead="Puerto Rico/LUMA Energy historic customer outage statistics",
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
    consecutive_run = _most_recent_consecutive_days(daily_points)
    if consecutive_run:
        run_day_count = len(consecutive_run)
        coverage_sentence = (
            f"Indices require 1 year of data for accurate calculation. "
            f"Currently {run_day_count} consecutive day{'s' if run_day_count != 1 else ''} "
            f"available. {_format_local_date(consecutive_run[0])} – "
            f"{_format_local_date(consecutive_run[-1])}"
        )
    else:
        coverage_sentence = "Awaiting the first full day of data."

    body_html = f"""
    <div class="draft-banner" role="note">
      <span class="draft-banner-label">Draft</span>
      <span>
        <strong>Everything on this page is draft work still in development.</strong>
        The indices, charts and explanations may change as the methods are
        reviewed and more data is collected, so please don't rely on these
        figures yet.
      </span>
    </div>
    <p class="data-range-note">{html.escape(coverage_sentence)}</p>
    {build_kpi_cards(summary)}
    <div class="panel">{build_outage_level_figure(hourly_points)}</div>
    <div class="panel">{build_daily_saidi_figure(daily_points, major_event_days)}</div>
    <div class="panel">{build_cumulative_saidi_figure(daily_points)}</div>
    <div class="panel">{build_index_trend_figure(daily_points)}</div>
    <div class="panel">{build_region_comparison_figure(region_daily_points)}</div>

    <div class="callout">
      <h3>How to read these numbers</h3>

      <h4>Where the data comes from</h4>
      <p>
        About every 10 minutes, LUMA publishes one number: how many customers are
        without power right now. We record that number each time, like taking a
        snapshot every 10 minutes. Snapshots show how many people are in the dark
        at each moment. They can't show individual outages: which homes went out,
        exactly when, or when each one came back.
      </p>

      <h4>What an interruption event log is</h4>
      <p>
        Internally, a utility keeps a record of every outage: when it started, which
        customers it affected, and when each was restored. Official reliability
        reports are calculated from that log. LUMA doesn't publish it in real time,
        so every number on this page is calculated from the snapshots instead. Some
        numbers come out very close to what the event log would give; others can
        only be a best estimate:
      </p>
      <table class="callout-table">
        <thead>
          <tr><th>Measure</th><th>From snapshots</th><th>Exact value needs</th></tr>
        </thead>
        <tbody>
          <tr><td data-label="Measure">SAIDI, ASAI, customer-minutes</td><td data-label="From snapshots">Reliable</td><td data-label="Exact value needs">Snapshots are enough</td></tr>
          <tr><td data-label="Measure">SAIFI</td><td data-label="From snapshots">A minimum (the real number is likely higher)</td><td data-label="Exact value needs">Event log</td></tr>
          <tr><td data-label="Measure">CAIDI</td><td data-label="From snapshots">A maximum (the real number is likely lower)</td><td data-label="Exact value needs">Event log</td></tr>
          <tr><td data-label="Measure">MAIFI</td><td data-label="From snapshots">Can't be measured</td><td data-label="Exact value needs">Event log or meter data</td></tr>
        </tbody>
      </table>

      <h4>SAIDI: average time without power {_status_badge("solid", "Reliable")}</h4>
      <p>
        Take all the time LUMA customers spent without power and share it out evenly
        across every customer. SAIDI is how many minutes each customer would get. For
        example, a SAIDI of 600 minutes means the average customer went about 10 hours
        without power over the period shown. Snapshots measure this well, because
        adding up time without power doesn't require knowing which outage was which.
      </p>
      <p class="expert-note">
        <strong>For experts:</strong> Σ customer-minutes ÷ average customers served.
        Customer-minutes are found by trapezoidal integration between samples up to
        35 minutes apart. Longer gaps are skipped, not interpolated, so missed
        collections slightly understate SAIDI. The headline figure is the sum of daily
        SAIDI over the period shown, not an annual number, and it includes planned
        outages and Major Event Days. Official IEEE 1366 reports usually exclude both,
        so don't compare it directly with LUMA's or the regulator's filings.
      </p>

      <h4>SAIFI: how often the average customer lost power {_status_badge("estimated", "A minimum")}</h4>
      <p>
        A SAIFI of 1.5 means the typical customer lost power about one and a half
        times. Ours is a floor. If an outage starts and ends between two snapshots, we
        never see it. And if one neighborhood is restored at the same moment another
        goes dark, the total doesn't change, so the new outage goes uncounted.
      </p>
      <p class="expert-note">
        <strong>For experts:</strong> Σ of positive increases in customers-out between
        consecutive samples ÷ customers served. It only sees net change, so overlapping
        restorations hide new interruptions, and outages shorter than the sampling
        interval can't be detected. That makes this a strict lower bound. An exact
        count needs the per-customer interruption log.
      </p>

      <h4>CAIDI: how long an outage typically lasts {_status_badge("estimated", "A maximum")}</h4>
      <p>
        When a customer does lose power, this is roughly how long it takes on average
        to get it back. Ours probably runs high: the short outages we can't see would
        pull the average down if we could count them.
      </p>
      <p class="expert-note">
        <strong>For experts:</strong> CAIDI = SAIDI ÷ SAIFI, the customer-weighted mean
        restoration time. The SAIDI on top is reliable, but SAIFI underneath is biased
        low, so CAIDI is biased high (an upper bound).
      </p>

      <h4>ASAI: share of time the power was on {_status_badge("solid", "Reliable")}</h4>
      <p>
        The percentage of time the average customer had power. Small differences
        matter: 99.9% sounds nearly perfect, but over a year it adds up to almost
        9 hours without power.
      </p>
      <p class="expert-note">
        <strong>For experts:</strong> ASAI = 1 − SAIDI ÷ minutes in the period. It
        inherits SAIDI's reliability and its small understatement from collection gaps.
      </p>

      <h4>MAIFI: brief flickers {_status_badge("unavailable", "Not shown")}</h4>
      <p>
        Counts momentary interruptions: the lights blink off, then come straight back.
        These last seconds, so 10-minute snapshots can't see them.
      </p>
      <p class="expert-note">
        <strong>For experts:</strong> IEEE 1366 treats interruptions under 5 minutes as
        momentary. Measuring them takes recloser or meter event data.
      </p>

      <h4>Major Event Days: separating disasters from everyday service</h4>
      <p>
        A hurricane or island-wide blackout can cause more outage time in one day than
        months of normal operation. The standard flags those days (red bars in the
        daily SAIDI chart) so extraordinary events can be looked at separately from
        day-to-day reliability. The cutoff is set from the history so far, so it will
        shift as more data comes in. The standard uses five years of history.
      </p>
      <p class="expert-note">
        <strong>For experts:</strong> IEEE 1366 2.5 Beta Method:
        T<sub>MED</sub> = exp(α + 2.5β), where α and β are the mean and sample standard
        deviation of ln(daily SAIDI), leaving out days with zero SAIDI. We start
        computing it once there are 30 days of history, and it's labelled provisional
        until there are five years. The headline cards above are <strong>not</strong>
        MED-normalized.
      </p>
      <p class="callout-note">{_major_event_day_note(major_event_days)}</p>
    </div>"""
    return _render_page(
        page_title="Reliability Indices",
        heading="Reliability Indices",
        lead="Puerto Rico/LUMA Energy estimated reliability indices",
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
