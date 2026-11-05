"""Clay enrichment — name + company in, work email out.

Clay's API is asynchronous: POST starts a run, results are polled separately.
Polling rather than Clay's webhook, so nothing here needs a public endpoint
beyond the one Slack already posts to.

Two things Clay validates silently, hence the pinned constants in `conventions.py`: the input
names are `Full Name` and `Company` exactly (an unrecognised key is accepted and
ignored, so a typo fails at enrichment time rather than at the API), and the
routine id must carry the `function:` prefix or it's rejected as an invalid tool
id. A run can also complete while the item inside it failed.
"""

import re
import time
from typing import Any

import requests

from config import (
    CLAY_API_KEY,
    CLAY_POLL_INTERVAL_SECONDS,
    CLAY_POLL_TIMEOUT_SECONDS,
    CLAY_ROUTINE_ID,
)

BASE_URL = 'https://api.clay.com/public/v0'

# The function's declared input names, exactly as Clay validates them.
FULL_NAME_FIELD = 'Full Name'
COMPANY_FIELD = 'Company'
# The output column the email arrives in, under the item's `result`.
EMAIL_OUTPUT_FIELD = 'Work Email'

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s.]+\.[^@\s]+$')
_TERMINAL_RUN_STATUSES = {'complete', 'completed', 'succeeded', 'success', 'done', 'finished'}
_FAILED_ITEM_STATUSES = {'failed', 'error', 'errored', 'cancelled', 'canceled'}


class ClayError(RuntimeError):
    """Clay refused the request, or the run/item failed."""


def find_work_emails(people: list[tuple[str, str, str]]) -> dict[str, str | None]:
    """Look up several work emails in ONE Clay run, keyed by the id passed in.

    Three badges photographed together are one enrichment, not three: Clay's
    endpoint takes an items array, and three sequential runs meant three waits of
    up to `CLAY_POLL_TIMEOUT_SECONDS` while a rep stood at a booth.

    A `None` value means Clay looked and found nobody — terminal for that lead.
    A ClayError means the lookup could not be performed for anyone, which is
    retryable; the two must not be conflated.
    """
    items = []
    for lead_id, name, company in people:
        full_name, employer = (name or '').strip(), (company or '').strip()
        if not (full_name and employer):
            # Both are required by the function, and an unreadable badge can
            # leave either blank — fail here rather than spend a Clay credit on
            # a request Clay will reject anyway.
            raise ClayError(
                f'Clay needs a full name and a company; got name={full_name!r} company={employer!r}'
            )
        items.append(
            {
                'id': str(lead_id),
                'inputs': {FULL_NAME_FIELD: full_name, COMPANY_FIELD: employer},
            }
        )
    if not items:
        return {}

    run_id = _start_run(items)
    found = _poll_items(run_id)
    return {str(lead_id): found.get(str(lead_id)) for lead_id, _, _ in people}


def _headers() -> dict[str, str]:
    return {'clay-api-key': CLAY_API_KEY, 'content-type': 'application/json'}


def _get(url: str):
    """Conference wifi drops. Wrap transport failures so the tool layer sees one
    exception type and reports them as retryable instead of aborting the turn."""
    try:
        return requests.get(url, headers=_headers(), timeout=30)
    except requests.RequestException as error:
        raise ClayError(f'Could not reach Clay ({type(error).__name__})') from None


def _start_run(items: list[dict[str, Any]]) -> str:
    try:
        response = requests.post(
            f'{BASE_URL}/routines/{CLAY_ROUTINE_ID}/run',
            headers=_headers(),
            json={'items': items},
            timeout=30,
        )
    except requests.RequestException as error:
        raise ClayError(f'Could not reach Clay ({type(error).__name__})') from None
    if response.status_code >= 400:
        raise ClayError(f'Clay rejected the run ({response.status_code}): {response.text[:400]}')

    run_id = (response.json() or {}).get('routine_run_id')
    if not run_id:
        raise ClayError(f'Clay did not return a routine_run_id: {response.text[:400]}')
    return run_id


def _poll_items(run_id: str) -> dict[str, str | None]:
    """Wait for the run, then the email found for each item id it returns.

    A missing or failed item is `None` rather than an omission, so one person
    Clay couldn't place doesn't look like a lookup that never ran.
    """
    deadline = time.monotonic() + CLAY_POLL_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        response = _get(f'{BASE_URL}/routines/run/{run_id}/results')
        # 202 means still running; 404 can appear briefly before the run registers.
        if response.status_code in (202, 404):
            time.sleep(CLAY_POLL_INTERVAL_SECONDS)
            continue
        if response.status_code >= 400:
            raise ClayError(f'Clay results failed ({response.status_code}): {response.text[:400]}')

        payload = response.json() or {}
        if str(payload.get('status', '')).lower() not in _TERMINAL_RUN_STATUSES:
            time.sleep(CLAY_POLL_INTERVAL_SECONDS)
            continue

        # The run reached a terminal state, so a failed item means Clay looked
        # and came back with nobody. That is a fact about the prospect, not a
        # failure of the lookup — raising would make it retryable forever.
        return {
            str(item.get('id')): (
                None
                if str(item.get('status', '')).lower() in _FAILED_ITEM_STATUSES
                else _read_email(item)
            )
            for item in payload.get('data') or []
        }

    raise ClayError(f'Clay run {run_id} did not finish within {CLAY_POLL_TIMEOUT_SECONDS}s')


def _read_email(item: dict[str, Any]) -> str | None:
    result = item.get('result')
    if not isinstance(result, dict):
        return None
    value = result.get(EMAIL_OUTPUT_FIELD)
    if isinstance(value, str) and _EMAIL_RE.match(value.strip()):
        return value.strip()
    # Present but not an address — treat as not found rather than pushing
    # something Marketo will reject or dedupe against the wrong person.
    return None
