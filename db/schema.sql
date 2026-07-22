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
