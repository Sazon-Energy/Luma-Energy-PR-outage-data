"""Retry helper for the transient network/gateway errors this pipeline hits.

Supabase's REST gateway occasionally answers with a 502/503/504 for a few
seconds; either that or the underlying connection to Supabase or LUMA drops
outright. Neither is worth failing a whole collection run over, since the
next attempt a few seconds later almost always succeeds.
"""

from __future__ import annotations

import time
from typing import Callable, TypeVar

import httpx
import requests
from postgrest.exceptions import APIError

TRANSIENT_POSTGREST_CODES = {"502", "503", "504"}
MAX_ATTEMPTS = 3
BASE_DELAY_SECONDS = 2.0

ResultType = TypeVar("ResultType")


def _is_transient(error: Exception) -> bool:
    if isinstance(error, APIError):
        return str(error.code) in TRANSIENT_POSTGREST_CODES
    return isinstance(
        error,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            httpx.TransportError,
        ),
    )


def with_retry(operation: Callable[[], ResultType], description: str) -> ResultType:
    """Call operation(), retrying transient network/gateway failures.

    Re-raises the triggering exception unchanged once attempts are
    exhausted, or immediately if it isn't judged transient.
    """
    attempt = 1
    while True:
        try:
            return operation()
        except Exception as error:
            if attempt >= MAX_ATTEMPTS or not _is_transient(error):
                raise
            delay_seconds = BASE_DELAY_SECONDS * (2 ** (attempt - 1))
            print(
                f"warning: {description} failed ({error}); "
                f"retrying in {delay_seconds:.0f}s "
                f"(attempt {attempt}/{MAX_ATTEMPTS})"
            )
            time.sleep(delay_seconds)
            attempt += 1
