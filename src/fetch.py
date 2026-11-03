from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from pathlib import Path

import requests

from src.attempt_log import (
    OUTCOME_CAPTURE_ERROR,
    OUTCOME_CONNECTION_ERROR,
    OUTCOME_TIMEOUT,
    OUTCOME_WAF_BLOCKED,
)
from src.constants import (
    BROWSERLESS_FUNCTION_URL,
    BROWSERLESS_REQUEST_TIMEOUT_SECONDS,
    REGION_ENDPOINT_URL,
    REQUEST_TIMEOUT_SECONDS,
    STATUS_PAGE_URL,
    USER_AGENTS,
)

CAPTURE_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "capture_region_snapshot.js"
)
RESPONSE_PREVIEW_LENGTH = 300


class FetchAttemptFailedError(Exception):
    """One attempt to get the regionsWithoutService JSON failed.

    `outcome` is one of the attempt_log OUTCOME_* values, so the failure can
    be recorded and charted by cause.
    """

    def __init__(self, outcome: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.outcome = outcome
        self.http_status = http_status


@dataclass
class FetchedPayload:
    response_body: object
    http_status: int | None


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,es-PR;q=0.8",
            "Referer": STATUS_PAGE_URL,
        }
    )
    return session


def read_override_payload(override_file_path: str) -> FetchedPayload:
    """Read a saved regionsWithoutService reply instead of the network.

    Used for local testing via the CLIENTS_WITHOUT_SERVICE_FILE environment
    variable.
    """
    with open(override_file_path, encoding="utf-8") as override_file:
        return FetchedPayload(response_body=json.load(override_file), http_status=None)


def fetch_region_snapshot_directly() -> FetchedPayload:
    """One direct request to the LUMA regionsWithoutService endpoint.

    A fresh session per attempt means a newly picked User-Agent and no
    cookies carried over from a response that just got blocked. A non-JSON
    reply is the Incapsula WAF's challenge page.
    """
    session = build_session()
    try:
        response = session.get(
            REGION_ENDPOINT_URL,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
    except requests.exceptions.Timeout as timeout_error:
        raise FetchAttemptFailedError(OUTCOME_TIMEOUT, str(timeout_error)) from timeout_error
    except requests.exceptions.RequestException as connection_error:
        raise FetchAttemptFailedError(
            OUTCOME_CONNECTION_ERROR, str(connection_error)
        ) from connection_error

    try:
        return FetchedPayload(response.json(), response.status_code)
    except ValueError as decode_error:
        raise FetchAttemptFailedError(
            OUTCOME_WAF_BLOCKED,
            f"LUMA outage API did not return JSON (http status "
            f"{response.status_code}); likely blocked by the Incapsula WAF",
            response.status_code,
        ) from decode_error


def capture_region_snapshot_via_browserless() -> FetchedPayload:
    """One capture through a local browserless container.

    Posts scripts/capture_region_snapshot.js to browserless's /function
    endpoint, which loads the real status page in headless Chrome and returns
    the regionsWithoutService reply the page itself receives - that passes
    the WAF like any other browser visit. On failure the script still
    returns JSON, carrying captureError plus the page title, which ends up in
    the attempt log.
    """
    capture_script = CAPTURE_SCRIPT_PATH.read_text(encoding="utf-8")
    try:
        response = requests.post(
            BROWSERLESS_FUNCTION_URL,
            json={"code": capture_script},
            timeout=BROWSERLESS_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.Timeout as timeout_error:
        raise FetchAttemptFailedError(OUTCOME_TIMEOUT, str(timeout_error)) from timeout_error
    except requests.exceptions.RequestException as connection_error:
        raise FetchAttemptFailedError(
            OUTCOME_CONNECTION_ERROR, f"browserless not reachable: {connection_error}"
        ) from connection_error

    try:
        capture_result = response.json()
    except ValueError as decode_error:
        raise FetchAttemptFailedError(
            OUTCOME_CAPTURE_ERROR,
            f"browserless returned non-JSON (http status {response.status_code}): "
            f"{response.text[:RESPONSE_PREVIEW_LENGTH]}",
            response.status_code,
        ) from decode_error

    if isinstance(capture_result, dict) and "captureError" in capture_result:
        raise FetchAttemptFailedError(
            OUTCOME_CAPTURE_ERROR,
            f"{capture_result.get('captureError')} (page title: "
            f"{capture_result.get('pageTitle')!r}, page url: "
            f"{capture_result.get('pageUrl')})",
            response.status_code,
        )
    return FetchedPayload(capture_result, response.status_code)


def override_file_path() -> str | None:
    return os.environ.get("CLIENTS_WITHOUT_SERVICE_FILE")
