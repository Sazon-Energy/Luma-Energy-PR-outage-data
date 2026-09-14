from __future__ import annotations

import os

from supabase import Client, create_client

from src.parse import RegionRecord, SnapshotRecord
from src.retry import with_retry


def build_supabase_client() -> Client:
    supabase_url = os.environ["SUPABASE_URL"]
    supabase_service_role_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return create_client(supabase_url, supabase_service_role_key)


def write_snapshot(
    supabase_client: Client,
    snapshot_record: SnapshotRecord,
    region_records: list[RegionRecord],
) -> int | None:
    """Insert one snapshot and its region readings; return the new snapshot id.

    Returns None (and inserts nothing) if a snapshot for this
    source_timestamp already exists, so re-running the collector within the
    same source hour never creates duplicate history.
    """
    snapshot_row = {
        "source_timestamp": snapshot_record.source_timestamp.isoformat(),
        "source_timestamp_text": snapshot_record.source_timestamp_text,
        "total_clients": snapshot_record.total_clients,
        "total_clients_with_service": snapshot_record.total_clients_with_service,
        "total_clients_without_service": snapshot_record.total_clients_without_service,
        "total_clients_affected_by_planned_outage": (
            snapshot_record.total_clients_affected_by_planned_outage
        ),
        "total_clients_affected_by_load_shed": (
            snapshot_record.total_clients_affected_by_load_shed
        ),
        "total_percentage_with_service": snapshot_record.total_percentage_with_service,
        "total_percentage_without_service": (
            snapshot_record.total_percentage_without_service
        ),
    }

    insert_response = with_retry(
        lambda: supabase_client.table("outage_snapshots")
        .upsert(snapshot_row, on_conflict="source_timestamp", ignore_duplicates=True)
        .execute(),
        description="upsert outage_snapshots",
    )

    if not insert_response.data:
        return None

    snapshot_id = insert_response.data[0]["id"]

    region_rows = [
        {
            "snapshot_id": snapshot_id,
            "region_name": region_record.region_name,
            "total_clients": region_record.total_clients,
            "total_clients_with_service": region_record.total_clients_with_service,
            "total_clients_without_service": (
                region_record.total_clients_without_service
            ),
            "total_clients_affected_by_planned_outage": (
                region_record.total_clients_affected_by_planned_outage
            ),
            "total_clients_affected_by_load_shed": (
                region_record.total_clients_affected_by_load_shed
            ),
            "percentage_clients_with_service": (
                region_record.percentage_clients_with_service
            ),
            "percentage_clients_without_service": (
                region_record.percentage_clients_without_service
            ),
        }
        for region_record in region_records
    ]

    if region_rows:
        with_retry(
            lambda: supabase_client.table("region_readings")
            .upsert(
                region_rows,
                on_conflict="snapshot_id,region_name",
                ignore_duplicates=True,
            )
            .execute(),
            description="upsert region_readings",
        )

    return snapshot_id
