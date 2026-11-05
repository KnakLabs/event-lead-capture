"""Recasing badge capitals, against the kind of values a badge scan produces.

A badge shouts so it can be read across a booth. Left alone it reaches Marketo as
`firstName: ANNALIIA` and greets her as "Hi ANNALIIA". Only capitalisation may change
— never a character — because the spelling is the one thing nothing is allowed to
touch.

What a short all-caps word means depends on the field it is in, which is why
there are three functions: ANA is a person, GLX is an acronym, OF is a word.
"""

from agent import badge_text

name = badge_text.recase_name
company = badge_text.recase_company
title = badge_text.recase_title


# ------------------------------------------------------------- the real badges


def test_the_values_from_the_live_captures():
    assert name('ANNALIIA VOLKOV') == 'Annaliia Volkov'
    assert company('MERIDIAN GROUP') == 'Meridian Group'
    assert title('GLOBAL ABM AND DEMAND GENERATION MANAGER') == (
        'Global ABM and Demand Generation Manager'
    )
    assert name('MARTINA KOWALSKI') == 'Martina Kowalski'
    assert title('SENIOR PAID MEDIA STRATEGIST') == 'Senior Paid Media Strategist'
    assert company('GLX') == 'GLX'


def test_an_unusual_spelling_survives_untouched():
    """The double-i is the whole point: recase, never respell."""
    assert 'aliia' in name('ANNALIIA VOLKOV')
    assert name('JONATHON SMYTHE') == 'Jonathon Smythe'


# --------------------------------------------- a short word means three things


def test_a_short_word_in_a_name_is_a_person_not_an_acronym():
    assert name('ANA MACHADO') == 'Ana Machado'
    assert name('JO ANN LEE') == 'Jo Ann Lee'
    assert name('TED KIM') == 'Ted Kim'


def test_a_short_word_in_a_company_is_an_acronym():
    assert company('IBM') == 'IBM'
    assert company('SAP AMERICA') == 'SAP America'


def test_a_joining_word_is_never_an_acronym():
    assert company('BANK OF AMERICA') == 'Bank of America'
    assert title('HEAD OF MARKETING, APAC') == 'Head of Marketing, APAC'
    assert title('DIRECTOR OF IT') == 'Director of IT'


def test_a_title_keeps_its_acronyms():
    assert title('VP MARKETING') == 'VP Marketing'
    assert title('SEO AND SEM LEAD') == 'SEO and SEM Lead'
    assert title('EMEA DEMAND GENERATION LEAD') == 'EMEA Demand Generation Lead'


def test_the_first_word_of_a_title_is_capitalised_even_when_joining():
    assert title('OF COUNSEL') == 'Of Counsel'


# --------------------------------------------------- abbreviations vs acronyms


def test_a_trailing_dot_marks_a_shortened_word():
    assert title('SR. GROWTH MARKETING MANAGER') == 'Sr. Growth Marketing Manager'
    assert name('ROBERT SMITH JR.') == 'Robert Smith Jr.'


def test_a_dotted_acronym_is_left_alone():
    assert title('VP, U.S. SALES') == 'VP, U.S. Sales'


def test_a_trailing_comma_does_not_make_a_word_of_an_acronym():
    assert title('VP, MARKETING') == 'VP, Marketing'


# ------------------------------------------------------------------ surnames


def test_mc_surnames_keep_their_inner_capital():
    assert name('DANA MCALLISTER') == 'Dana McAllister'
    assert name('RONAN MCDONALD') == 'Ronan McDonald'


def test_mac_is_left_alone_because_machado_is_not_macahdo():
    assert name('ANA MACHADO') == 'Ana Machado'


def test_apostrophes_and_hyphens_capitalise_correctly():
    assert name("SIOBHAN O'NEILL") == "Siobhan O'Neill"
    assert name('ANNE-MARIE DUBOIS') == 'Anne-Marie Dubois'


# --------------------------------------------------------- leaving well alone


def test_anything_already_cased_is_returned_untouched():
    """Second-guessing a human's casing is how McAllister becomes Mcallister."""
    for value in ('Annaliia Volkov', 'McAllister', 'Anne-Marie Dubois'):
        assert name(value) == value
    for value in ('PayNearMe', 'SurveyMonkey', 'iRobot', 'eBay', 'Meridian Group'):
        assert company(value) == value


def test_blank_and_missing_values_are_safe():
    for fn in (name, company, title):
        assert fn('') == ''
        assert fn('   ') == ''
        assert fn(None) == ''


def test_inner_spacing_is_preserved():
    assert company('MERIDIAN   GROUP') == 'Meridian   Group'


def test_a_camel_case_brand_cannot_be_recovered_and_that_is_accepted():
    """Nothing in `PAYNEARME` says where the capital belongs. Better than caps."""
    assert company('PAYNEARME') == 'Paynearme'
    assert company('SURVEYMONKEY') == 'Surveymonkey'
