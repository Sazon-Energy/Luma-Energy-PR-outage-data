"""Records every attempt to fetch LUMA's outage figures in `collection_attempts`.

Logging is strictly best effort: a failure to record an attempt prints a
warning and is otherwise ignored, so it can never cost a snapshot.
"""

from __future__ import annotations

import os
from datetime import datetime

from supabase import Client

from src.retry import with_retry

METHOD_DIRECT = "direct"
METHOD_BROWSERLESS = "browserless"

OUTCOME_CAPTURED = "captured"
OUTCOME_ALREADY_RECORDED = "already_recorded"
OUTCOME_WAF_BLOCKED = "waf_blocked"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_CONNECTION_ERROR = "connection_error"
OUTCOME_INVALID_PAYLOAD = "invalid_payload"
OUTCOME_CAPTURE_ERROR = "capture_error"
OUTCOME_WRITE_ERROR = "write_error"

SUCCESSFUL_OUTCOMES = {OUTCOME_CAPTURED, OUTCOME_ALREADY_RECORDED}

ERROR_MESSAGE_MAXIMUM_LENGTH = 500


def _optional_integer_environment_variable(name: str) -> int | None:
    raw_value = os.environ.get(name)
    return int(raw_value) if raw_value and raw_value.isdigit() else None


def record_attempt(
    supabase_client: Client,
    method: str,
    attempt_number: int,
    started_at: datetime,
    duration_milliseconds: int,
    outcome: str,
    http_status: int | None = None,
    error_message: str | None = None,
    source_timestamp: datetime | None = None,
) -> None:
    attempt_row = {
        # Set automatically inside GitHub Actions; absent for local runs.
        "github_run_id": _optional_integer_environment_variable("GITHUB_RUN_ID"),
        "github_run_attempt": _optional_integer_environment_variable(
            "GITHUB_RUN_ATTEMPT"
        ),
        "method": method,
        "attempt_number": attempt_number,
        "started_at": started_at.isoformat(),
        "duration_milliseconds": duration_milliseconds,
        "outcome": outcome,
        "http_status": http_status,
        "error_message": (
            error_message[:ERROR_MESSAGE_MAXIMUM_LENGTH] if error_message else None
        ),
        "source_timestamp": source_timestamp.isoformat() if source_timestamp else None,
    }
    try:
        with_retry(
            lambda: supabase_client.table("collection_attempts")
            .insert(attempt_row)
            .execute(),
            description="insert collection_attempts",
        )
    except Exception as logging_error:
        print(f"warning: could not record collection attempt ({logging_error})")
