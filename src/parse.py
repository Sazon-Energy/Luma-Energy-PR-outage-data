from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

PUERTO_RICO_TIMEZONE = ZoneInfo("America/Puerto_Rico")
SOURCE_TIMESTAMP_FORMAT = "%m/%d/%Y %I:%M %p"


@dataclass
class SnapshotRecord:
    source_timestamp: datetime
    source_timestamp_text: str
    total_clients: int | None
    total_clients_with_service: int | None
    total_clients_without_service: int | None
    total_clients_affected_by_planned_outage: int | None
    total_clients_affected_by_load_shed: int | None
    total_percentage_with_service: float | None
    total_percentage_without_service: float | None


@dataclass
class RegionRecord:
    region_name: str
    total_clients: int | None
    total_clients_with_service: int | None
    total_clients_without_service: int | None
    total_clients_affected_by_planned_outage: int | None
    total_clients_affected_by_load_shed: int | None
    percentage_clients_with_service: float | None
    percentage_clients_without_service: float | None


class InvalidPayloadError(ValueError):
    """Raised when a JSON reply lacks a readable `timestamp`.

    LUMA's API occasionally answers with JSON that has no timestamp at all
    (seen alongside its read timeouts), which is worth retrying rather than
    crashing on.
    """


def parse_source_timestamp(raw_timestamp_text: str) -> datetime:
    naive_timestamp = datetime.strptime(raw_timestamp_text, SOURCE_TIMESTAMP_FORMAT)
    return naive_timestamp.replace(tzinfo=PUERTO_RICO_TIMEZONE)


def parse_snapshot(response_body: dict) -> tuple[SnapshotRecord, list[RegionRecord]]:
    if not isinstance(response_body, dict) or not response_body.get("timestamp"):
        raise InvalidPayloadError(
            f"LUMA reply has no timestamp; reply starts: {str(response_body)[:200]}"
        )
    raw_timestamp_text = response_body["timestamp"]
    try:
        source_timestamp = parse_source_timestamp(raw_timestamp_text)
    except ValueError as format_error:
        raise InvalidPayloadError(
            f"LUMA timestamp {raw_timestamp_text!r} is unreadable ({format_error})"
        ) from format_error
    totals = response_body.get("totals") or {}

    snapshot_record = SnapshotRecord(
        source_timestamp=source_timestamp,
        source_timestamp_text=raw_timestamp_text,
        total_clients=totals.get("totalClients"),
        total_clients_with_service=totals.get("totalClientsWithService"),
        total_clients_without_service=totals.get("totalClientsWithoutService"),
        total_clients_affected_by_planned_outage=totals.get(
            "totalClientsAffectedByPlannedOutage"
        ),
        total_clients_affected_by_load_shed=totals.get(
            "totalClientsAffectedByLoadShed"
        ),
        # Note: the totals object names these fields "totalPercentage...",
        # while each region object below names the same measure
        # "percentageClients..." - that inconsistency comes from the LUMA API
        # itself, not a typo here.
        total_percentage_with_service=totals.get("totalPercentageWithService"),
        total_percentage_without_service=totals.get("totalPercentageWithoutService"),
    )

    region_records = [
        RegionRecord(
            region_name=region["name"],
            total_clients=region.get("totalClients"),
            total_clients_with_service=region.get("totalClientsWithService"),
            total_clients_without_service=region.get("totalClientsWithoutService"),
            total_clients_affected_by_planned_outage=region.get(
                "totalClientsAffectedByPlannedOutage"
            ),
            total_clients_affected_by_load_shed=region.get(
                "totalClientsAffectedByLoadShed"
            ),
            percentage_clients_with_service=region.get("percentageClientsWithService"),
            percentage_clients_without_service=region.get(
                "percentageClientsWithoutService"
            ),
        )
        for region in response_body.get("regions") or []
        if region.get("name")
    ]

    return snapshot_record, region_records
