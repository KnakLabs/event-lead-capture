"""One Slack thread's leads: the handles, the cards, and the agent's tools.

A thread holds one row per prospect. Three badges photographed in one message are
three people, so every tool takes an optional `who` and defaults to all of them —
`find_emails()` is one Clay run for the whole group, `push_to_marketo()` pushes
everyone who is ready.

The agent acts rather than asks (`system_prompt.py`). A rep on an expo floor
cannot answer a confirmation question, so the row is created, the email found and
the lead pushed on the turn the badge arrives, and the rep is told what happened.
Corrections come afterwards by tagging the agent, which is why a push is no
longer terminal: `push_to_marketo` re-pushes an already-pushed lead against its
stored Marketo id so a fix reaches Marketo instead of only the sheet.

Two rules stay enforced here rather than in the prompt, because both have to
hold every time and the turn ingests untrusted badge photos and voice
transcripts: a lead reaches Marketo only with an email, and `start_lead` refuses
a name the thread already holds — which also makes re-reading the same badge on
a later turn harmless.
"""

import re
from typing import Any

from agent import badge_text, clay, marketo, sheets, store

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s.]+\.[^@\s]+$')

# Everything an operation can raise that the rep can act on. Returned to the
# model as an `Error:` string rather than propagating: a tool that raises aborts
# the turn, losing the reply entirely while the queue retries the message and
# the rep is answered twice.
_RECOVERABLE = (sheets.SheetsError, clay.ClayError, marketo.MarketoError)

# `who` values meaning "every lead in this thread".
_ALL = {'all', 'everyone', 'both', '*'}

# A tab can hold a whole event; a listing is context for the model, not a report.
MAX_LISTED_LEADS = 60

# Which recasing each column gets. An email is never recased — it is a literal.
_RECASE = {
    'name': badge_text.recase_name,
    'company': badge_text.recase_company,
    'job_title': badge_text.recase_title,
}


class Capture:
    """One Slack thread's leads. `program` is the only mutable identity."""

    def __init__(
        self,
        channel_id: str,
        thread_ts: str,
        slack_user_id: str,
        program: marketo.EventProgram | None,
    ) -> None:
        self.channel_id = channel_id
        self.thread_ts = thread_ts
        self.slack_user_id = slack_user_id
        self.program = program

    @property
    def ref(self) -> str:
        if self.program is None:
            return ''
        return store.make_ref(self.channel_id, self.thread_ts, self.program.name)

    def leads(self) -> list[dict[str, Any]]:
        """Re-read each time — an operation may have just created or changed one."""
        if self.program is None:
            return []
        return store.leads_for_thread(self.channel_id, self.thread_ts, self.program.name)

    def resolve(
        self,
        who: str,
        leads: list[dict[str, Any]] | None = None,
        *,
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        """The leads `who` names. Empty or "all" means every lead in the thread.

        Two modes, because the two callers want opposite things. Looking a lead
        up is loose — the model is working from a badge it read, so it may pass a
        full name, a first name or the slug. Deciding whether a person is
        *already here* must be exact: the loose rule matches on shared first
        name, so it would take "Priya Nguyen" for the "Priya Raman" already in
        the thread and refuse to file her at all.
        """
        pool = self.leads() if leads is None else leads
        wanted = (who or '').strip().lower()
        if not wanted or wanted in _ALL:
            return [] if strict else pool

        target = store.slug(wanted)
        exact = [
            lead
            for lead in pool
            if lead['slug'] == target
            or (lead['fields'].get('name') or '').strip().lower() == wanted
        ]
        if exact or strict:
            return exact
        return [
            lead
            for lead in pool
            if wanted in (lead['fields'].get('name') or '').lower()
            or target in lead['slug']
            or lead['slug'].startswith(target.split('-')[0])
        ]


def _names(leads: list[dict[str, Any]]) -> str:
    return ', '.join((lead['fields'].get('name') or lead['slug']) for lead in leads) or 'none'


def describe(capture: Capture, channel_name: str, notices: list[str]) -> str:
    """The situation the model reasons over, as a prompt section."""
    parts = [f'## Where you are\nSlack channel: #{channel_name or "(name unavailable)"}']
    if capture.program is not None:
        parts.append(f'Marketo event program: {capture.program.name}')
    else:
        parts.append(
            "Marketo event program: **not resolved** — this channel's name does not match "
            'one. Use find_event_program to look for it, then start_lead with the exact name.'
        )

    leads = capture.leads()
    if not leads:
        parts.append(
            '\n## No leads in this thread yet\nIf a badge photo is attached — including one '
            'posted earlier in the thread — read it and call start_lead, once per person. If '
            'there is no badge, they are just talking to you.'
        )
    else:
        lines = []
        for lead in leads:
            fields = lead['fields']
            lines.append(
                f'- **{fields.get("name") or lead["slug"]}** (who: "{fields.get("name") or lead["slug"]}")\n'
                f'  - status: {lead.get("status", "pending")}\n'
                f'  - Company: {fields.get("company") or ""}\n'
                f'  - Job Title: {fields.get("job_title") or ""}\n'
                f'  - Email: {fields.get("email") or "(none yet)"}\n'
                f'  - Marketo lead: {lead.get("marketo_lead_id") or "(not pushed)"}\n'
                f'  - Sales Notes: {lead.get("sales_notes") or "(none)"}'
            )
        parts.append(
            f'\n## The {len(leads)} lead(s) in this thread\n'
            'Pass the name shown as `who` to act on one, or omit `who` to act on all of them.\n'
            + '\n'.join(lines)
        )

    if notices:
        parts.append('\n## Worth knowing\n' + '\n'.join(f'- {n}' for n in notices))
    return '\n'.join(parts)


# ---------------------------------------------------------------------- card


def _escape(value: Any) -> str:
    """Everything on the card is read off a badge photo or a voice note, so
    `<!channel>` printed on a lanyard would otherwise notify the whole channel."""
    text = str(value).strip() if value else ''
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _or_dash(value: Any) -> str:
    return _escape(value) or '—'


def render_cards(capture: Capture, slack) -> list[tuple[str, str]]:
    """Post or update one card per lead. Returns (who, why) for any that failed.

    One card each rather than a combined one: a card is edited in place as its
    lead changes, and three prospects whose rows move independently cannot share
    a message without rewriting all three every time any one of them changes.

    Per lead, not per turn: the caller has one try/except around the whole call,
    so without this a rate limit on the second lead's card would leave the third
    prospect with no card at all — invisible to the rep on the turn their row was
    written and pushed. `card_ts` is only stored on success, so a missed card is
    posted on the next tag.

    The cause is returned rather than logged here, and rather than dropped: this
    module does no logging (the caller owns it, as with `transcribe`), and a bare
    "a card failed" cannot tell a rate limit from a malformed block.

    A list rather than a dict keyed by name, because a name is not unique: a
    correction can leave two rows in one thread displaying the same one, and
    keying on it would drop one of two failures on the turn both cards fail.
    """
    failed: list[tuple[str, str]] = []
    for lead in capture.leads():
        try:
            _render_one(lead, capture, slack)
        except Exception as error:  # noqa: BLE001 — one bad card must not hide the others
            failed.append((_who_of(lead), repr(error)))
    return failed


def _render_one(lead: dict[str, Any], capture: Capture, slack) -> None:
    fields = lead['fields']
    label = marketo.cache.label_for(lead['program_name']) or lead['program_name']
    blocks: list[dict[str, Any]] = [
        {
            'type': 'section',
            'fields': [
                {'type': 'mrkdwn', 'text': f'*Name*\n{_or_dash(fields.get("name"))}'},
                {'type': 'mrkdwn', 'text': f'*Company*\n{_or_dash(fields.get("company"))}'},
                {'type': 'mrkdwn', 'text': f'*Job title*\n{_or_dash(fields.get("job_title"))}'},
                {'type': 'mrkdwn', 'text': f'*Email*\n{_or_dash(fields.get("email"))}'},
            ],
        },
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': f'*Event:* {_escape(label)}'}},
    ]
    if lead.get('sales_notes'):
        blocks.append(
            {
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': f'*Sales notes*\n{_escape(lead["sales_notes"])}',
                },
            }
        )
    if lead.get('mops_notes'):
        blocks.append(
            {
                'type': 'section',
                'text': {'type': 'mrkdwn', 'text': f'*MOPs notes*\n{_escape(lead["mops_notes"])}'},
            }
        )
    status = lead.get('status', store.STATUS_PENDING).capitalize()
    marketo_url = lead.get('marketo_url')
    footer = f'<{marketo_url}|View in Marketo> · {status}' if marketo_url else status
    blocks.append(
        {
            'type': 'context',
            'elements': [
                {
                    'type': 'mrkdwn',
                    'text': f'{footer} · Captured {_escape(lead.get("captured_at"))}',
                }
            ],
        }
    )
    summary = f'{fields.get("name") or "Lead"} — {fields.get("company") or ""}'.strip(' —')

    card_ts = lead.get('card_ts')
    if card_ts:
        slack.chat_update(channel=capture.channel_id, ts=card_ts, text=summary, blocks=blocks)
        return
    posted = slack.chat_postMessage(
        channel=capture.channel_id, thread_ts=capture.thread_ts, text=summary, blocks=blocks
    )
    store.write_lead(lead, card_ts=posted['ts'])


# --------------------------------------------------------------------- tools


def build_tools(capture: Capture) -> list:
    """The tool surface for this thread. Closures over one `Capture`, so the
    model cannot address a lead outside the thread it was tagged in."""

    def guarded(operation) -> str:
        try:
            return operation()
        except _RECOVERABLE as error:
            return f'Error: {error}. Tell the salesperson what failed; they can ask you to retry.'

    def pick(who: str, *, verb: str) -> list[dict[str, Any]] | str:
        """Resolve `who`, or an Error: string naming what is actually here."""
        leads = capture.leads()
        if not leads:
            return f'Error: there are no leads in this thread yet, so there is nothing to {verb}.'
        matched = capture.resolve(who, leads)
        if not matched:
            return (
                f'Error: no lead here matches {who!r}. This thread has: {_names(leads)}. '
                'Pass one of those as `who`, or omit `who` to act on all of them.'
            )
        named_one = bool((who or '').strip()) and (who or '').strip().lower() not in _ALL
        if named_one and len(matched) > 1:
            # A partial name that fits two people is not a licence to act on
            # both — that would push a prospect the rep never singled out.
            return (
                f'Error: {who!r} matches {len(matched)} leads here ({_names(matched)}). '
                'Pass the full name of the one you mean.'
            )
        return matched

    def find_event_program(query: str = '') -> str:
        """Search Marketo's event programs. Use when the context says the channel did not resolve to one: pass whatever you can infer from the channel name and you get back the closest matches. An empty query lists everything.

        Args:
        - query: event name or fragment, e.g. "adobe summit"
        """

        def run() -> str:
            candidates = marketo.cache.get()
            if not candidates:
                return 'Error: no event programs found in Marketo at all.'
            words = re.sub(r'[^a-z0-9]+', ' ', (query or '').lower()).split()
            hits = [
                p
                for p in candidates
                if all(w in re.sub(r'[^a-z0-9]+', ' ', p.name.lower()) for w in words)
            ]
            listing = '\n'.join(f'- {p.name}' for p in (hits or candidates)[:25])
            if not hits:
                return f'Nothing matched {query!r}. Every event program:\n{listing}'
            return f'{len(hits)} match(es):\n{listing}'

        return guarded(run)

    def start_lead(
        name: str, company: str, job_title: str = '', phone: str = '', program_name: str = ''
    ) -> str:
        """Create the row for one prospect whose badge you have just read. Call it once per badge — three badges in a message means three calls. Pass exactly what is printed; never guess or expand a name or company you cannot actually read. If a photo is too blurred or dark to read the name and company, do NOT call this for it: say so and ask for a clearer photo of that one.

        Args:
        - name: full name as printed on the badge
        - company: the attendee's employer — NOT the conference, and NOT a sponsor whose logo is printed on the badge
        - job_title: job title, if printed
        - phone: phone number, only if printed
        - program_name: only when the channel did not resolve to a program. Pass an exact name from find_event_program.
        """

        def run() -> str:
            if program_name.strip():
                chosen = marketo.cache.by_name(program_name.strip())
                if chosen is None:
                    return (
                        f'Error: {program_name!r} is not an event program. Call '
                        'find_event_program and pass a name exactly as listed.'
                    )
                capture.program = chosen
            if capture.program is None:
                return (
                    'Error: no Marketo event program for this thread yet. Call '
                    'find_event_program, then call start_lead again with program_name set.'
                )
            # Clay needs both, and they are the anti-invention gate: an
            # unreadable badge leaves nothing legitimate to pass.
            if not (name.strip() and company.strip()):
                return (
                    'Error: both name and company are required, read off the badge. If you '
                    'cannot read them, do not guess — say the photo is unreadable and ask '
                    'for another.'
                )
            # Idempotent by name. A badge posted earlier in the thread is shown to
            # you again on later turns, so without this the same person would be
            # filed twice; it is also the guard that stopped working when three
            # badges arrived at once. Strict: a shared first name is two people,
            # not one, and refusing the second would strand a real lead.
            existing = capture.resolve(name, strict=True)
            if existing:
                return (
                    f'Error: {_names(existing)} is already in this thread. Use update_lead '
                    f'with who="{name.strip()}" to change that row.'
                )
            store.create_lead(
                capture.program.name,
                capture.channel_id,
                capture.thread_ts,
                capture.slack_user_id,
                {
                    # Recased, not respelled: a badge shouts so it can be read
                    # across a booth, and `firstName: ANNALIIA` would greet her as
                    # "Hi ANNALIIA" in every email that uses the token.
                    'name': badge_text.recase_name(name),
                    'company': badge_text.recase_company(company),
                    'job_title': badge_text.recase_title(job_title) or None,
                    'phone': phone.strip() or None,
                    'email': None,
                },
            )
            return (
                f'Row created for {name.strip()} in {capture.program.name}. When every badge '
                'in this message has a row, call find_emails once for all of them.'
            )

        return guarded(run)

    def update_lead(
        who: str = '',
        name: str = '',
        company: str = '',
        job_title: str = '',
        email: str = '',
        sales_notes: str = '',
    ) -> str:
        """Set columns on one lead's row, or on every lead in the thread. Pass only what should change; an empty value is ignored and cannot clear a column. Sales Notes is replaced wholesale, so pass the complete text you want it to contain.

        When one note covers several people photographed together, omit `who` — the same notes are written to all of them, which is the normal case for a group of badges shared with a single comment.

        Fill a column that is currently empty from anything the thread states — a fact told to you by voice or text belongs in its column, not only in the notes. Overwrite a value the badge already gave you only when the thread is clearly better: a corrected spelling, or someone who has changed jobs.

        If the lead is already in Marketo, follow this with push_to_marketo for the same `who` so the change reaches Marketo too, not just the sheet.

        Args:
        - who: the person's name, as shown in the thread's lead list. Omit to apply to every lead in the thread.
        - name: full name — only to correct a misread badge. Never use this to point at a different person; that is what `who` is for.
        - company: employer
        - job_title: job title — set it whenever the thread names their role and the column is blank
        - email: only if a real address was stated in the thread. Never invent one — find_emails looks it up.
        - sales_notes: the complete Sales Notes about the prospect: what they do, what problem they have, what they asked for, what happens next
        """

        def run() -> str:
            targets = pick(who, verb='update')
            if isinstance(targets, str):
                return targets

            field_changes: dict[str, Any] = {}
            for column, value in (
                ('name', name),
                ('company', company),
                ('job_title', job_title),
                ('email', email),
            ):
                cleaned = (value or '').strip()
                if not cleaned:
                    # The model routinely passes '' for fields it has no opinion
                    # on; treating that as "clear it" would wipe a good badge read.
                    continue
                if column == 'email' and not _EMAIL_RE.match(cleaned):
                    return (
                        f'Error: {cleaned!r} is not an email address. Leave email empty unless '
                        'a real one was stated in the thread.'
                    )
                # Same recasing as start_lead — a correction quoted back off the
                # badge arrives in capitals too.
                field_changes[column] = _RECASE.get(column, lambda text: text)(cleaned)

            if len(targets) > 1 and field_changes:
                # Notes are shared across a group; identity is not. Writing one
                # name or email onto three rows would merge three people.
                return (
                    f'Error: {sorted(field_changes)} would be written to all {len(targets)} '
                    f'leads ({_names(targets)}). Only sales_notes can apply to everyone — '
                    'pass `who` to change a person-specific column.'
                )

            changed = sorted(field_changes)
            changes: dict[str, Any] = {}
            if sales_notes.strip():
                changes['sales_notes'] = sales_notes.strip()
                changed.append('sales_notes')
            if field_changes:
                changes['fields'] = field_changes
            if not changes:
                return 'Nothing to change.'
            for lead in targets:
                store.write_lead(lead, **changes)
            return f'Updated {", ".join(changed)} on {_names(targets)}.'

        return guarded(run)

    def find_emails(who: str = '') -> str:
        """Look up work emails for the leads in this thread — every one that still needs one, in a single lookup. Call it once after the rows exist rather than once per person. Nothing can be pushed to Marketo without an email; anyone enrichment cannot place is flagged for Marketing Ops.

        Args:
        - who: a person's name to look up just that one. Omit to look up everyone in the thread who still needs an email.
        """

        def run() -> str:
            targets = pick(who, verb='enrich')
            if isinstance(targets, str):
                return targets

            # Only a pending lead is worth a Clay credit. A flagged one already
            # had its run come back empty — that is terminal, and re-running it
            # every time the rep tags the agent burns a credit per nudge and
            # appends the same MOPs note again. Enforced here rather than in the
            # prompt because it costs real money each time it leaks.
            outstanding = [
                lead
                for lead in targets
                if not (lead['fields'].get('email') or '').strip()
                and lead.get('status') == store.STATUS_PENDING
            ]
            settled = [
                lead
                for lead in targets
                if lead.get('status') == store.STATUS_FLAGGED
                and not (lead['fields'].get('email') or '').strip()
            ]
            if not outstanding:
                have = ', '.join(
                    f'{lead["fields"].get("name")} ({lead["fields"].get("email")})'
                    for lead in targets
                    if lead['fields'].get('email')
                )
                parts = []
                if have:
                    parts.append(f'Already has an email: {have}.')
                if settled:
                    parts.append(
                        f'{_names(settled)} was already looked up and could not be placed — '
                        'flagged for Marketing Ops, not pushable, and not worth retrying.'
                    )
                return ' '.join(parts) or f'Nothing to enrich for {_names(targets)}.'

            missing = [
                lead
                for lead in outstanding
                if not (lead['fields'].get('name') and lead['fields'].get('company'))
            ]
            if missing:
                return (
                    f'Error: {_names(missing)} needs both a name and a company before '
                    'enrichment can run. Read them off the badge or ask.'
                )

            # One Clay run for the whole group. A ClayError propagates to
            # `guarded`, which reports it as retryable — only a lead Clay
            # actually looked up and could not place gets flagged, because an
            # unreachable Clay would otherwise burn every lead at an event.
            results = clay.find_work_emails(
                [
                    (lead['slug'], lead['fields']['name'], lead['fields']['company'])
                    for lead in outstanding
                ]
            )

            found, flagged = [], []
            for lead in outstanding:
                email = results.get(lead['slug'])
                if email:
                    store.write_lead(lead, fields={'email': email})
                    found.append(f'{lead["fields"]["name"]} → {email}')
                else:
                    store.write_lead(
                        lead,
                        status=store.STATUS_FLAGGED,
                        mops_notes=_add_note(
                            lead.get('mops_notes'),
                            'No email — not pushed to Marketo. Enrichment returned nobody.',
                        ),
                    )
                    flagged.append(lead['fields']['name'])

            report = []
            if found:
                report.append(f'Found: {"; ".join(found)}.')
            if flagged:
                report.append(
                    f'No email for {", ".join(flagged)} — flagged for Marketing Ops and not '
                    'pushable. Say so plainly and do not retry them.'
                )
            if settled:
                report.append(
                    f'{_names(settled)} was already flagged on an earlier turn and was not '
                    'looked up again.'
                )
            if found:
                report.append('Now call push_to_marketo for the ones with an email.')
            return ' '.join(report)

        return guarded(run)

    def push_to_marketo(who: str = '') -> str:
        """Send the leads in this thread to Marketo against this event's program, with status Attended. Do this as soon as a lead has an email — do not ask permission first. Also use it after update_lead on a lead that is already in Marketo, to push the correction to the existing record.

        Args:
        - who: a person's name to push just that one. Omit to push everyone in the thread who is ready.
        """

        def run() -> str:
            targets = pick(who, verb='push')
            if isinstance(targets, str):
                return targets

            target_program = marketo.cache.by_name(targets[0]['program_name'])
            if target_program is None:
                return "Error: this thread's Marketo program can't be found any more."

            # A named `who` is the rep asking for this person specifically, which
            # is the deliberate re-push path after a correction. Without a name
            # the tool is being called as the last step of a capture, and the
            # prompt has it do that every turn — so an already-pushed lead must
            # be left alone or a thread of three re-pushes all three on every
            # nudge, rewriting program status on records nothing changed.
            named_one = bool((who or '').strip()) and (who or '').strip().lower() not in _ALL

            done, skipped, sheet_failed, failed = [], [], [], []
            for lead in targets:
                try:
                    outcome = _sync_one(lead, target_program, explicit=named_one)
                except _RECOVERABLE as error:
                    # Per lead, not per call: leads already pushed in this loop
                    # are in Marketo, and losing their ids to one failure would
                    # leave the rep told only about the failure.
                    failed.append(f'{_who_of(lead)} failed ({error})')
                    continue
                if outcome['kind'] == 'skipped':
                    skipped.append(outcome['text'])
                elif outcome['kind'] == 'sheet_failed':
                    sheet_failed.append(outcome['text'])
                else:
                    done.append(outcome['text'])

            if not (done or sheet_failed or failed) and len(skipped) == 1:
                # One lead, nothing done: the reason IS the answer, so give the
                # model the raw reason rather than burying it in a summary.
                return f'Error: {skipped[0]}'

            report = []
            if done:
                report.append(f'Done: {"; ".join(done)}.')
            if sheet_failed:
                report.append(
                    f'In Marketo but the sheet row could not be updated: {"; ".join(sheet_failed)}. '
                    'Tell them the lead IS in Marketo, give the id, and say the row needs fixing '
                    'by hand. Do not push those again.'
                )
            if failed:
                report.append(
                    f'Not in Marketo — {"; ".join(failed)}. Name them and say they can ask you '
                    'to retry those.'
                )
            if skipped:
                report.append(f'Not pushed: {"; ".join(skipped)}.')
            report.append('Tell them what happened, including the Marketo lead ids.')
            return ' '.join(report)

        return guarded(run)

    def discard_lead(who: str = '') -> str:
        """Mark a lead discarded, when the salesperson says to drop it. Nothing is sent to Marketo and the row stays in the sheet.

        Args:
        - who: the person's name. Omit only when the thread holds a single lead.
        """

        def run() -> str:
            targets = pick(who, verb='discard')
            if isinstance(targets, str):
                return targets
            if len(targets) > 1 and not (who or '').strip():
                return (
                    f'Error: this thread has {len(targets)} leads ({_names(targets)}). Pass '
                    '`who` — discarding is not something to do to all of them by default.'
                )
            for lead in targets:
                store.write_lead(lead, status=store.STATUS_DISCARDED)
            return f'Discarded {_names(targets)}. Nothing was sent to Marketo.'

        return guarded(run)

    def list_event_leads() -> str:
        """Read this event's tab in the tracking sheet and list every lead captured for it, by anyone, in any thread. Use it when the salesperson asks about the sheet, or about leads outside this thread. You can only change the leads in this thread — say so if they ask you to act on another one."""

        def run() -> str:
            if capture.program is None:
                return (
                    'Error: this thread has no Marketo event program, so there is no tab to read.'
                )
            rows = sheets.all_rows(capture.program.name)
            if not rows:
                return f'The {capture.program.name} tab has no leads yet.'
            lines = []
            for number, row in rows[:MAX_LISTED_LEADS]:
                lead = store.lead_from_row(capture.program.name, capture.channel_id, number, row)
                fields = lead['fields']
                here = ' (this thread)' if lead['thread_ts'] == capture.thread_ts else ''
                lines.append(
                    f'- {fields.get("name") or "?"} · {fields.get("company") or "?"} · '
                    f'{fields.get("email") or "no email"} · {lead.get("status")}{here}'
                )
            more = (
                f'\n… and {len(rows) - MAX_LISTED_LEADS} more'
                if len(rows) > MAX_LISTED_LEADS
                else ''
            )
            return f'{len(rows)} lead(s) in {capture.program.name}:\n' + '\n'.join(lines) + more

        return guarded(run)

    return [
        find_event_program,
        start_lead,
        update_lead,
        find_emails,
        push_to_marketo,
        discard_lead,
        list_event_leads,
    ]


def _who_of(lead: dict[str, Any]) -> str:
    return lead['fields'].get('name') or lead['slug']


def _sync_one(
    lead: dict[str, Any], program: marketo.EventProgram, *, explicit: bool = False
) -> dict[str, str]:
    """Create or re-push one lead. Returns what to tell the model about it.

    `explicit` means the rep named this person, which is the only way an
    already-pushed lead is sent again — that is the correction path.
    """
    name = _who_of(lead)
    email = (lead['fields'].get('email') or '').strip()
    if lead.get('status') == store.STATUS_DISCARDED:
        return {'kind': 'skipped', 'text': f'{name} was discarded'}
    if lead.get('status') == store.STATUS_PUSHED and not explicit:
        return {
            'kind': 'skipped',
            'text': f'{name} is already in Marketo as lead {lead.get("marketo_lead_id") or "?"}',
        }
    if not email:
        return {
            'kind': 'skipped',
            'text': f'{name} has no email — call find_emails, a lead is never created without one',
        }

    existing_id = (lead.get('marketo_lead_id') or '').strip()
    fields = lead['fields']
    result = marketo.push_lead(
        program=program,
        email=email,
        name=fields.get('name') or '',
        company=fields.get('company') or '',
        job_title=fields.get('job_title') or '',
        phone=fields.get('phone') or '',
        notes=lead.get('sales_notes') or '',
        lead_id=existing_id or None,
    )
    lead_id = result.get('id') or existing_id
    changes: dict[str, Any] = {
        'status': store.STATUS_PUSHED,
        'marketo_lead_id': str(lead_id or ''),
        'marketo_url': marketo.lead_url(lead_id),
    }
    duplicate = result.get('resolved_duplicate')
    if duplicate:
        # The push succeeded, so this isn't a flag — but the duplicates are
        # still there, and MOPs Notes is where Marketing Ops looks.
        changes['mops_notes'] = _add_note(
            lead.get('mops_notes'),
            f'{duplicate["considered"]} Marketo records matched {email}; updated the '
            f'{duplicate["sfdc_type"]} record (id {duplicate["chosen_id"]}). '
            'The duplicates still need merging.',
        )
    verb = 'updated' if existing_id else 'pushed'
    detail = f'{name} {verb} as Marketo lead {lead_id}'
    if duplicate:
        detail += (
            f' (Marketo held {duplicate["considered"]} records for that email, so this went '
            f'to the {duplicate["sfdc_type"]} one — Marketing Ops noted to merge them)'
        )
    try:
        store.write_lead(lead, **changes)
    except sheets.SheetsError as error:
        # The lead is already in Marketo. This must not read as a failure the rep
        # would retry, because a retry pushes again.
        return {'kind': 'sheet_failed', 'text': f'{detail} ({error})'}
    return {'kind': 'done', 'text': detail}


def _add_note(existing: str | None, note: str) -> str:
    """Append to MOPs Notes. A row can already carry one, and overwriting it
    would drop whatever an earlier turn flagged."""
    return f'{existing.strip()} {note}' if (existing or '').strip() else note
