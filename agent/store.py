"""Lead state, stored in the event's Google Sheet tab. There is no database.

A thread holds one row per prospect: a rep who photographs three badges in one
message is capturing three people, and each needs its own row that tools can
reach. So a lead's handle is `channel_id|thread_ts|slug|program_name`, where the
slug comes from the person's name — self-describing, because it names the tab as
well as the row.

Rows written before the slug existed carry a bare `thread_ts` in the key column,
and several of them may share it (which is how two of three GLX leads became
unreachable). They are matched on the thread prefix and told apart by their Name
column, and the next write to one repairs its key — so the migration happens as
a side effect of ordinary use rather than as a separate pass.

Rows are read and written whole. That is safe without locking because the
ingress queue groups by `thread_ts`: one thread's messages are serialised
against each other, and two threads never share a row.
"""

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from agent import sheets

STATUS_PENDING = 'pending'
STATUS_PUSHED = 'pushed'
STATUS_FLAGGED = 'flagged'
STATUS_DISCARDED = 'discarded'

_SLUG_STRIP = re.compile(r'[^a-z0-9]+')


def now_pt() -> str:
    """Timestamps are read by people, so render them in Pacific."""
    return datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%b %-d, %Y, %-I:%M %p')


def slug(name: str) -> str:
    """A lead's id within its thread, from the name on the badge.

    Two prospects in one thread sharing a name is possible but has never
    happened; a collision would address the first, which is recoverable. Basing
    it on the name is what lets a row written before slugs existed be matched.
    """
    return _SLUG_STRIP.sub('-', (name or '').lower()).strip('-') or 'lead'


def make_ref(channel_id: str, thread_ts: str, program_name: str, lead_slug: str = '') -> str:
    """`|` separates: program names carry spaces and hyphens but never a pipe,
    and a slug is only `[a-z0-9-]`, so the program name can hold the remainder."""
    return f'{channel_id}|{thread_ts}|{lead_slug}|{program_name}'


def parse_ref(ref: str) -> tuple[str, str, str, str]:
    """(channel_id, thread_ts, slug, program_name). Missing parts come back empty."""
    parts = (ref or '').split('|', 3)
    parts += [''] * (4 - len(parts))
    return parts[0], parts[1], parts[2], parts[3]


def row_key(thread_ts: str, lead_slug: str) -> str:
    return f'{thread_ts}{sheets.KEY_SEPARATOR}{lead_slug}' if lead_slug else thread_ts


def create_lead(
    program_name: str,
    channel_id: str,
    thread_ts: str,
    slack_user_id: str,
    fields: dict[str, Any],
) -> str:
    """Write the lead's row and return its ref."""
    lead_slug = slug(fields.get('name') or '')
    lead = {
        'program_name': program_name,
        'captured_at': now_pt(),
        'thread_ts': thread_ts,
        'slug': lead_slug,
        'channel_id': channel_id,
        'slack_user_id': slack_user_id,
        'status': STATUS_PENDING,
        'fields': fields,
        'sales_notes': '',
        'mops_notes': '',
    }
    sheets.append_row(program_name, _to_row(lead))
    return make_ref(channel_id, thread_ts, program_name, lead_slug)


def leads_for_thread(channel_id: str, thread_ts: str, program_name: str) -> list[dict[str, Any]]:
    """Every lead captured in one thread, in sheet order.

    One read serves the whole turn, which is what makes a three-badge capture
    cost the same in Sheets calls as a one-badge capture.
    """
    if not (thread_ts and program_name):
        return []
    return [
        _to_lead(program_name, channel_id, number, row)
        for number, row in sheets.rows_for_thread(program_name, thread_ts)
    ]


def get_lead(ref: str) -> dict[str, Any] | None:
    """The lead a ref names. An empty slug matches a thread's only lead."""
    channel_id, thread_ts, lead_slug, program_name = parse_ref(ref)
    leads = leads_for_thread(channel_id, thread_ts, program_name)
    if not leads:
        return None
    if not lead_slug:
        return leads[0] if len(leads) == 1 else None
    return next((lead for lead in leads if lead['slug'] == lead_slug), None)


def update_lead(ref: str, **changes: Any) -> dict[str, Any] | None:
    """Read-modify-write the lead's row. Returns the updated lead, or None if
    there is no row — callers treat that as "nothing to update", never a crash."""
    lead = get_lead(ref)
    if lead is None:
        return None
    return write_lead(lead, **changes)


def write_lead(lead: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """Write against a lead already read this turn, skipping the re-read.

    Used when several leads change in one pass — re-reading per lead would be a
    Sheets round trip each for rows already in hand.
    """
    if 'fields' in changes:
        lead['fields'] = {**lead['fields'], **changes.pop('fields')}
    lead.update(changes)
    # A row keyed before slugs existed gets its key repaired here, which is what
    # makes it addressable from now on.
    lead['slug'] = lead.get('slug') or slug(lead['fields'].get('name') or '')
    sheets.update_row(lead['program_name'], lead['row_number'], _to_row(lead))
    return lead


def lead_from_row(
    program_name: str, channel_id: str, row_number: int, row: list[str]
) -> dict[str, Any]:
    """Read a lead out of raw cells, for callers listing a whole tab."""
    return _to_lead(program_name, channel_id, row_number, row)


def _to_lead(program_name: str, channel_id: str, row_number: int, row: list[str]) -> dict[str, Any]:
    cells = list(row) + [''] * (len(sheets.HEADERS) - len(row))
    state = sheets.decode_state(cells[sheets.STATE_INDEX])
    thread_ts, _, key_slug = cells[sheets.KEY_INDEX].partition(sheets.KEY_SEPARATOR)
    name = cells[1] or None
    # A legacy row has no slug in its key, so derive one from the name it holds.
    lead_slug = key_slug or slug(name or '')
    owning_channel = state.get('channel_id', channel_id)
    return {
        'program_name': program_name,
        'row_number': row_number,
        'thread_ts': thread_ts,
        'slug': lead_slug,
        'captured_at': cells[0],
        'fields': {
            'name': name,
            'company': cells[2] or None,
            'email': cells[3] or None,
            'job_title': cells[4] or None,
            # Phone has no column of its own but is still pushed to Marketo.
            'phone': state.get('phone'),
        },
        'sales_notes': cells[5],
        'mops_notes': cells[6],
        'marketo_url': cells[7],
        'status': state.get('status', STATUS_PENDING),
        'card_ts': state.get('card_ts'),
        'channel_id': owning_channel,
        'slack_user_id': state.get('slack_user_id', ''),
        'marketo_lead_id': state.get('marketo_lead_id'),
        'ref': make_ref(owning_channel, thread_ts, program_name, lead_slug),
    }


def _to_row(lead: dict[str, Any]) -> list[str]:
    fields = lead.get('fields', {})
    state = {
        'status': lead.get('status', STATUS_PENDING),
        'card_ts': lead.get('card_ts'),
        'channel_id': lead.get('channel_id', ''),
        'slack_user_id': lead.get('slack_user_id', ''),
        'phone': fields.get('phone'),
        'marketo_lead_id': lead.get('marketo_lead_id'),
    }
    return [
        lead.get('captured_at') or now_pt(),
        fields.get('name') or '',
        fields.get('company') or '',
        fields.get('email') or '',
        fields.get('job_title') or '',
        lead.get('sales_notes') or '',
        lead.get('mops_notes') or '',
        lead.get('marketo_url') or '',
        row_key(lead.get('thread_ts') or '', lead.get('slug') or ''),
        sheets.encode_state({k: v for k, v in state.items() if v not in (None, '')}),
    ]
