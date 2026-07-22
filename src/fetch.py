import json
import os
import random

import requests

from src.constants import (
    REGION_ENDPOINT_URL,
    REQUEST_TIMEOUT_SECONDS,
    STATUS_PAGE_URL,
    USER_AGENTS,
)


class OutageApiBlockedError(Exception):
    """Raised when the LUMA outage API responds with something other than JSON.

    This happens when the Incapsula WAF in front of the API serves a
    challenge page instead of the real response.
    """


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


def fetch_region_snapshot() -> dict:
    """Return the parsed JSON body of the LUMA regionsWithoutService endpoint.

    If the CLIENTS_WITHOUT_SERVICE_FILE environment variable is set, the JSON
    is read from that file instead of the network. The GitHub Actions
    workflow uses this seam to hand off a payload captured by a headless
    browser when a direct request gets blocked by the WAF.
    """
    override_file_path = os.environ.get("CLIENTS_WITHOUT_SERVICE_FILE")
    if override_file_path:
        with open(override_file_path, encoding="utf-8") as override_file:
            return json.load(override_file)

    session = build_session()
    response = session.get(
        REGION_ENDPOINT_URL, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True
    )

    try:
        return response.json()
    except ValueError as decode_error:
        raise OutageApiBlockedError(
            f"LUMA outage API did not return JSON (http status "
            f"{response.status_code}); likely blocked by the Incapsula WAF"
        ) from decode_error
