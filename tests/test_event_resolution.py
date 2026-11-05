"""Which Marketo program a Slack channel names.

The rule: a lead goes to the program whose name matches the channel it was
captured in. Nothing is inferred and nobody is asked, so the matching has to
survive what Slack does to a name — lowercasing it and forbidding spaces.

These run against the example convention shipped in `conventions.py` and the
real program names it was written for, so they also serve as the worked example
of what `PROGRAM_NAME_PATTERN` is supposed to admit and reject. Swap in your own
pattern and these are the tests to change with it.
"""

import pytest

from agent.marketo import EventProgram, canonical, find_by_channel, parse_program_name


def program(name: str, created: str = '2026-01-01T00:00:00Z+0000') -> EventProgram:
    parsed = parse_program_name(name) or ('', '')
    return EventProgram(
        id=abs(hash(name)) % 100000,
        name=name,
        event_name=parsed[0],
        activation=parsed[1],
        created_at=created,
    )


# ------------------------------------------------------- what counts as an event


@pytest.mark.parametrize(
    'name,expected',
    [
        (
            'FY2026-Q2-Knak Hosted Event-Salesforce Connections-Party',
            ('Salesforce Connections', 'Party'),
        ),
        (
            'FY2026-Q2-Knak Sponsored Event-Salesforce Connections-Booth',
            ('Salesforce Connections', 'Booth'),
        ),
        ('FY2026-Q2-Knak Hosted Event-Roundtable-Chicago', ('Roundtable', 'Chicago')),
        (
            'FY2026-Q2-Knak Sponsored Event-Gartner Marketing Symposium-Speaking Session',
            ('Gartner Marketing Symposium', 'Speaking Session'),
        ),
        # A hyphenated event name splits on the LAST hyphen.
        ('FY2026-Q2-Knak Sponsored Event-MOps-Apalooza-Booth', ('MOps-Apalooza', 'Booth')),
        # No event type: qualifies anyway, the activation just comes back empty.
        ('FY2026-Q3-Knak Sponsored Event-Test', ('Test', '')),
        # Nothing after the prefix at all: still qualifies.
        ('FY2026-Q3-Knak Hosted Event', ('', '')),
        # Case and spacing vary in the wild.
        ('fy2026-q3-knak sponsored event-Test Event-Booth', ('Test Event', 'Booth')),
    ],
)
def test_an_event_program_qualifies_and_its_suffix_is_parsed(name, expected):
    assert parse_program_name(name) == expected


@pytest.mark.parametrize(
    'name',
    [
        'FY2026-Q3-WB-Email Governance',  # webinar
        'FY2026-Q3-EM-August Newsletter',  # single-touch email
        'FY2026-Q3-Paid-LinkedIn-Always On',  # online ads
        'Knak - Generic Landing Page',  # web content
        'FY2026-Nurture-FSR-Phase 1',  # nurture
        'FY2026-Q3-Knak Event-Adobe Summit-Booth',  # missing Hosted/Sponsored
        'FY2026-Q3-Knak Webinar Event-Something',  # wrong word before Event
        '2026-Q3-Knak Hosted Event-Adobe Summit',  # missing the FY prefix
    ],
)
def test_other_campaign_types_never_qualify(name):
    assert parse_program_name(name) is None


# ------------------------------------------------------------------ the label


def test_the_label_always_renders_something_usable():
    assert (
        program('FY2026-Q2-Knak Sponsored Event-Adobe Summit-Booth').label == 'Adobe Summit — Booth'
    )
    assert program('FY2026-Q3-Knak Sponsored Event-Test').label == 'Test'
    # Neither part parsed: fall back to the full program name rather than blank.
    assert program('FY2026-Q3-Knak Hosted Event').label == 'FY2026-Q3-Knak Hosted Event'


# --------------------------------------------------------- channel -> program

PROGRAMS = [
    program('FY2026-Q3-Knak Hosted Event-Dreamforce-Party'),
    program('FY2026-Q3-Knak Sponsored Event-Test'),
    program('FY2026-Q2-Knak Sponsored Event-Adobe Summit-Booth'),
    program('FY2026-Q2-Knak Sponsored Event-Adobe Summit-Speaking session'),
]

DREAMFORCE = 'FY2026-Q3-Knak Hosted Event-Dreamforce-Party'


@pytest.mark.parametrize(
    'channel',
    [
        # Exactly as Slack renders the program name.
        'fy2026-q3-knak-hosted-event-dreamforce-party',
        # Someone typed capitals; Slack lowercases, but be tolerant anyway.
        'FY2026-Q3-Knak-Hosted-Event-Dreamforce-Party',
        # Underscores instead of hyphens.
        'fy2026_q3_knak_hosted_event_dreamforce_party',
        # Doubled separators collapse.
        'fy2026-q3-knak-hosted-event--dreamforce--party',
        # Truncated at Slack's 80-character limit — a unique prefix still resolves.
        'fy2026-q3-knak-hosted-event-dreamforce-par',
    ],
)
def test_a_channel_resolves_to_its_program(channel):
    hit = find_by_channel(channel, PROGRAMS)
    assert hit is not None and hit.name == DREAMFORCE


@pytest.mark.parametrize(
    'channel,why',
    [
        ('general', 'an unrelated channel'),
        ('', 'an empty channel name'),
        # Prefixes both the Booth and the Speaking session program.
        ('fy2026-q2-knak-sponsored-event-adobe-summit', 'an ambiguous prefix'),
    ],
)
def test_no_match_is_better_than_a_guess(channel, why):
    assert find_by_channel(channel, PROGRAMS) is None, why


def test_the_longest_real_program_still_fits_slacks_channel_limit():
    longest = 'FY2026-Q2-Knak Sponsored Event-Gartner Marketing Symposium-Speaking Session'
    assert len(canonical(longest)) <= 80
