# LUMA Energy PR Outage History

Turns LUMA Energy's live outage status page
([miluma.lumapr.com/outages/status](https://miluma.lumapr.com/outages/status))
into a queryable history, and derives standard electric-utility reliability
indices (SAIDI, SAIFI, CAIDI, ASAI) from it on a public dashboard. The source
page only ever shows the *current* outage state, so a GitHub Actions job polls
the underlying API every 10 minutes and appends each snapshot to a Supabase
(Postgres) database.

Each snapshot records, per region (Arecibo, Bayamon, Caguas, Carolina,
Mayaguez, Ponce, San Juan) and island-wide: total customers, customers
without service, customers on planned outage, customers on load shed, and the
corresponding percentages.

Municipality/town-level detail is not collected yet - see **Roadmap** below.

## How it works

```
GitHub Actions (every 10 minutes)
  -> python -m src              fetch + parse + write one snapshot
       (falls back to a headless browser if the WAF blocks the request)
  -> python -m src.metrics      recompute reliability indices from history
Supabase (Postgres)
  outage_snapshots (1) --- (many) region_readings
  reliability_metrics            hourly/daily/monthly SAIDI/SAIFI/CAIDI/ASAI
  major_event_days               IEEE 1366 Major Event Day classification

GitHub Actions (on every successful collection)
  -> python -m src.dashboard    render a static HTML page from the metric tables
  -> deploy to GitHub Pages
```

The LUMA API sits behind an Incapsula WAF. A request with realistic browser
headers usually passes; on the rare occasion it doesn't, the collection
workflow falls back to loading the real status page in `browserless/chrome`
and reading the same network response the page itself receives.

## Reliability metrics

The source feed reports a *level* - how many customers are out right now -
not an interruption event log. That distinction means the standard IEEE 1366
indices are not all equally solid:

| Index | Confidence | Why |
|---|---|---|
| **SAIDI**, **ASAI**, customer-minutes | Solid | Integrating a sampled level signal over time is well posed. |
| **SAIFI** | Estimated (lower bound) | Inferred from rising edges of the customers-out curve; an outage that starts and ends between two samples is invisible. |
| **CAIDI** | Estimated (upper bound) | = SAIDI / SAIFI, so it inherits SAIFI's downward bias. It doubles as the customer-weighted average restoration time. |
| MAIFI (momentary interruptions) | Not computed | Undetectable at a 10-minute sampling interval. |

All four are computed per region and island-wide, hourly/daily/monthly, and
split by cause (`all`, `unplanned`, `planned`, `load_shed` - `planned` and
`load_shed` are confirmed subsets of "without service" in the source data).
The dashboard defaults to `all`; the other splits are already stored in
`reliability_metrics` for later exposure (see Roadmap).

**Major Event Days** are classified with the IEEE 1366 2.5 Beta Method
([src/metrics/major_event_days.py](src/metrics/major_event_days.py)):
daily SAIDI is log-transformed, and any day exceeding `exp(mean + 2.5*stdev)`
of that log-series is flagged. The standard calls for five years of history;
until then the threshold is still computed and shown, but marked
**provisional** since it will move as more data accumulates.

Math is unit-tested against hand-computed values and an independently
computed 2.5-beta threshold - see the computation module
([src/metrics/reliability.py](src/metrics/reliability.py)) for the full
trapezoidal-integration / rising-edge methodology.

## One-time setup

1. **Create a Supabase project** at [supabase.com](https://supabase.com) (if
   you don't have one already) and run [`db/schema.sql`](db/schema.sql) in its
   SQL editor. It's fully idempotent - safe to re-run after pulling schema
   changes, including the `reliability_metrics` and `major_event_days` tables
   added for the dashboard.
2. **Get your credentials**: Project Settings -> API -> copy the Project URL
   and the `service_role` secret key (not the `anon` key - writes require the
   service role).
3. **Add GitHub repository secrets** (Settings -> Secrets and variables ->
   Actions) named `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` with those
   values.
4. **Make the repository public.** The 10-minute collection cadence uses
   roughly 4,300 Actions minutes/month, well over the 2,000 free minutes a
   private repo gets - a public repo has no minute cap. This also unlocks free
   GitHub Pages hosting for the dashboard, needed for the next step. (Settings
   -> General -> Danger Zone -> Change visibility. Requires repo **admin**
   rights - ask an org owner if you don't have them.)
5. **Enable GitHub Pages**: Settings -> Pages -> Source: **GitHub Actions**.
   (Also requires admin rights.) Once enabled,
   [`.github/workflows/publish_dashboard.yml`](.github/workflows/publish_dashboard.yml)
   deploys the dashboard automatically after every successful collection run;
   the Pages URL appears on that Settings page once the first deploy
   succeeds.
6. Push this repository to GitHub so
   [`.github/workflows/collect.yml`](.github/workflows/collect.yml) can run.
   It polls every 10 minutes and can also be run on demand from the Actions
   tab ("Run workflow").

## Running locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY
set -a && source .env && set +a

python -m src              # collect one snapshot
python -m src.metrics      # recompute reliability metrics (add --full to rebuild all history)
python -m src.dashboard    # render site/index.html
```

A successful collection run prints either a new snapshot id or a message that
the current period was already recorded.

## Example queries

```sql
-- Most recent snapshots
select * from outage_snapshots order by collected_at desc limit 5;

-- Per-region detail for the latest snapshot
select region_name, total_clients_without_service, percentage_clients_without_service
from region_readings
where snapshot_id = (select id from outage_snapshots order by source_timestamp desc limit 1);

-- Island-wide customers without service over time
select source_timestamp, total_clients_without_service
from outage_snapshots
order by source_timestamp;

-- Daily SAIDI/SAIFI/CAIDI, island-wide, all causes
select period_start, saidi_minutes, saifi_estimated, caidi_minutes, coverage_ratio
from reliability_metrics
where granularity = 'day' and scope_name = 'Island-wide' and cause_basis = 'all'
order by period_start desc;

-- Confirmed Major Event Days
select event_date, daily_saidi_minutes, threshold_t_med
from major_event_days
where is_major_event_day
order by event_date desc;
```

## Roadmap

- **Municipality/town-level detail.** The LUMA API also exposes a
  `POST /outage/municipality/towns` endpoint with per-town outage areas and
  zones (no customer counts, no timestamp of its own). Adding it unlocks
  incident-level **MTTR** and repeat-offender zone hotspots - both deferred
  until this capture exists.
- **Selectable cause basis in the dashboard.** `unplanned` / `planned` /
  `load_shed` splits are already computed and stored; only the `all` basis is
  currently rendered.
- Feeder/circuit-level analysis is not possible with the public API - it has
  no circuit or feeder identifiers at any granularity.
