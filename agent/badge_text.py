"""Capitalisation of text read off a badge.

Badges are printed in capitals so they can be read across a booth, not because
that is how anyone writes their name. Left alone, `ANNALIIA VOLKOV` reaches Marketo
as `firstName: ANNALIIA` and every email using the token greets her as "Hi ANNALIIA".

So an all-caps value is recased on the way in. Only capitalisation changes —
never a character — so an unusual spelling survives intact, which matters because
the spelling is the one thing the agent is forbidden to touch (`system_prompt`).

Which field it is decides what a short all-caps word means, and there is no way
to get this right without knowing:

    ANA MACHADO                 a name    — ANA is a person, not an acronym
    GLX                         a company — GLX is an acronym, not a word
    HEAD OF MARKETING, APAC     a title   — OF is a word, APAC is an acronym

Hence three entry points rather than one clever function.

Deliberately deterministic and in the tool rather than asked of the model:
casing is a rule, not a judgement, and a model applying it per badge would be
slower and occasionally creative.

What this cannot recover is a brand's internal capital — `PAYNEARME` becomes
`Paynearme`, not `PayNearMe` — because nothing in the text says where the capital
belongs. Still an improvement on shouting, and the rep can correct it.
"""

import re

# Never an acronym, always a word — so `BANK OF AMERICA` doesn't keep its `OF`.
_STOPWORDS = frozenset(
    {'a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'in', 'of', 'on', 'or', 'the', 'to', 'with'}
)

# Longer than the short-token rule below would catch, and worth keeping.
_LONG_ACRONYMS = frozenset({'EMEA', 'APAC', 'LATAM', 'MENA', 'DACH', 'NORAM', 'SAAS', 'ABM'})

# A token this short in a company or a job title is an acronym often enough that
# keeping it beats title-casing it — GLX, IBM, VP, IT, SEO, PLG.
_MAX_SHORT_ACRONYM = 3

# Split on whitespace but keep it, so original spacing is preserved.
_TOKENS = re.compile(r'(\s+)')

# "MCALLISTER" -> "McAllister". Mc followed by three or more letters is a Scottish or
# Irish surname often enough to be worth it; Mac is not (Machado, Mackenzie).
_MC = re.compile(r'^Mc([a-z])(?=[a-z]{2})')


def recase_name(value: str) -> str:
    """A person's name. No acronym preservation — `ANA` is a person."""
    return _recase(value, keep_acronyms=False)


def recase_company(value: str) -> str:
    """An employer. A short token is an acronym: GLX, IBM, SAP."""
    return _recase(value, keep_acronyms=True, lowercase_stopwords=True)


def recase_title(value: str) -> str:
    """A job title. Acronyms kept, joining words lowercased."""
    return _recase(value, keep_acronyms=True, lowercase_stopwords=True)


def _recase(value: str, *, keep_acronyms: bool, lowercase_stopwords: bool = False) -> str:
    """Anything already carrying a lowercase letter is returned untouched — a
    human or the model has already cased it, and second-guessing that is how
    `McAllister` becomes `Mcallister`."""
    text = (value or '').strip()
    if not text or any(character.islower() for character in text):
        return text

    parts = _TOKENS.split(text)
    first = True
    out = []
    for part in parts:
        if not part.strip():
            out.append(part)
            continue
        out.append(
            _recase_token(
                part,
                keep_acronyms=keep_acronyms,
                # The first word of a title is capitalised even when it is a
                # joining word: "Of Counsel", not "of Counsel".
                lowercase_stopwords=lowercase_stopwords and not first,
            )
        )
        first = False
    return ''.join(out)


def _is_abbreviation(token: str) -> bool:
    """`SR.` is a shortened word, `U.S.` is an acronym, `VP,` is neither.

    Only a trailing dot marks an abbreviation — and only when it's the token's
    single dot, or `U.S.` would be title-cased to `U.s.`. Trailing commas and
    semicolons are separators and say nothing either way.
    """
    core = token.rstrip(',;:')
    return core.endswith('.') and '.' not in core[:-1]


def _recase_token(token: str, *, keep_acronyms: bool, lowercase_stopwords: bool) -> str:
    letters = re.sub(r'[^A-Z]', '', token)
    if not letters:
        return token
    word = letters.lower()

    if lowercase_stopwords and word in _STOPWORDS:
        return token.lower()
    is_acronym = (
        keep_acronyms
        and (letters in _LONG_ACRONYMS or len(letters) <= _MAX_SHORT_ACRONYM)
        and word not in _STOPWORDS
        and not _is_abbreviation(token)
    )
    if is_acronym:
        return token

    # str.title() also capitalises after an apostrophe or hyphen, which is what
    # O'Neill-Smith wants.
    return _MC.sub(lambda match: f'Mc{match.group(1).upper()}', token.title())
