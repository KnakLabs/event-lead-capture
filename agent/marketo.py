"""Marketo: find the event program a Slack channel names, then push the lead.

The channel is named after the program, so nothing is inferred and nobody is
asked. Both names reduce to a canonical form before comparing, which absorbs
what Slack forces on a name (lowercase, no spaces). Which programs qualify as
events, and the folder they live under, are `conventions.py`'s to define — this
module only applies them.

Listing them walks Marketo's folder tree, which takes seconds, so `cache` holds
the answer: the program list for an hour, and each channel's resolved program
for the life of the container.
"""

import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import requests

from config import (
    MARKETO_BASE_URL,
    MARKETO_CLIENT_ID,
    MARKETO_CLIENT_SECRET,
    MARKETO_UI_HOST,
)
from conventions import (
    EVENTS_ROOT_TEMPLATE,
    NOTES_FIELD,
    PROGRAM_NAME_PATTERN,
    PROGRAM_STATUS,
)

# One email can match several Marketo records — typically a Salesforce Contact
# alongside an older Lead that was never merged. Marketo refuses to guess and
# skips the push, which used to strand the rep behind a data-quality problem
# they cannot fix from a booth. Pick one instead, preferring the record most
# likely to be the person's live, sales-owned identity:
#
#   1. SFDC type   Contact, then Lead, then anything not synced to Salesforce
#   2. updatedAt   most recently touched
#   3. createdAt   earliest — the original the others duplicate
#
# Anything Marketo doesn't sync to Salesforce leaves sfdcType null, which is why
# unranked sorts last rather than raising.
_SFDC_TYPE_ORDER = ('contact', 'lead')

# The only fields the ranking reads. Asking for all 1374 would work and waste
# most of the response.
_DEDUPE_FIELDS = 'id,email,createdAt,updatedAt,sfdcType'

CACHE_TTL_SECONDS = 60 * 60
# Short, so a renamed channel or a newly created program is picked up quickly.
UNMATCHED_TTL_SECONDS = 60

# Marketo signals an expired token with these codes rather than a 401.
_TOKEN_ERROR_CODES = {'601', '602'}


class MarketoError(RuntimeError):
    """Marketo returned an error we can't recover from."""


@dataclass
class EventProgram:
    id: int
    name: str
    event_name: str
    activation: str
    created_at: str = ''

    @property
    def label(self) -> str:
        if self.event_name and self.activation:
            return f'{self.event_name} — {self.activation}'
        # A name with no parsable suffix still has to show something.
        return self.event_name or self.name


def lead_url(lead_id: Any) -> str:
    """Deep link to a person's record, for whoever reads the sheet."""
    if not lead_id:
        return ''
    return f'{MARKETO_UI_HOST}/leadDatabase/loadLeadDetail?leadId={lead_id}&accessZoneId=1'


def parse_program_name(name: str) -> tuple[str, str] | None:
    """(event name, event type), or None if this isn't an event program.

    ...Event-Adobe Summit-Booth   -> ('Adobe Summit', 'Booth')
    ...Event-MOps-Apalooza-Booth  -> ('MOps-Apalooza', 'Booth')
    ...Event-Test                 -> ('Test', '')
    """
    match = PROGRAM_NAME_PATTERN.match(name or '')
    if match is None:
        return None
    remainder = (name[match.end() :] or '').lstrip('-').strip()
    if not remainder:
        return '', ''
    if '-' in remainder:
        event, _, activation = remainder.rpartition('-')
        return event.strip(), activation.strip()
    return remainder, ''


def canonical(name: str) -> str:
    """Lowercase, every run of non-alphanumerics collapsed to one hyphen.

    Applied to both sides, so it doesn't matter whether Slack turned the
    program's spaces into hyphens or underscores, or collapsed a doubled
    separator in `...Event-Dreamforce`.
    """
    return re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-')


def find_by_channel(channel_name: str, programs: list[EventProgram]) -> EventProgram | None:
    """Exact canonical match, else a channel that prefixes exactly one program.

    The prefix rule exists because Slack caps channel names at 80 characters and
    the longest real program name already reaches 75. An ambiguous prefix
    resolves to nothing rather than a guess.
    """
    target = canonical(channel_name)
    if not target:
        return None
    for program in programs:
        if canonical(program.name) == target:
            return program
    prefixed = [p for p in programs if canonical(p.name).startswith(target)]
    return prefixed[0] if len(prefixed) == 1 else None


def push_lead(
    *,
    program: EventProgram,
    email: str,
    name: str = '',
    company: str = '',
    job_title: str = '',
    phone: str = '',
    notes: str = '',
    lead_id: Any = None,
) -> dict[str, Any]:
    """Create or update the person and set their status in the event program.

    An email is mandatory — it's the dedupe key, and pushing without one would
    create an unmergeable orphan record.

    `lead_id` re-pushes against a record we already created, which is how a
    correction the rep makes after the push reaches Marketo instead of leaving
    the record stale. It bypasses the email lookup entirely, so a corrected
    email lands on the right record rather than creating a second one.
    """
    if not (email or '').strip():
        raise ValueError('Refusing to push a lead without an email address')

    # split() on any whitespace run, not partition(' '): a badge printing the
    # name over two lines reaches us with a newline in it, which would put the
    # whole name in firstName and leave lastName empty.
    parts = (name or '').split()
    record: dict[str, Any] = {'email': email.strip()}
    for key, value in (
        ('firstName', parts[0] if parts else ''),
        ('lastName', ' '.join(parts[1:])),
        ('company', company),
        ('title', job_title),
        ('phone', phone),
        (NOTES_FIELD, notes),
    ):
        if value:
            record[key] = value

    if lead_id:
        updated = _push({**record, 'id': int(lead_id)}, program, lookup_field='id')
        if updated.get('status') == 'skipped':
            raise MarketoError(f'Marketo skipped the update: {updated.get("reasons")}')
        return updated

    first = _push(record, program, lookup_field='email')
    if first.get('status') != 'skipped':
        return first

    # A skip is how Marketo reports a lookup that matched more than one record,
    # but the reason code and wording aren't in the published spec — so check the
    # condition itself rather than parsing the message. A skip for any other
    # reason still surfaces its original text.
    matches = find_leads_by_email(email)
    if len(matches) < 2:
        raise MarketoError(f'Marketo skipped the lead: {first.get("reasons")}')

    chosen = choose_duplicate(matches)
    resolved = _push({**record, 'id': chosen['id']}, program, lookup_field='id')
    if resolved.get('status') == 'skipped':
        raise MarketoError(f'Marketo skipped the lead: {resolved.get("reasons")}')
    return {
        **resolved,
        'resolved_duplicate': {
            'considered': len(matches),
            'chosen_id': chosen['id'],
            'sfdc_type': chosen.get('sfdcType') or 'not in Salesforce',
        },
    }


def _push(record: dict[str, Any], program: EventProgram, *, lookup_field: str) -> dict[str, Any]:
    payload = _request(
        'POST',
        '/rest/v1/leads/push.json',
        headers={'Content-Type': 'application/json'},
        json={
            'programName': program.name,
            'programStatus': PROGRAM_STATUS,
            'lookupField': lookup_field,
            'input': [record],
        },
    )
    return (payload.get('result') or [{}])[0]


def find_leads_by_email(email: str) -> list[dict[str, Any]]:
    payload = _request(
        'GET',
        '/rest/v1/leads.json',
        params={
            'filterType': 'email',
            'filterValues': (email or '').strip(),
            'fields': _DEDUPE_FIELDS,
        },
    )
    return payload.get('result') or []


def choose_duplicate(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The record to update when one email matches several.

    Kept pure and separate from the push so the ranking can be tested without
    Marketo.
    """
    return min(records, key=_dedupe_rank) if records else None


def _dedupe_rank(record: dict[str, Any]) -> tuple[int, float, float, int]:
    return (
        _sfdc_rank(record.get('sfdcType')),
        # Negated: min() is picking, so "most recent" has to be the smaller key.
        -_timestamp(record.get('updatedAt'), missing=0.0),
        _timestamp(record.get('createdAt'), missing=math.inf),
        # Two records identical on all three above still have to order somehow,
        # or which one gets updated varies between calls.
        int(record.get('id') or 0),
    )


def _sfdc_rank(sfdc_type: Any) -> int:
    try:
        return _SFDC_TYPE_ORDER.index(str(sfdc_type or '').strip().lower())
    except ValueError:
        return len(_SFDC_TYPE_ORDER)


def _timestamp(value: Any, *, missing: float) -> float:
    """ISO-8601 as a sortable number. A missing or unreadable one must never win,
    so callers pass the losing end of whichever direction they sort."""
    if not value:
        return missing
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return missing


# ------------------------------------------------------------------ discovery


def list_event_programs(year: int | None = None) -> list[EventProgram]:
    """Every event program under this year's events folder, newest first."""
    root_id = _folder_id(EVENTS_ROOT_TEMPLATE.format(year=year or date.today().year))
    if root_id is None:
        return []
    programs: list[EventProgram] = []
    for folder_id in _walk_folders(root_id):
        programs.extend(_programs_in_folder(folder_id))
    deduped = list({p.id: p for p in programs}.values())
    deduped.sort(key=lambda program: program.created_at, reverse=True)
    return deduped


class ProgramCache:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._programs: list[EventProgram] = []
        self._fetched_at = 0.0
        self._by_channel: dict[str, str] = {}
        self._unmatched: dict[str, float] = {}

    def get(self, force: bool = False) -> list[EventProgram]:
        with self._lock:
            fresh = time.monotonic() - self._fetched_at < CACHE_TTL_SECONDS
            if self._programs and fresh and not force:
                return self._programs
            try:
                self._programs = list_event_programs()
                self._fetched_at = time.monotonic()
            except MarketoError:
                # Serve a stale list rather than nothing — a rep mid-capture
                # shouldn't be blocked by a transient Marketo failure.
                pass
            return self._programs

    def for_channel(self, channel_name: str) -> EventProgram | None:
        if not channel_name:
            return None
        with self._lock:
            remembered = self._by_channel.get(channel_name)
            if remembered:
                program = self.by_name(remembered)
                if program is not None:
                    return program
                self._by_channel.pop(channel_name, None)  # renamed or moved

            gave_up_at = self._unmatched.get(channel_name)
            if gave_up_at and time.monotonic() - gave_up_at < UNMATCHED_TTL_SECONDS:
                return None

            program = find_by_channel(channel_name, self.get())
            if program is None and self._programs:
                # It may have been created after our list was cached.
                program = find_by_channel(channel_name, self.get(force=True))
            if program is None:
                self._unmatched[channel_name] = time.monotonic()
                return None

            self._by_channel[channel_name] = program.name
            self._unmatched.pop(channel_name, None)
            return program

    def by_name(self, name: str) -> EventProgram | None:
        return next((program for program in self.get() if program.name == name), None)

    def label_for(self, name: str) -> str | None:
        program = self.by_name(name)
        return program.label if program else None


cache = ProgramCache()


# ------------------------------------------------------------------- transport


class _TokenCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token = ''
        self._expires_at = 0.0

    def get(self, force_refresh: bool = False) -> str:
        with self._lock:
            if not force_refresh and self._token and time.monotonic() < self._expires_at:
                return self._token
            try:
                response = requests.get(
                    f'{MARKETO_BASE_URL}/identity/oauth/token',
                    params={
                        'grant_type': 'client_credentials',
                        'client_id': MARKETO_CLIENT_ID,
                        'client_secret': MARKETO_CLIENT_SECRET,
                    },
                    timeout=30,
                )
            except requests.RequestException as error:
                # Never interpolate the exception — a requests error carries the
                # full URL, and these query params are the credentials.
                raise MarketoError(
                    f'Could not reach Marketo to authenticate ({type(error).__name__})'
                ) from None
            if response.status_code >= 400:
                raise MarketoError(f'Marketo auth failed ({response.status_code})')
            payload = response.json()
            self._token = payload['access_token']
            # Refresh a minute early so a request never races the expiry.
            self._expires_at = time.monotonic() + max(int(payload.get('expires_in', 3600)) - 60, 60)
            return self._token


_tokens = _TokenCache()


def _request(method: str, path: str, **kwargs) -> dict[str, Any]:
    """Call Marketo, retrying once on an expired access token."""
    # Popped once, outside the loop: popping inside would leave the retry with
    # no caller headers at all.
    caller_headers = kwargs.pop('headers', {})
    for attempt in (0, 1):
        token = _tokens.get(force_refresh=attempt == 1)
        headers = {'Authorization': f'Bearer {token}', **caller_headers}
        try:
            response = requests.request(
                method, f'{MARKETO_BASE_URL}{path}', headers=headers, timeout=60, **kwargs
            )
        except requests.RequestException as error:
            # Conference wifi drops; wrap so callers handle one exception type.
            raise MarketoError(
                f'Could not reach Marketo for {path} ({type(error).__name__})'
            ) from None
        if response.status_code >= 400:
            raise MarketoError(f'{path} failed ({response.status_code}): {response.text[:300]}')

        payload = response.json()
        errors = payload.get('errors') or []
        if errors:
            codes = {str(error.get('code')) for error in errors}
            if codes & _TOKEN_ERROR_CODES and attempt == 0:
                continue
            raise MarketoError(f'{path} returned errors: {errors}')
        if not payload.get('success', True):
            raise MarketoError(f'{path} was unsuccessful: {payload}')
        return payload

    raise MarketoError(f'{path} failed after a token refresh')


def _folder_id(name: str) -> int | None:
    payload = _request(
        'GET', '/rest/asset/v1/folder/byName.json', params={'name': name, 'type': 'Folder'}
    )
    results = payload.get('result') or []
    return int(results[0]['id']) if results else None


def _walk_folders(root_id: int, depth: int = 0) -> list[int]:
    """The folder and all its descendants. Depth-capped as a loop guard."""
    found = [root_id]
    if depth >= 4:
        return found
    for child_id in _child_folder_ids(root_id):
        found.extend(_walk_folders(child_id, depth + 1))
    return found


def _child_folder_ids(folder_id: int) -> list[int]:
    """Ids of a folder's subfolders. The content endpoint returns no names."""
    payload = _request(
        'GET', f'/rest/asset/v1/folder/{folder_id}/content.json', params={'maxReturn': 200}
    )
    return [
        int(item['id'])
        for item in payload.get('result') or []
        if str(item.get('type', '')).lower() == 'folder' and item.get('id') is not None
    ]


def _programs_in_folder(folder_id: int) -> list[EventProgram]:
    payload = _request(
        'GET',
        '/rest/asset/v1/programs.json',
        params={'filterType': 'folderId', 'filterValues': folder_id, 'maxReturn': 200},
    )
    programs = []
    for record in payload.get('result') or []:
        name = record.get('name') or ''
        parsed = parse_program_name(name)
        if parsed is None:
            continue
        programs.append(
            EventProgram(
                id=int(record['id']),
                name=name,
                event_name=parsed[0],
                activation=parsed[1],
                created_at=record.get('createdAt') or '',
            )
        )
    return programs
