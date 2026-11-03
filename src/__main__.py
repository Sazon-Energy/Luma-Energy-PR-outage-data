"""Collect one LUMA outage snapshot: `python -m src [--method direct|browserless]`.

Keeps retrying the chosen method until its deadline (see constants.py),
recording every attempt in `collection_attempts`.
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from src.attempt_log import (
    METHOD_BROWSERLESS,
    METHOD_DIRECT,
    OUTCOME_ALREADY_RECORDED,
    OUTCOME_CAPTURED,
    OUTCOME_INVALID_PAYLOAD,
    OUTCOME_WRITE_ERROR,
    record_attempt,
)
from src.constants import (
    BROWSERLESS_CAPTURE_DEADLINE_SECONDS,
    DIRECT_FETCH_DEADLINE_SECONDS,
    RETRY_DELAY_MAXIMUM_SECONDS,
    RETRY_DELAY_STEP_SECONDS,
)
from src.fetch import (
    FetchAttemptFailedError,
    capture_region_snapshot_via_browserless,
    fetch_region_snapshot_directly,
    override_file_path,
    read_override_payload,
)
from src.parse import InvalidPayloadError, parse_snapshot
from src.supabase_writer import build_supabase_client, write_snapshot

# Distinct from any other failure so the GitHub Actions workflow can tell
# "could not get LUMA's figures" (worth retrying via the headless-browser
# fallback) apart from a real error such as a Supabase permission or schema
# problem (not worth retrying, since it would just fail the same way).
FETCH_FAILED_EXIT_CODE = 2

DEADLINE_SECONDS_BY_METHOD = {
    METHOD_DIRECT: DIRECT_FETCH_DEADLINE_SECONDS,
    METHOD_BROWSERLESS: BROWSERLESS_CAPTURE_DEADLINE_SECONDS,
}


def collection_started_at_epoch_seconds() -> float:
    """When this collection run began, so both methods share one deadline
    clock. The workflow sets COLLECTION_STARTED_AT at job start; local runs
    start the clock now."""
    raw_value = os.environ.get("COLLECTION_STARTED_AT")
    return float(raw_value) if raw_value else time.time()


def retry_delay_seconds(attempt_number: int) -> float:
    return min(RETRY_DELAY_STEP_SECONDS * attempt_number, RETRY_DELAY_MAXIMUM_SECONDS)


def main() -> int:
    argument_parser = argparse.ArgumentParser(
        description="Collect one LUMA outage snapshot into Supabase."
    )
    argument_parser.add_argument(
        "--method",
        choices=[METHOD_DIRECT, METHOD_BROWSERLESS],
        default=METHOD_DIRECT,
        help="direct request (default) or capture through a local browserless container",
    )
    arguments = argument_parser.parse_args()

    supabase_client = build_supabase_client()
    deadline_epoch_seconds = (
        collection_started_at_epoch_seconds()
        + DEADLINE_SECONDS_BY_METHOD[arguments.method]
    )
    saved_payload_path = override_file_path()
    if saved_payload_path:
        fetch_payload = lambda: read_override_payload(saved_payload_path)
    elif arguments.method == METHOD_BROWSERLESS:
        fetch_payload = capture_region_snapshot_via_browserless
    else:
        fetch_payload = fetch_region_snapshot_directly

    attempt_number = 1
    while True:
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()

        def elapsed_milliseconds() -> int:
            return int((time.monotonic() - started_monotonic) * 1000)

        http_status = None
        try:
            fetched_payload = fetch_payload()
            http_status = fetched_payload.http_status
            snapshot_record, region_records = parse_snapshot(
                fetched_payload.response_body
            )
        except (FetchAttemptFailedError, InvalidPayloadError) as attempt_error:
            outcome = getattr(attempt_error, "outcome", OUTCOME_INVALID_PAYLOAD)
            http_status = getattr(attempt_error, "http_status", None) or http_status
            record_attempt(
                supabase_client,
                method=arguments.method,
                attempt_number=attempt_number,
                started_at=started_at,
                duration_milliseconds=elapsed_milliseconds(),
                outcome=outcome,
                http_status=http_status,
                error_message=str(attempt_error),
            )
            delay_seconds = retry_delay_seconds(attempt_number)
            # A saved payload gives the same answer every time; don't retry it.
            if saved_payload_path or time.time() + delay_seconds >= deadline_epoch_seconds:
                print(
                    f"error: {arguments.method} attempt {attempt_number} failed "
                    f"({outcome}: {attempt_error}); deadline reached, giving up",
                    file=sys.stderr,
                )
                return FETCH_FAILED_EXIT_CODE
            print(
                f"warning: {arguments.method} attempt {attempt_number} failed "
                f"({outcome}: {attempt_error}); retrying in {delay_seconds:.0f}s"
            )
            time.sleep(delay_seconds)
            attempt_number += 1
            continue

        try:
            snapshot_id = write_snapshot(supabase_client, snapshot_record, region_records)
        except Exception as write_error:
            record_attempt(
                supabase_client,
                method=arguments.method,
                attempt_number=attempt_number,
                started_at=started_at,
                duration_milliseconds=elapsed_milliseconds(),
                outcome=OUTCOME_WRITE_ERROR,
                http_status=http_status,
                error_message=str(write_error),
                source_timestamp=snapshot_record.source_timestamp,
            )
            raise

        record_attempt(
            supabase_client,
            method=arguments.method,
            attempt_number=attempt_number,
            started_at=started_at,
            duration_milliseconds=elapsed_milliseconds(),
            outcome=OUTCOME_CAPTURED if snapshot_id is not None else OUTCOME_ALREADY_RECORDED,
            http_status=http_status,
            source_timestamp=snapshot_record.source_timestamp,
        )
        if snapshot_id is None:
            print(
                f"snapshot for {snapshot_record.source_timestamp_text} "
                "already recorded; skipped"
            )
        else:
            print(
                f"recorded snapshot {snapshot_id} for "
                f"{snapshot_record.source_timestamp_text} with "
                f"{len(region_records)} region readings via {arguments.method} "
                f"(attempt {attempt_number})"
            )
        return 0


if __name__ == "__main__":
    sys.exit(main())
