"""Reads the history of the "Publish reliability dashboard" workflow.

The status page uses it to show how reliably the site itself gets published.
The runs come straight from the GitHub Actions API, so no table is needed.
The repository is public, but GitHub limits unauthenticated calls to 60 an
hour, so the workflow passes its GITHUB_TOKEN in.

The run that is building the page is still in progress and is never in the
list, so every figure describes earlier runs.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests

from src.dashboard.data import _parse_timestamp
from src.retry import with_retry

GITHUB_API_URL = "https://api.github.com"
PUBLISH_WORKFLOW_FILE_NAME = "publish_dashboard.yml"
RUNS_PAGE_SIZE = 100
REQUEST_TIMEOUT_SECONDS = 20
# Stays well below GitHub's limit of 100 concurrent requests per token.
PARALLEL_REQUEST_LIMIT = 8
ERROR_MESSAGE_MAXIMUM_LENGTH = 300

CONCLUSION_SUCCESS = "success"
CONCLUSION_FAILURE = "failure"
CONCLUSION_CANCELLED = "cancelled"
CONCLUSION_SKIPPED = "skipped"


@dataclass
class PublishRun:
    run_id: int
    created_at: datetime
    run_started_at: datetime
    updated_at: datetime
    conclusion: str | None
    html_url: str
    first_error_message: str | None = None

    @property
    def duration_seconds(self) -> float:
        return (self.updated_at - self.run_started_at).total_seconds()


def _github_get(session: requests.Session, path: str, parameters: dict | None = None):
    def send_request():
        response = session.get(
            f"{GITHUB_API_URL}{path}",
            params=parameters,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()

    return with_retry(send_request, description=f"GET {path}")


def _first_error_message(
    session: requests.Session, repository: str, run_id: int
) -> str | None:
    """The first error annotation of a failed run, such as deploy-pages'
    "No artifacts named github-pages were found". A job's id is also its
    check run id, which is what the annotations endpoint takes."""
    jobs = _github_get(session, f"/repos/{repository}/actions/runs/{run_id}/jobs")
    for job in jobs.get("jobs", []):
        if job.get("conclusion") != CONCLUSION_FAILURE:
            continue
        annotations = _github_get(
            session, f"/repos/{repository}/check-runs/{job['id']}/annotations"
        )
        for annotation in annotations:
            if annotation.get("annotation_level") == "failure":
                return annotation.get("message", "")[:ERROR_MESSAGE_MAXIMUM_LENGTH]
    return None


def load_publish_runs(since: datetime, error_details_since: datetime) -> list[PublishRun]:
    """Every completed publish run created since `since`, oldest first.
    Failed runs created since `error_details_since` also get their first
    error message, which costs two extra API calls per failure."""
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not repository:
        print("warning: GITHUB_REPOSITORY not set; skipping publish run history")
        return []

    token = os.environ.get("GITHUB_TOKEN")

    # GitHub returns at most 1,000 runs for any filtered query, and a month
    # is about 4,300 runs, so ask for one UTC day at a time (~150 runs each).
    # Each page takes a second or more to come back, so the days are fetched
    # in parallel; one after another, a month took about 90 seconds.
    first_day = since.astimezone(timezone.utc).date()
    query_days = [
        first_day + timedelta(days=day_offset)
        for day_offset in range((datetime.now(timezone.utc).date() - first_day).days + 1)
    ]
    with ThreadPoolExecutor(max_workers=PARALLEL_REQUEST_LIMIT) as executor:
        runs_by_day = executor.map(
            lambda query_day: _load_runs_for_day(repository, token, query_day, since),
            query_days,
        )
        runs = [run for day_runs in runs_by_day for run in day_runs]

        def attach_first_error_message(run: PublishRun) -> None:
            try:
                run.first_error_message = _first_error_message(
                    _build_session(token), repository, run.run_id
                )
            except Exception as lookup_error:
                print(
                    f"warning: could not read errors for run {run.run_id} ({lookup_error})"
                )

        list(
            executor.map(
                attach_first_error_message,
                [
                    run
                    for run in runs
                    if run.conclusion == CONCLUSION_FAILURE
                    and run.created_at >= error_details_since
                ],
            )
        )

    return sorted(runs, key=lambda run: run.created_at)


def _build_session(token: str | None) -> requests.Session:
    """One session per worker thread, since requests.Session isn't
    guaranteed to be thread-safe."""
    session = requests.Session()
    session.headers["Accept"] = "application/vnd.github+json"
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    return session


def _load_runs_for_day(
    repository: str, token: str | None, query_day: date, since: datetime
) -> list[PublishRun]:
    session = _build_session(token)
    day_runs: list[PublishRun] = []
    page_number = 1
    while True:
        page = _github_get(
            session,
            f"/repos/{repository}/actions/workflows/{PUBLISH_WORKFLOW_FILE_NAME}/runs",
            parameters={
                "created": query_day.isoformat(),
                "status": "completed",
                "per_page": RUNS_PAGE_SIZE,
                "page": page_number,
            },
        )
        rows = page.get("workflow_runs", [])
        for row in rows:
            created_at = _parse_timestamp(row["created_at"])
            if created_at < since:
                continue
            day_runs.append(
                PublishRun(
                    run_id=row["id"],
                    created_at=created_at,
                    run_started_at=_parse_timestamp(
                        row.get("run_started_at") or row["created_at"]
                    ),
                    updated_at=_parse_timestamp(row["updated_at"]),
                    conclusion=row.get("conclusion"),
                    html_url=row["html_url"],
                )
            )
        if len(rows) < RUNS_PAGE_SIZE:
            return day_runs
        page_number += 1
