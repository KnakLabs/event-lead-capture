"""How YOUR Marketo instance and tracking sheet are organised.

Everything in this file is a convention rather than a credential — it describes
the shape of someone's Marketo, not their access to it. Keys, hosts and account
ids live in `.env` instead (see `.env.example`).

The values shipped here are the ones this was built against, at Knak. They are
worked examples, not defaults that will work anywhere: a different Marketo
instance has a different folder name, a different program-naming scheme, and
almost certainly no field called `Personal_Notes__c`. Change all four before the
first capture, or the program lookup matches nothing and the push fails on an
unknown field.
"""

import re

# ---------------------------------------------------------------- Marketo

# The folder every event program lives under, one per year. `{year}` is filled
# in with the current year, so a new year needs a new folder, not a code change.
EVENTS_ROOT_TEMPLATE = '{year} - Knak - Events'

# Which programs inside that folder count as events the agent can file into.
#
# A Slack channel is named after its Marketo program, and the two are compared
# after both are lowercased with runs of punctuation collapsed to one hyphen —
# so this pattern only has to be loose enough to admit the right programs, not
# to parse them exactly. Only the PREFIX decides; whatever follows is parsed
# best-effort for the label shown to the rep and never disqualifies a program.
#
# Knak's scheme, which this matches:
#     FY2026-Q3-Knak Hosted Event-Dreamforce-Party
#     FY2026-Q3-Knak Sponsored Event-Adobe Summit-Booth
#
# Replace it with something that matches yours. To accept every program in the
# events folder instead, use `re.compile('')`.
PROGRAM_NAME_PATTERN = re.compile(
    r'^FY(\d{4})-Q(\d)-Knak\s+(Hosted|Sponsored)\s+Event\b', re.IGNORECASE
)

# The program membership status every captured lead is given. Marketo rejects a
# status the program's channel doesn't define, and the agent checks this against
# the channel's real progression statuses before pushing — so a status valid for
# a Tradeshow but not a Live Event fails loudly rather than silently.
PROGRAM_STATUS = 'Attended'

# The API name of the Marketo field the rep's notes are written to. This one is
# a custom field at Knak; yours will be called something else. It must be a
# `text` field rather than `string` — a long voice-note transcript overflows a
# string field and Marketo truncates it without saying so.
NOTES_FIELD = 'Personal_Notes__c'

# ----------------------------------------------------------------- Sheet

# The columns Marketing Ops reads, in order, written as the header row of every
# event's tab. Renaming one renames it in the sheet; the agent addresses them by
# position, so reordering or removing one needs a matching change in `store.py`.
SHEET_COLUMNS = (
    'Timestamp',
    'Name',
    'Company',
    'Email',
    'Job Title',
    'Sales Notes',
    'MOPs Notes',
    # Filled in on a successful push: a deep link to the person in Marketo.
    'Marketo Lead',
)


# ------------------------------------------------------------ Transcription

# Spellings to nudge the transcription model towards. A rep says product and
# vendor names at speed on a loud expo floor, and without a hint those come back
# phonetically — which then lands in Sales Notes and in Marketo. List the words
# your team actually says.
TRANSCRIPTION_VOCABULARY = 'Knak, Marketo, Adobe, marketing ops, MOPs, email builder, demo.'
