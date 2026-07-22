import sys

from src.fetch import OutageApiBlockedError, fetch_region_snapshot
from src.parse import parse_snapshot
from src.supabase_writer import build_supabase_client, write_snapshot

# Distinct from any other failure so the GitHub Actions workflow can tell a
# WAF block (worth retrying via the headless-browser fallback) apart from a
# real error such as a Supabase permission or schema problem (not worth
# retrying, since retrying the fetch again would just fail the same way).
WAF_BLOCKED_EXIT_CODE = 2


def main() -> int:
    try:
        response_body = fetch_region_snapshot()
    except OutageApiBlockedError as blocked_error:
        print(f"error: {blocked_error}", file=sys.stderr)
        return WAF_BLOCKED_EXIT_CODE

    snapshot_record, region_records = parse_snapshot(response_body)

    supabase_client = build_supabase_client()
    snapshot_id = write_snapshot(supabase_client, snapshot_record, region_records)

    if snapshot_id is None:
        print(
            f"snapshot for {snapshot_record.source_timestamp_text} "
            "already recorded; skipped"
        )
    else:
        print(
            f"recorded snapshot {snapshot_id} for "
            f"{snapshot_record.source_timestamp_text} with "
            f"{len(region_records)} region readings"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
