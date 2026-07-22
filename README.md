# LUMA Energy PR Outage History

Turns LUMA Energy's live outage status page
([miluma.lumapr.com/outages/status](https://miluma.lumapr.com/outages/status))
into a queryable history. The page only ever shows the *current* outage state,
so a GitHub Actions job polls the underlying API on an hourly schedule and
appends each snapshot to a Supabase (Postgres) database.

Each snapshot records, per region (Arecibo, Bayamon, Caguas, Carolina,
Mayaguez, Ponce, San Juan) and island-wide: total customers, customers
without service, customers on planned outage, customers on load shed, and the
corresponding percentages.

Municipality/town-level detail is not collected yet - see **Roadmap** below.

## How it works

```
GitHub Actions (hourly cron)
  -> python -m src
       1. fetch region JSON from the LUMA outage API
            (falls back to a headless browser if the WAF blocks the request)
       2. parse into one snapshot record + one record per region
       3. write to Supabase (skipped if that hour was already recorded)
Supabase (Postgres)
  outage_snapshots (1) --- (many) region_readings
```

The LUMA API sits behind an Incapsula WAF. A request with realistic browser
headers usually passes; on the rare occasion it doesn't, the workflow falls
back to loading the real status page in `browserless/chrome` and reading the
same network response the page itself receives.

## One-time setup

1. **Create a Supabase project** at [supabase.com](https://supabase.com).
2. **Create the tables**: open the Supabase SQL editor and run
   [`db/schema.sql`](db/schema.sql).
3. **Get your credentials**: Project Settings -> API -> copy the Project URL
   and the `service_role` secret key (not the `anon` key - writes require the
   service role).
4. **Add GitHub repository secrets** (Settings -> Secrets and variables ->
   Actions) named `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` with those
   values.
5. Push this repository to GitHub so the workflow in
   [`.github/workflows/collect.yml`](.github/workflows/collect.yml) can run.
   It polls hourly and can also be run on demand from the Actions tab
   ("Run workflow").

## Running locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY
set -a && source .env && set +a
python -m src
```

A successful run prints either a new snapshot id or a message that the
current hour was already recorded.

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
```

## Roadmap

- **Municipality/town-level detail.** The LUMA API also exposes a
  `POST /outage/municipality/towns` endpoint with per-town outage areas and
  zones. This is deferred for now (region-level history ships first); adding
  it later means a `municipality_readings` table plus a fetch/parse step for
  that endpoint.
- A read-only dashboard or view layer over the accumulated history.
