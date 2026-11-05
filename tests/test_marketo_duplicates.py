"""One email, several Marketo records: which one gets updated, and the push path
that falls back to it.

Marketo skips a push whose lookup matched more than one record. The ranking is
pure, so most of this exercises `choose_duplicate` directly; the rest fakes
`_request` to prove the fallback fires only when the skip really was a duplicate
and re-pushes against the chosen id.
"""

import pytest

from agent import marketo

PROGRAM = marketo.EventProgram(
    id=1,
    name='FY2026-Q3-Knak Sponsored Event-Test',
    event_name='Test',
    activation='',
)


def record(**overrides):
    """A lead as /rest/v1/leads.json returns it, with the fields the ranking reads."""
    return {
        'id': 100,
        'email': 'arun@example.com',
        'sfdcType': None,
        'createdAt': '2026-01-01T00:00:00Z',
        'updatedAt': '2026-01-01T00:00:00Z',
        **overrides,
    }


# ------------------------------------------------------------------ 1. SFDC type


def test_contact_beats_lead_even_when_the_lead_is_newer_and_older():
    contact = record(
        id=1, sfdcType='Contact', updatedAt='2020-01-01T00:00:00Z', createdAt='2024-01-01T00:00:00Z'
    )
    lead = record(
        id=2, sfdcType='Lead', updatedAt='2026-09-01T00:00:00Z', createdAt='2019-01-01T00:00:00Z'
    )
    assert marketo.choose_duplicate([lead, contact])['id'] == 1


def test_lead_beats_a_record_not_synced_to_salesforce():
    lead = record(id=1, sfdcType='Lead', updatedAt='2020-01-01T00:00:00Z')
    unsynced = record(id=2, sfdcType=None, updatedAt='2026-09-01T00:00:00Z')
    assert marketo.choose_duplicate([unsynced, lead])['id'] == 1


def test_sfdc_type_is_matched_case_insensitively():
    assert marketo._sfdc_rank('contact') == marketo._sfdc_rank('Contact') == 0
    assert marketo._sfdc_rank('LEAD') == 1


def test_an_unrecognised_sfdc_type_sorts_last_rather_than_raising():
    assert marketo._sfdc_rank('Account') == marketo._sfdc_rank(None) == 2


# --------------------------------------------------------------- 2. updatedAt


def test_most_recently_updated_wins_within_the_same_sfdc_type():
    stale = record(id=1, sfdcType='Lead', updatedAt='2026-01-01T00:00:00Z')
    fresh = record(id=2, sfdcType='Lead', updatedAt='2026-09-01T00:00:00Z')
    assert marketo.choose_duplicate([stale, fresh])['id'] == 2


def test_a_missing_updatedat_never_wins():
    missing = record(id=1, sfdcType='Lead', updatedAt=None)
    present = record(id=2, sfdcType='Lead', updatedAt='2020-01-01T00:00:00Z')
    assert marketo.choose_duplicate([missing, present])['id'] == 2


# --------------------------------------------------------------- 3. createdAt


def test_earliest_created_wins_when_updated_ties():
    later = record(id=1, sfdcType='Lead', createdAt='2026-05-01T00:00:00Z')
    original = record(id=2, sfdcType='Lead', createdAt='2019-05-01T00:00:00Z')
    assert marketo.choose_duplicate([later, original])['id'] == 2


def test_an_unparseable_createdat_never_wins():
    broken = record(id=1, sfdcType='Lead', createdAt='not a date')
    fine = record(id=2, sfdcType='Lead', createdAt='2026-05-01T00:00:00Z')
    assert marketo.choose_duplicate([broken, fine])['id'] == 2


# ------------------------------------------------------- 4. determinism / edges


def test_records_identical_on_every_criterion_resolve_the_same_way_each_time():
    a, b = record(id=7, sfdcType='Lead'), record(id=3, sfdcType='Lead')
    assert marketo.choose_duplicate([a, b])['id'] == 3
    assert marketo.choose_duplicate([b, a])['id'] == 3


def test_no_records_is_not_an_error():
    assert marketo.choose_duplicate([]) is None


def test_the_full_ordering_holds_across_a_realistic_mix():
    records = [
        record(id=1, sfdcType=None, updatedAt='2026-09-05T00:00:00Z'),
        record(id=2, sfdcType='Lead', updatedAt='2026-09-04T00:00:00Z'),
        record(
            id=3,
            sfdcType='Contact',
            updatedAt='2026-01-01T00:00:00Z',
            createdAt='2025-01-01T00:00:00Z',
        ),
        record(
            id=4,
            sfdcType='Contact',
            updatedAt='2026-01-01T00:00:00Z',
            createdAt='2021-01-01T00:00:00Z',
        ),
    ]
    # Contact outranks the rest; the two Contacts tie on updatedAt, so the
    # earlier createdAt takes it.
    assert marketo.choose_duplicate(records)['id'] == 4


# ------------------------------------------------------------- 5. the push path


class FakeMarketo:
    """Stands in for `_request`, recording the pushes it was asked to make."""

    def __init__(self, *, first_status, matches=()):
        self.first_status = first_status
        self.matches = list(matches)
        self.pushes = []
        self.lookups = 0

    def __call__(self, method, path, **kwargs):
        if path == '/rest/v1/leads.json':
            self.lookups += 1
            return {'result': self.matches}
        body = kwargs['json']
        self.pushes.append(body)
        if body['lookupField'] == 'email':
            return {
                'result': [
                    {
                        'id': 100,
                        'status': self.first_status,
                        'reasons': [{'code': '1007', 'message': 'Multiple lead match'}],
                    }
                ]
            }
        return {'result': [{'id': body['input'][0]['id'], 'status': 'updated'}]}


def push(monkeypatch, fake):
    monkeypatch.setattr(marketo, '_request', fake)
    return marketo.push_lead(
        program=PROGRAM,
        email='arun@example.com',
        name='Arun Mehta',
        company='KNAK',
        notes='met at the booth',
    )


def test_a_clean_push_never_looks_for_duplicates(monkeypatch):
    fake = FakeMarketo(first_status='created')
    result = push(monkeypatch, fake)
    assert fake.lookups == 0
    assert len(fake.pushes) == 1
    assert 'resolved_duplicate' not in result


def test_a_skip_with_several_matches_re_pushes_against_the_chosen_record(monkeypatch):
    fake = FakeMarketo(
        first_status='skipped',
        matches=[record(id=11, sfdcType='Lead'), record(id=12, sfdcType='Contact')],
    )
    result = push(monkeypatch, fake)

    assert [p['lookupField'] for p in fake.pushes] == ['email', 'id']
    assert fake.pushes[1]['input'][0]['id'] == 12
    assert result['resolved_duplicate'] == {
        'considered': 2,
        'chosen_id': 12,
        'sfdc_type': 'Contact',
    }


def test_the_second_push_carries_the_same_program_status_and_field_values(monkeypatch):
    fake = FakeMarketo(
        first_status='skipped',
        matches=[record(id=11, sfdcType='Lead'), record(id=12, sfdcType='Contact')],
    )
    push(monkeypatch, fake)

    retry = fake.pushes[1]
    assert retry['programName'] == PROGRAM.name
    assert retry['programStatus'] == marketo.PROGRAM_STATUS
    assert retry['input'][0]['lastName'] == 'Mehta'
    assert retry['input'][0][marketo.NOTES_FIELD] == 'met at the booth'


def test_a_skip_that_is_not_a_duplicate_still_raises_its_original_reason(monkeypatch):
    fake = FakeMarketo(first_status='skipped', matches=[record(id=11)])
    with pytest.raises(marketo.MarketoError, match='Multiple lead match'):
        push(monkeypatch, fake)
    assert [p['lookupField'] for p in fake.pushes] == ['email']


def test_an_email_is_still_mandatory():
    with pytest.raises(ValueError):
        marketo.push_lead(program=PROGRAM, email='  ')
