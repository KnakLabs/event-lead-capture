"""Several prospects in one Slack thread.

Three badges photographed together are three people. Before this, every row in a
thread shared one key and `find_row` returned the first — so two of the three GLX
leads captured on 2026-09-09 were written to the sheet and then unreachable by
every tool. These tests pin down the keying, the legacy rows that already share a
bare key, and the tool surface that addresses one lead or all of them.

The sheet is faked in memory, so no network and no credentials.
"""

import pytest

from agent import clay, marketo, sheets, store
from agent import lead as lead_mod

PROGRAM = marketo.EventProgram(
    id=1,
    name='FY2026-Q3-Knak Sponsored Event-Summit Expo-Booth',
    event_name='Summit Expo',
    activation='Booth',
)
THREAD = '1700000000.000100'
CHANNEL = 'C01EXAMPLE1'

TABS: dict[str, list[list[str]]] = {}


# --------------------------------------------------------------- the fake sheet


def _pad(values):
    return list(values) + [''] * (len(sheets.HEADERS) - len(values))


@pytest.fixture(autouse=True)
def fake_sheet(monkeypatch):
    TABS.clear()
    TABS[sheets.tab_title(PROGRAM.name)] = [list(sheets.HEADERS)]

    def append_row(program_name, values):
        TABS.setdefault(sheets.tab_title(program_name), [list(sheets.HEADERS)]).append(_pad(values))

    def update_row(program_name, number, values):
        TABS[sheets.tab_title(program_name)][number] = _pad(values)

    def rows_for_thread(program_name, thread_ts):
        prefix = f'{thread_ts}{sheets.KEY_SEPARATOR}'
        return [
            (offset, row)
            for offset, row in enumerate(TABS.get(sheets.tab_title(program_name), []))
            if offset > 0
            and len(row) > sheets.KEY_INDEX
            and (row[sheets.KEY_INDEX] == thread_ts or row[sheets.KEY_INDEX].startswith(prefix))
        ]

    def all_rows(program_name):
        return [
            (offset, row)
            for offset, row in enumerate(TABS.get(sheets.tab_title(program_name), []))
            if offset > 0
        ]

    monkeypatch.setattr(sheets, 'append_row', append_row)
    monkeypatch.setattr(sheets, 'update_row', update_row)
    monkeypatch.setattr(sheets, 'rows_for_thread', rows_for_thread)
    monkeypatch.setattr(sheets, 'all_rows', all_rows)
    monkeypatch.setattr(
        marketo.cache, 'by_name', lambda name: PROGRAM if name == PROGRAM.name else None
    )
    monkeypatch.setattr(marketo.cache, 'label_for', lambda name: 'Summit Expo — Booth')


def add(name, company, **fields):
    return store.create_lead(
        PROGRAM.name,
        CHANNEL,
        THREAD,
        'U01EXAMPLE1',
        {'name': name, 'company': company, 'email': None, **fields},
    )


def legacy_row(name, company, email=''):
    """A row as written before slugs existed: a bare thread_ts as its key."""
    TABS[sheets.tab_title(PROGRAM.name)].append(
        _pad(
            [
                'Sep 9, 2026, 9:40 AM',
                name,
                company,
                email,
                '',
                '',
                '',
                '',
                THREAD,
                sheets.encode_state({'status': 'pending', 'channel_id': CHANNEL}),
            ]
        )
    )


def capture():
    return lead_mod.Capture(CHANNEL, THREAD, 'U01EXAMPLE1', PROGRAM)


def tools():
    return {fn.__name__: fn for fn in lead_mod.build_tools(capture())}


# ------------------------------------------------------------------- 1. keying


def test_three_badges_in_one_thread_are_three_reachable_leads():
    add('Dana McAllister', 'GLX')
    add('Priya Raman', 'GLX')
    add('Iris Okonkwo', 'GLX')

    leads = store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)
    assert [lead['fields']['name'] for lead in leads] == [
        'Dana McAllister',
        'Priya Raman',
        'Iris Okonkwo',
    ]
    # The failure that started this: each must be individually addressable.
    for lead in leads:
        assert store.get_lead(lead['ref'])['fields']['name'] == lead['fields']['name']


def test_each_row_carries_its_own_key():
    add('Dana McAllister', 'GLX')
    add('Priya Raman', 'GLX')
    keys = [row[sheets.KEY_INDEX] for row in TABS[sheets.tab_title(PROGRAM.name)][1:]]
    assert keys == [f'{THREAD}#dana-mcallister', f'{THREAD}#priya-raman']


def test_a_ref_round_trips_through_a_program_name_full_of_hyphens():
    ref = store.make_ref(CHANNEL, THREAD, PROGRAM.name, 'priya-raman')
    assert store.parse_ref(ref) == (CHANNEL, THREAD, 'priya-raman', PROGRAM.name)


def test_an_empty_slug_matches_a_thread_with_one_lead_only():
    add('Dana McAllister', 'GLX')
    bare = store.make_ref(CHANNEL, THREAD, PROGRAM.name)
    assert store.get_lead(bare)['fields']['name'] == 'Dana McAllister'
    add('Priya Raman', 'GLX')
    # Ambiguous now — better to resolve to nothing than to silently pick one.
    assert store.get_lead(bare) is None


def test_slug_is_stable_and_never_empty():
    assert store.slug('Iris Okonkwo') == 'iris-okonkwo'
    assert store.slug("Siobhán O'Neill-Smith") == 'siobh-n-o-neill-smith'
    assert store.slug('') == 'lead'


# ------------------------------------------------------- 2. the orphaned rows


def test_legacy_rows_sharing_a_bare_key_are_told_apart_by_name():
    legacy_row('Dana McAllister', 'GLX', 'dmcallister@example.com')
    legacy_row('Priya Raman', 'GLX')
    legacy_row('Iris Okonkwo', 'GLX')

    leads = store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)
    assert [lead['slug'] for lead in leads] == ['dana-mcallister', 'priya-raman', 'iris-okonkwo']
    assert store.get_lead(leads[2]['ref'])['fields']['name'] == 'Iris Okonkwo'


def test_writing_to_a_legacy_row_repairs_its_key():
    legacy_row('Priya Raman', 'GLX')
    lead = store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)[0]
    assert TABS[sheets.tab_title(PROGRAM.name)][1][sheets.KEY_INDEX] == THREAD

    store.update_lead(lead['ref'], fields={'email': 'praman@example.com'})
    assert TABS[sheets.tab_title(PROGRAM.name)][1][sheets.KEY_INDEX] == f'{THREAD}#priya-raman'


# -------------------------------------------------------- 3. addressing a lead


def test_who_matches_a_full_name_a_first_name_or_a_slug():
    add('Dana McAllister', 'GLX')
    add('Priya Raman', 'GLX')
    handle = capture()
    for who in ('Priya Raman', 'priya', 'priya-raman', 'PRIYA RAMAN'):
        assert [lead['slug'] for lead in handle.resolve(who)] == ['priya-raman'], who


def test_omitting_who_means_every_lead():
    add('Dana McAllister', 'GLX')
    add('Priya Raman', 'GLX')
    handle = capture()
    assert len(handle.resolve('')) == 2
    assert len(handle.resolve('all')) == 2


def test_a_name_that_matches_nobody_lists_who_is_actually_here():
    add('Dana McAllister', 'GLX')
    result = tools()['update_lead'](who='Bruce Wayne', job_title='CEO')
    assert result.startswith('Error:')
    assert 'Dana McAllister' in result


# ---------------------------------------------------------- 4. shared notes


def test_one_note_for_three_badges_is_written_to_all_of_them():
    add('Dana McAllister', 'GLX')
    add('Priya Raman', 'GLX')
    add('Iris Okonkwo', 'GLX')
    notes = 'Uses Dynamics, not a fan of the building process.'

    result = tools()['update_lead'](sales_notes=notes)

    assert 'Iris Okonkwo' in result
    assert [
        lead['sales_notes'] for lead in store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)
    ] == [notes, notes, notes]


def test_a_person_specific_column_is_refused_for_the_whole_group():
    add('Dana McAllister', 'GLX')
    add('Priya Raman', 'GLX')

    # Writing one email onto both rows would merge two people.
    result = tools()['update_lead'](email='dana@example.com')
    assert result.startswith('Error:')
    assert [
        lead['fields']['email'] for lead in store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)
    ] == [
        None,
        None,
    ]


def test_a_person_specific_column_is_allowed_when_who_names_one():
    add('Dana McAllister', 'GLX')
    add('Priya Raman', 'GLX')
    tools()['update_lead'](who='Priya', job_title='Sr. Growth Marketing Manager')
    leads = store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)
    assert leads[0]['fields']['job_title'] is None
    assert leads[1]['fields']['job_title'] == 'Sr. Growth Marketing Manager'


# ------------------------------------------------------- 5. start_lead guard


def test_the_same_badge_read_twice_does_not_file_the_person_twice():
    add('Dana McAllister', 'GLX')
    result = tools()['start_lead'](name='Dana McAllister', company='GLX')
    assert result.startswith('Error:')
    assert len(store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)) == 1


def test_a_second_different_person_is_accepted():
    add('Dana McAllister', 'GLX')
    result = tools()['start_lead'](name='Priya Raman', company='GLX')
    assert not result.startswith('Error:')
    assert len(store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)) == 2


def test_an_unreadable_badge_is_still_refused():
    assert tools()['start_lead'](name='', company='GLX').startswith('Error:')
    assert store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name) == []


# --------------------------------------------------------- 6. batched Clay


def test_three_leads_are_enriched_in_one_clay_run(monkeypatch):
    add('Dana McAllister', 'GLX')
    add('Priya Raman', 'GLX')
    add('Iris Okonkwo', 'GLX')

    calls = []

    def fake(people):
        calls.append(people)
        return {
            'dana-mcallister': 'dmcallister@example.com',
            'priya-raman': 'praman@example.com',
            'iris-okonkwo': None,
        }

    monkeypatch.setattr(clay, 'find_work_emails', fake)
    result = tools()['find_emails']()

    assert len(calls) == 1, 'one run for the whole group, not one per person'
    assert len(calls[0]) == 3
    leads = {lead['slug']: lead for lead in store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)}
    assert leads['dana-mcallister']['fields']['email'] == 'dmcallister@example.com'
    assert leads['priya-raman']['fields']['email'] == 'praman@example.com'
    # Clay ran and found nobody: terminal for that lead, and MOPs must see why.
    assert leads['iris-okonkwo']['status'] == store.STATUS_FLAGGED
    assert 'No email' in leads['iris-okonkwo']['mops_notes']
    assert 'Iris Okonkwo' in result


def test_enrichment_skips_anyone_who_already_has_an_email(monkeypatch):
    add('Dana McAllister', 'GLX', email='dmcallister@example.com')
    add('Priya Raman', 'GLX')
    seen = []
    monkeypatch.setattr(
        clay,
        'find_work_emails',
        lambda people: (seen.append(people), {'priya-raman': 'praman@example.com'})[1],
    )
    tools()['find_emails']()
    assert [person[0] for person in seen[0]] == ['priya-raman']


def test_an_unreachable_clay_flags_nobody(monkeypatch):
    add('Dana McAllister', 'GLX')
    monkeypatch.setattr(
        clay,
        'find_work_emails',
        lambda people: (_ for _ in ()).throw(clay.ClayError('Could not reach Clay')),
    )
    result = tools()['find_emails']()
    assert result.startswith('Error:')
    # Flagging on a transport failure would burn every lead at an event.
    assert (
        store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)[0]['status'] == store.STATUS_PENDING
    )


# ------------------------------------------------------------- 7. the push


def test_pushing_with_no_who_pushes_everyone_who_is_ready(monkeypatch):
    add('Dana McAllister', 'GLX', email='dmcallister@example.com')
    add('Priya Raman', 'GLX', email='praman@example.com')
    add('Iris Okonkwo', 'GLX')  # no email

    pushed = []

    def fake_push(**kwargs):
        pushed.append(kwargs)
        return {'id': 2850000 + len(pushed), 'status': 'created'}

    monkeypatch.setattr(marketo, 'push_lead', fake_push)
    result = tools()['push_to_marketo']()

    assert [kwargs['email'] for kwargs in pushed] == [
        'dmcallister@example.com',
        'praman@example.com',
    ]
    leads = {lead['slug']: lead for lead in store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)}
    assert leads['dana-mcallister']['status'] == store.STATUS_PUSHED
    assert leads['priya-raman']['marketo_lead_id'] == '2850002'
    # The one that couldn't go must be named, not quietly dropped.
    assert leads['iris-okonkwo']['status'] == store.STATUS_PENDING
    assert 'Iris Okonkwo' in result and 'no email' in result


def test_a_lead_with_no_email_is_never_pushed(monkeypatch):
    add('Iris Okonkwo', 'GLX')
    monkeypatch.setattr(
        marketo, 'push_lead', lambda **kwargs: pytest.fail('pushed without an email')
    )
    assert tools()['push_to_marketo'](who='Iris').startswith('Error:')


def test_no_confirmation_is_required_before_pushing(monkeypatch):
    """The gate this replaces refused every push until the rep replied again."""
    add('Dana McAllister', 'GLX', email='dmcallister@example.com')
    monkeypatch.setattr(marketo, 'push_lead', lambda **kwargs: {'id': 1000001, 'status': 'created'})
    result = tools()['push_to_marketo']()
    assert not result.startswith('Error:')
    assert '1000001' in result


def test_correcting_an_already_pushed_lead_re_pushes_to_the_same_record(monkeypatch):
    add('Dana McAllister', 'GLX', email='dmcallister@example.com')
    monkeypatch.setattr(marketo, 'push_lead', lambda **kwargs: {'id': 1000001, 'status': 'created'})
    tools()['push_to_marketo']()

    tools()['update_lead'](who='Dana', job_title='Campaign Marketing Manager')

    seen = []

    def fake_push(**kwargs):
        seen.append(kwargs)
        return {'id': 1000001, 'status': 'updated'}

    monkeypatch.setattr(marketo, 'push_lead', fake_push)
    result = tools()['push_to_marketo'](who='Dana')

    # Pushed by id, so a correction lands on the record we created rather than
    # creating a second one.
    assert seen[0]['lead_id'] == '1000001'
    assert seen[0]['job_title'] == 'Campaign Marketing Manager'
    assert 'updated' in result


def test_a_discarded_lead_is_left_alone(monkeypatch):
    add('Dana McAllister', 'GLX', email='dmcallister@example.com')
    tools()['discard_lead'](who='Dana')
    monkeypatch.setattr(marketo, 'push_lead', lambda **kwargs: pytest.fail('pushed a discard'))
    assert 'discarded' in tools()['push_to_marketo'](who='Dana')


def test_discarding_needs_a_name_when_the_thread_holds_several():
    add('Dana McAllister', 'GLX')
    add('Priya Raman', 'GLX')
    assert tools()['discard_lead']().startswith('Error:')
    assert all(
        lead['status'] == store.STATUS_PENDING
        for lead in store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)
    )


# -------------------------------------------------------- 8. reading the sheet


def test_the_sheet_can_be_listed_rather_than_denied():
    add('Dana McAllister', 'GLX')
    other = store.create_lead(
        PROGRAM.name,
        CHANNEL,
        '1700000120.000200',
        'U02EXAMPLE2',
        {'name': 'Tomas Berger', 'company': 'Zoom', 'email': None},
    )
    assert other

    result = tools()['list_event_leads']()

    # The deployed agent told the rep "I don't have access to Google Sheets".
    assert 'Dana McAllister' in result
    assert 'Tomas Berger' in result
    assert '(this thread)' in result


# ------------------------------- 9. the peer-review round (PR #588 findings)


def test_two_prospects_sharing_a_first_name_are_two_leads():
    """The loose `who` match once took 'Priya Nguyen' for the 'Priya Raman'
    already filed and refused to create her row at all."""
    add('Priya Raman', 'GLX')
    result = tools()['start_lead'](name='Priya Nguyen', company='Zoom')

    assert not result.startswith('Error:'), result
    assert [
        lead['fields']['name'] for lead in store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)
    ] == [
        'Priya Raman',
        'Priya Nguyen',
    ]


def test_the_same_person_is_still_refused_twice():
    add('Priya Raman', 'GLX')
    assert tools()['start_lead'](name='Priya Raman', company='GLX').startswith('Error:')
    assert tools()['start_lead'](name='priya raman', company='GLX').startswith('Error:')
    assert len(store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)) == 1


def test_a_partial_name_matching_two_people_is_refused_not_applied_to_both():
    add('Priya Raman', 'GLX')
    add('Priya Nguyen', 'Zoom')
    result = tools()['update_lead'](who='Priya', job_title='CEO')

    assert result.startswith('Error:')
    assert 'matches 2' in result
    assert all(
        lead['fields']['job_title'] is None
        for lead in store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)
    )


def test_a_full_name_still_addresses_one_of_two_similar_leads():
    add('Priya Raman', 'GLX')
    add('Priya Nguyen', 'Zoom')
    tools()['update_lead'](who='Priya Nguyen', job_title='CEO')
    titles = {
        lead['fields']['name']: lead['fields']['job_title']
        for lead in store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)
    }
    assert titles == {'Priya Raman': None, 'Priya Nguyen': 'CEO'}


def test_an_already_pushed_lead_is_not_re_pushed_by_the_routine_call(monkeypatch):
    """The prompt calls push_to_marketo every turn, so a bare call must be a
    no-op for anyone already in Marketo."""
    add('Dana McAllister', 'GLX', email='d@example.com')
    calls = []
    monkeypatch.setattr(
        marketo,
        'push_lead',
        lambda **kw: (calls.append(kw.get('email')), {'id': 1000001, 'status': 'created'})[1],
    )
    tools()['push_to_marketo']()
    result = tools()['push_to_marketo']()

    assert len(calls) == 1, f'pushed {len(calls)} times'
    assert 'already in Marketo' in result


def test_naming_a_pushed_lead_still_re_pushes_it(monkeypatch):
    """The correction path: update the row, then push that person by name."""
    add('Dana McAllister', 'GLX', email='d@example.com')
    monkeypatch.setattr(marketo, 'push_lead', lambda **kw: {'id': 1000001, 'status': 'created'})
    tools()['push_to_marketo']()

    tools()['update_lead'](who='Dana McAllister', job_title='Campaign Marketing Manager')
    seen = []
    monkeypatch.setattr(
        marketo,
        'push_lead',
        lambda **kw: (seen.append(kw), {'id': 1000001, 'status': 'updated'})[1],
    )
    tools()['push_to_marketo'](who='Dana McAllister')

    assert len(seen) == 1
    assert seen[0]['lead_id'] == '1000001'
    assert seen[0]['job_title'] == 'Campaign Marketing Manager'


def test_one_failed_push_still_reports_the_ones_that_landed(monkeypatch):
    add('Dana McAllister', 'GLX', email='d@example.com')
    add('Priya Raman', 'GLX', email='p@example.com')

    def flaky(**kwargs):
        if kwargs['email'] == 'p@example.com':
            raise marketo.MarketoError('Marketo rejected the record')
        return {'id': 1000001, 'status': 'created'}

    monkeypatch.setattr(marketo, 'push_lead', flaky)
    result = tools()['push_to_marketo']()

    # Dana is in Marketo; her id must survive Priya's failure.
    assert '1000001' in result
    assert 'Priya Raman failed' in result
    leads = {lead['slug']: lead for lead in store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)}
    assert leads['dana-mcallister']['status'] == store.STATUS_PUSHED
    assert leads['priya-raman']['status'] == store.STATUS_PENDING


def test_a_flagged_lead_is_never_enriched_twice(monkeypatch):
    add('Iris Okonkwo', 'GLX')
    runs = []
    monkeypatch.setattr(clay, 'find_work_emails', lambda people: (runs.append(len(people)), {})[1])
    tools()['find_emails']()
    result = tools()['find_emails']()

    assert len(runs) == 1, f'ran Clay {len(runs)} times'
    lead = store.leads_for_thread(CHANNEL, THREAD, PROGRAM.name)[0]
    assert lead['status'] == store.STATUS_FLAGGED
    # The note was appended once, not once per nudge.
    assert lead['mops_notes'].count('Enrichment returned nobody') == 1
    assert 'could not be placed' in result


def test_a_flagged_lead_does_not_block_enriching_the_others(monkeypatch):
    add('Iris Okonkwo', 'GLX')
    add('Priya Raman', 'GLX')
    monkeypatch.setattr(clay, 'find_work_emails', lambda people: {})
    tools()['find_emails']()  # flags both

    add('Dana McAllister', 'GLX')
    asked = []
    monkeypatch.setattr(
        clay,
        'find_work_emails',
        lambda people: (asked.extend(p[0] for p in people), {'dana-mcallister': 'd@example.com'})[
            1
        ],
    )
    tools()['find_emails']()

    assert asked == ['dana-mcallister']


def test_one_cards_failure_does_not_hide_the_other_leads(monkeypatch):
    """One outer try/except covers the whole render, so without per-lead
    isolation a rate limit on lead two left lead three with no card at all."""
    add('Dana McAllister', 'GLX')
    add('Priya Raman', 'GLX')
    add('Iris Okonkwo', 'GLX')

    posted = []

    class FlakySlack:
        def chat_postMessage(self, **kwargs):
            if 'Priya' in kwargs.get('text', ''):
                raise RuntimeError('ratelimited')
            posted.append(kwargs['text'])
            return {'ts': f'170{len(posted)}.0'}

        def chat_update(self, **kwargs):
            posted.append(kwargs['text'])

    failed = lead_mod.render_cards(capture(), FlakySlack())

    # The cause travels with the name — a bare "a card failed" cannot tell a
    # rate limit from a malformed block.
    assert [who for who, _ in failed] == ['Priya Raman']
    assert 'ratelimited' in failed[0][1]
    assert any('Dana' in text for text in posted)
    assert any('Iris' in text for text in posted), 'the third card was lost'


def test_two_leads_showing_the_same_name_both_report_their_failure():
    """A name is not unique — `update_lead` can leave two rows displaying one.
    Keyed by name, the second failure silently replaced the first."""
    add('Priya Raman', 'GLX')
    add('Priya Nguyen', 'Zoom')
    tools()['update_lead'](who='Priya Nguyen', name='Priya Raman')

    class DeadSlack:
        def chat_postMessage(self, **kwargs):
            raise RuntimeError('ratelimited')

        def chat_update(self, **kwargs):
            raise RuntimeError('ratelimited')

    failed = lead_mod.render_cards(capture(), DeadSlack())

    assert len(failed) == 2, f'one failure was lost: {failed}'
