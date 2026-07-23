-- LUMA Energy outage history schema.
--
-- Run this once in the Supabase SQL editor (or via `psql`) before the first
-- collection run. Safe to re-run: every statement is idempotent.
--
-- Two tables:
--   outage_snapshots  one row per polled timestamp from the LUMA API,
--                     holding the island-wide totals for that timestamp.
--   region_readings   one row per region within a snapshot (Arecibo,
--                     Bayamon, Caguas, Carolina, Mayaguez, Ponce, San Juan).
--
-- `source_timestamp` is unique on outage_snapshots so re-running the
-- collector within the same source hour never creates duplicate history.

create table if not exists outage_snapshots (
    id                                        bigint generated always as identity primary key,
    source_timestamp                          timestamptz not null unique,
    source_timestamp_text                     text        not null,
    collected_at                              timestamptz not null default now(),
    total_clients                             integer,
    total_clients_with_service                integer,
    total_clients_without_service             integer,
    total_clients_affected_by_planned_outage  integer,
    total_clients_affected_by_load_shed       integer,
    total_percentage_with_service             numeric,
    total_percentage_without_service          numeric
);

create table if not exists region_readings (
    id                                        bigint generated always as identity primary key,
    snapshot_id                               bigint not null references outage_snapshots(id) on delete cascade,
    region_name                               text   not null,
    total_clients                             integer,
    total_clients_with_service                integer,
    total_clients_without_service             integer,
    total_clients_affected_by_planned_outage  integer,
    total_clients_affected_by_load_shed       integer,
    percentage_clients_with_service           numeric,
    percentage_clients_without_service        numeric,
    unique (snapshot_id, region_name)
);

create index if not exists region_readings_region_name_idx on region_readings (region_name);
create index if not exists region_readings_snapshot_id_idx on region_readings (snapshot_id);

-- Tables created through the Supabase SQL editor are not always covered by
-- Supabase's automatic default-privilege grants, so service_role (the key
-- the collector authenticates with) needs to be granted access explicitly.
-- Without this, writes fail with "permission denied for table ...".
grant select, insert on outage_snapshots to service_role;
grant select, insert on region_readings to service_role;


-- ---------------------------------------------------------------------------
-- Derived reliability metrics
-- ---------------------------------------------------------------------------
-- Computed by `python -m src.metrics` and stored here (rather than recomputed
-- per viewer) so every consumer reads identical numbers.
--
-- Periods are bucketed in America/Puerto_Rico local time, so a "day" is a
-- local calendar day rather than a UTC one.
--
-- Index definitions and their confidence:
--   saidi_minutes  - integral of the customers-out curve / customers served.
--                    Well supported by interval sampling.
--   saifi_estimated- derived from rising edges of the customers-out curve.
--                    A LOWER BOUND: outages that start and end between two
--                    samples are invisible.
--   caidi_minutes  - saidi / saifi, so it inherits saifi's downward bias and
--                    is therefore an UPPER bound.
--   asai_percentage- 1 - saidi / minutes in period.

create table if not exists reliability_metrics (
    id                                bigint generated always as identity primary key,
    granularity                       text        not null check (granularity in ('hour', 'day', 'month')),
    period_start                      timestamptz not null,
    scope_name                        text        not null,   -- region name, or 'Island-wide'
    cause_basis                       text        not null check (cause_basis in ('all', 'unplanned', 'planned', 'load_shed')),
    customers_served                  integer,
    customer_minutes_interrupted      numeric,
    customers_interrupted_estimated   numeric,
    saidi_minutes                     numeric,
    saifi_estimated                   numeric,
    caidi_minutes                     numeric,
    asai_percentage                   numeric,
    peak_customers_without_service    integer,
    mean_customers_without_service    numeric,
    covered_minutes                   numeric,
    coverage_ratio                    numeric,
    computed_at                       timestamptz not null default now(),
    unique (granularity, period_start, scope_name, cause_basis)
);

create index if not exists reliability_metrics_lookup_idx
    on reliability_metrics (granularity, scope_name, cause_basis, period_start desc);

-- IEEE 1366 Major Event Days, classified with the 2.5 Beta Method.
-- `is_provisional` stays true until the threshold is backed by the standard's
-- recommended five years of daily history.
create table if not exists major_event_days (
    id                        bigint generated always as identity primary key,
    event_date                date    not null,
    scope_name                text    not null,
    cause_basis               text    not null,
    daily_saidi_minutes       numeric not null,
    threshold_t_med           numeric,
    is_major_event_day        boolean not null default false,
    days_used_in_threshold    integer,
    is_provisional            boolean not null default true,
    computed_at               timestamptz not null default now(),
    unique (event_date, scope_name, cause_basis)
);

create index if not exists major_event_days_lookup_idx
    on major_event_days (scope_name, cause_basis, event_date desc);

-- The metrics job re-computes recent periods in place, so it needs update
-- (and delete) in addition to insert.
grant select, insert, update, delete on reliability_metrics to service_role;
grant select, insert, update, delete on major_event_days    to service_role;
