"""
Near-miss precision for the local theme patterns in `binder._THEME_PATTERNS`.

`test_autofill_binder.py` asserts precision on labels from the measured
corpus. The outside audit of 2026-10-12 (AUDIT_REPORT.md §2, H4) found every
wrong deterministic fill in a label it had *not* seen: the theme's keyword
read in another sense — "relocation assistance" for willingness to relocate,
"may we contact you via text" for the employer reference, "available for
interviews" for a term confirmation, a bare "Start Date", "extended work
authorization" for the STEM OPT extension. Each such label became a confident
wrong answer from an unrelated stored fact.

So for every pattern there are two lists here: labels that carry the keyword
and must NOT land on the theme, and positive controls that still must. A
near-miss landing on UNKNOWN is the right outcome; it goes to the model gate
or to review, which costs one glance.
"""
import pytest

from app.autofill.binder import DeterministicBinder, _THEME_PATTERNS
from app.autofill.buckets import looks_like_date, match_numeric_bucket, match_term_bucket
from app.autofill.format import match_option
from app.autofill.schema import FieldClass, FieldKind, FieldOption, FormField
from app.autofill.themes import QuestionTheme


def _theme(label):
    return DeterministicBinder().classify_themes([label])[label][0]


def _options(*labels):
    return [FieldOption(label=o, value=o) for o in labels]


#: theme -> (near misses that must not match it, positive controls that must)
NEAR_MISSES = {
    QuestionTheme.WILLING_TO_RELOCATE: (
        [
            "Do you require relocation assistance?",                              # audit §2
            "Will you need a relocation package?",
            "Are you requesting relocation reimbursement or a relocation stipend?",
            "We are unable to provide relocation assistance. Will you be located in the Seattle area?",
        ],
        [
            "Are you open to relocation for this role?",                          # corpus
            "Are you willing to relocate?",
            "Would you relocate to Austin, TX for this position?",
        ],
    ),
    QuestionTheme.CONTACT_CURRENT_EMPLOYER: (
        [
            "May we contact you via text message?",                               # audit §2
            "May we contact you via SMS about your application?",
            "Do we have your permission to contact you by WhatsApp?",
            "May we contact you at this phone number?",
        ],
        [
            "May we contact your current employer?",
            "Do we have your permission to contact your previous employer for a reference?",
            "May we contact your present employer?",
        ],
    ),
    QuestionTheme.INTERNSHIP_AVAILABILITY: (
        [
            "Interview availability: are you available for interviews in the next two weeks?",  # audit §2
            "Are you available for an interview next week?",
            "Please confirm your availability for a phone interview",
        ],
        [
            "I confirm my availability for a Summer 2027 (May/June starts) internship",
            "Are you available for the Summer 2027 term?",
            "Do you have the flexibility to consider a Winter 2027 start?",
        ],
    ),
    QuestionTheme.INTERVIEWED_HERE_BEFORE: (
        [
            "Interview availability: are you available for interviews in the next two weeks?",
            "What is your preferred programming language for interviews?",       # x6 in the corpus
            "Interview Code of Conduct",                                           # x7
            "Interview Recording Consent",                                         # x3
        ],
        [
            "Have you ever interviewed at Anthropic before?",                     # corpus
            "Have you previously interviewed with us?",
            "Have you applied or interviewed here in the past 12 months?",
        ],
    ),
    QuestionTheme.EARLIEST_START: (
        [
            "Start Date",                                                          # audit §2
            "Start date month",                                                    # Greenhouse education block, x411
            "Start date year",
            "Employment start date",
            "Are you available to start on either Monday, May 24th, 2026 or Monday, June 14th, 2027?",
        ],
        [
            "Earliest start date",
            "What is your earliest available start date?",
            "When can you start?",
            "When is the earliest you would want to start working with us?",      # corpus
            "Current Notice Period",
            "Preferred Internship/Co-Op Start Date",                               # x9
            "What is your ideal internship start date?",
            "When are you available to start?",                                    # x7
            "What is the start date that you could join? m/d/yyyy",
        ],
    ),
    QuestionTheme.WORK_AUTHORIZATION: (
        [
            "Do you have a STEM degree that would make you eligible for extended work authorization?",  # audit §2
            "Are you eligible for a 24-month STEM OPT extension to work in the US?",
            "Are you currently authorized to work under CPT or OPT?",
        ],
        [
            "Are you legally authorized to work in the United States?",           # corpus
            "Are you legally entitled to work in Canada?",
            "Are you authorized to work in the country for which you applied?",
        ],
    ),
    QuestionTheme.IN_OFFICE_TOLERANCE: (
        [
            "Are you able to work in-person in San Jose this summer?",            # audit §2
            "Are you open to being in-office 5 days a week in Sunnyvale?",       # x11
            "Are you able to work in person at our Louisville, CO HQ?",
            "This is a hybrid role based in Alpharetta, GA. Are you willing and able to accommodate this?",
            "Are you willing to be fully in-person in San Francisco?",
        ],
        [
            "Are you open to working in-person in one of our offices 25% of the time?",  # corpus
            "Are you comfortable working in the office 3 days/week?",
            "Are you open to a hybrid schedule?",
            "This role is in person in the office. Are you comfortable with that?",
        ],
    ),
    QuestionTheme.WORK_ADDRESS: (
        [
            "Please provide your university email address",                       # audit §2, x7
            "Email Address",
            "Enter your .EDU email address. Assessments will only be sent to your school email.",
            "Please provide the institutional email address of the professor",
            "Street Address (Optional Second Line)",                               # x5: line 2, not line 1
            "Do you currently have a permanent or school address in the DC metro area?",  # x5: yes/no
            # Review of 2026-10-12, finding 3: somebody else's address, or a
            # URL. With street_address stored each filled with the home street.
            "Employer Address",
            "University Address",
            "School Address",
            "Address of your most recent employer",
            "GitHub address",
            "LinkedIn address",
            "Portfolio address",
            "Previous Address",
            "Billing address",
            "Reference 1 Address",
            "Please provide the address of the office you would like to work from",
        ],
        [
            "Address 1",
            "Address Line 1",
            "Address",
            "Home Address",
            "Street address",
            "Mailing address",
            "Permanent Address",
            "Legal address:",
            "Current Mailing Address",
            "Please provide your current mailing address. This information is used solely for recruitment.",  # x7
            "Primary Mailing Address",                                             # x5
            "1.6 Please provide us with your current STREET ADDRESS (location where you permanently reside and will work in this role).",  # x4
            "What address would you work from?",
        ],
    ),
}


def test_another_partys_address_is_never_filled_with_the_home_street():
    """Finding 3 end to end: on main these went to review because the theme's
    key was unset; after M9 pointed it at street_address they filled at 1.0."""
    from app.autofill.binder import resolve_form
    from app.autofill.classify import classify
    from app.autofill.memory import Memory
    from app.autofill.schema import FormSchema

    misses, hits = NEAR_MISSES[QuestionTheme.WORK_ADDRESS]
    fields = [FormField(key="q{}".format(i), label=label, kind=FieldKind.TEXT,
                        field_class=classify("q{}".format(i), label, FieldKind.TEXT))
              for i, label in enumerate(misses + hits)]
    memory = Memory(facts={"street_address": "1 Main St", "email": "ada@example.com", "github": "https://github.com/ada"})
    plan = resolve_form(FormSchema(source="t", ats="greenhouse", posting_id="1", fields=fields),
                        memory, binder=DeterministicBinder())
    by_label = {e.label: e for e in plan.entries}
    for label in misses:
        assert by_label[label].value != "1 Main St", label
    for label in hits:
        if "second line" in label.lower():
            continue
        assert by_label[label].value == "1 Main St" and not by_label[label].needs_review, label


@pytest.mark.parametrize("theme,label", [
    (theme, label) for theme, (misses, _hits) in NEAR_MISSES.items() for label in misses
])
def test_a_keyword_in_another_sense_does_not_land_on_the_theme(theme, label):
    assert _theme(label) != theme, "{!r} must not be {}".format(label, theme.value)


@pytest.mark.parametrize("theme,label", [
    (theme, label) for theme, (_misses, hits) in NEAR_MISSES.items() for label in hits
])
def test_the_positive_controls_still_land_on_the_theme(theme, label):
    assert _theme(label) == theme, "{!r} must still be {}".format(label, theme.value)


def test_every_local_pattern_has_a_near_miss_list_or_a_stated_reason():
    """A pattern added without near misses is a pattern nobody tried to break.
    The themes below are exempt for a stated reason; anything else new here
    must be added to NEAR_MISSES."""
    exempt = {
        # Keyword-exact and unambiguous on an application form.
        QuestionTheme.GPA, QuestionTheme.POSTAL_CODE, QuestionTheme.HEARD_ABOUT,
        QuestionTheme.PERSONAL_PREFERENCES, QuestionTheme.CURRENT_UNIVERSITY,
        QuestionTheme.COUNTRY_OF_RESIDENCE, QuestionTheme.CURRENT_STATE,
        QuestionTheme.AGE_ELIGIBILITY, QuestionTheme.TIMELINE_NOTES,
        # Computed from the posting or the stored degree, so a near miss costs
        # a review and never a wrong value (DECISIONS §52 rule 2).
        QuestionTheme.GRADUATION_DATE, QuestionTheme.STUDYING_IN_COUNTRY,
        QuestionTheme.GRADUATE_PROGRAM, QuestionTheme.ENROLLMENT_STATUS,
        QuestionTheme.DEGREE_PROGRAM, QuestionTheme.INTERNSHIP_TERM,
        QuestionTheme.ENGINEERING_TRACK, QuestionTheme.CURRENT_JOB_TITLE,
        QuestionTheme.CURRENT_EMPLOYER, QuestionTheme.VISA_SPONSORSHIP,
        QuestionTheme.WORKED_HERE_BEFORE, QuestionTheme.CURRENT_LOCATION,
        QuestionTheme.OFFICE_PREFERENCE, QuestionTheme.DESIRED_SALARY,
        QuestionTheme.INTERNSHIP_END,
    }
    covered = set(NEAR_MISSES) | exempt
    missing = [theme.value for theme, _pattern in _THEME_PATTERNS if theme not in covered]
    assert not missing, missing


# --- the notice-period menu: a date is not a quantity (audit §2, H4) ----------

NOTICE_PERIOD = _options("Immediate", "2 weeks", "1 month", "2 months", "3+ months")


@pytest.mark.parametrize("value", ["June 2027", "May 2026", "Spring 2028", "06/2026", "04/2027", "2026-06"])
def test_a_stored_date_never_lands_in_a_quantity_bucket(value):
    """"June 2027" against Immediate / 2 weeks / 1 month / 2 months / 3+ months
    parsed as the number 2027 and picked "3+ months" — right by accident. The
    audit's phrase. A date matches no quantity bucket; the field goes to the
    option-translation gate or review with the date intact."""
    assert looks_like_date(value)
    assert match_numeric_bucket(value, NOTICE_PERIOD) is None
    field = FormField(key="n", label="Current Notice Period", kind=FieldKind.SINGLE_SELECT, options=NOTICE_PERIOD)
    assert match_option(value, field) is None


def test_quantities_and_year_buckets_still_match():
    """What must not regress: a GPA in a range, a score past 100, a month-year
    on a year list and on a year range (IMC, §53)."""
    assert match_numeric_bucket("3.8", _options("3.6 or above", "3.0 - 3.5")).label == "3.6 or above"
    assert match_numeric_bucket("1480", _options("1400 - 1490", "1500 - 1600")).label == "1400 - 1490"
    assert not looks_like_date("3.8") and not looks_like_date("2028")
    years = FormField(key="y", label="High school graduation year", kind=FieldKind.SINGLE_SELECT,
                      options=_options("2023", "2024", "2025"))
    assert match_option("May 2024", years).label == "2024"
    ranged = FormField(key="y", label="Expected graduation", kind=FieldKind.SINGLE_SELECT,
                       options=_options("2025 - 2026", "2027 - 2028", "2029 - 2030"))
    assert match_option("May 2028", ranged).label == "2027 - 2028"
    assert match_term_bucket("May 2028", _options("Earlier than Fall 2027", "Spring 2028", "Later than Summer 2028")).label == "Spring 2028"


# --- thousands separators (code audit of 2026-10-12, F32) ---------------------

SALARY_WITH_COMMAS = _options("$40,000 - $55,000", "$55,000 - $70,000", "$70,000+")
SALARY_PLAIN = _options("$40000 - $55000", "$55000 - $70000", "$70000+")
SAT_WITH_COMMAS = _options("1,000 - 1,190", "1,200 - 1,390", "1,400 - 1,490", "1,500 - 1,600")
SAT_PLAIN = _options("1000 - 1190", "1200 - 1390", "1400 - 1490", "1500 - 1600")


@pytest.mark.parametrize("value,expected", [
    ("45000", "$40,000 - $55,000"),       # p20: landed in "$70,000+"
    ("45,000", "$40,000 - $55,000"),
    ("$45,000", "$40,000 - $55,000"),
    ("60000", "$55,000 - $70,000"),
    ("72000", "$70,000+"),
    ("$120,000", "$70,000+"),
    ("1,000,000", "$70,000+"),
])
def test_a_salary_lands_in_its_own_bucket_whether_or_not_the_menu_uses_commas(value, expected):
    """"$70,000+" read as the number 000 "or more", so every salary was the
    top bucket. The separator is noise inside a number, on either side."""
    assert match_numeric_bucket(value, SALARY_WITH_COMMAS).label == expected
    assert match_numeric_bucket(value, SALARY_PLAIN).label == expected.replace(",", "")
    field = FormField(key="s", label="What are your salary expectations?", kind=FieldKind.SINGLE_SELECT,
                      options=SALARY_WITH_COMMAS)
    assert match_option(value, field).label == expected


@pytest.mark.parametrize("value,expected", [
    ("1480", "1,400 - 1,490"),
    ("1,480", "1,400 - 1,490"),
    ("1200", "1,200 - 1,390"),
    ("1600", "1,500 - 1,600"),
])
def test_a_test_score_lands_in_its_own_bucket_whether_or_not_the_menu_uses_commas(value, expected):
    """"1,400 - 1,490" parsed as the range 400–1, which contains nothing."""
    assert match_numeric_bucket(value, SAT_WITH_COMMAS).label == expected
    assert match_numeric_bucket(value, SAT_PLAIN).label == expected.replace(",", "")


@pytest.mark.parametrize("value", ["30000", "30,000", "$39,999"])
def test_a_salary_below_every_bucket_matches_nothing(value):
    assert match_numeric_bucket(value, SALARY_WITH_COMMAS) is None


def test_a_salary_on_a_shared_boundary_is_ambiguous_and_so_review():
    """70,000 is inside "$55,000 - $70,000" and "$70,000+" alike; two
    containing buckets is a choice the applicant makes."""
    assert match_numeric_bucket("70000", SALARY_WITH_COMMAS) is None
    assert match_numeric_bucket("70,000", SALARY_WITH_COMMAS) is None


def test_a_gap_between_buckets_and_the_other_scales_are_left_alone():
    """1,495 sits in no SAT bucket, which is review, not the nearest one; a
    GPA and a percentage still find their own scale."""
    assert match_numeric_bucket("1,495", SAT_WITH_COMMAS) is None
    assert match_numeric_bucket("3.8", _options("3.6 or above", "3.0 - 3.5")).label == "3.6 or above"
    assert match_numeric_bucket("85", _options("80% or above", "70-79%", "3.6 or above")).label == "80% or above"


def test_theme_matching_is_bounded_in_label_length():
    """A 50 KB label is scanned like its first 2,000 characters (audit F10): the
    two history patterns are double lookaheads and were quadratic in length."""
    import time
    from app.autofill.binder import DeterministicBinder
    from app.autofill.jev_binder import _local_theme
    head = "Have you previously interviewed with us? "
    label = head + ("lorem ipsum " * 5000)
    started = time.perf_counter()
    themed = DeterministicBinder().classify_themes([label])[label]
    local = _local_theme(label)
    elapsed = time.perf_counter() - started
    assert themed == DeterministicBinder().classify_themes([head])[head]
    assert local == _local_theme(head)
    assert elapsed < 2.0


# --- R50 live round, item 1: the legal name is the stored name ----------------

def _core_key(label, kind=FieldKind.TEXT):
    from app.autofill.classify import classify, memory_key_for
    return classify("question_1", label, kind).value, memory_key_for("question_1", label, kind)


@pytest.mark.parametrize("label", [
    "Full Legal Name",                                        # xAI, Greenhouse custom question
    "Legal Name",
    "Legal Full Name",
    "Full Name (as it appears on your passport)",
    "Full name as shown on your government ID",
    "Your full legal name",
])
def test_the_legal_name_is_the_stored_full_name(label):
    """xAI asked "Full Legal Name" and the plan went to review at p=0.50 with
    the whole name on file. The stored name *is* the legal one, composed from
    first and last (`Memory.full_name`)."""
    from app.autofill.memory import Memory

    assert _core_key(label) == ("core", "full_name"), label
    assert Memory(facts={"first_name": "Ada", "last_name": "Lovelace"}).lookup("full_name") == "Ada Lovelace"


@pytest.mark.parametrize("label,expected", [
    ("Preferred Name", ("core", "preferred_first_name")),     # not the legal name
    ("Legal First Name", ("core", "first_name")),             # the existing DRW alias
    ("Legal Last Name", ("core", "last_name")),
])
def test_the_other_name_fields_keep_their_own_keys(label, expected):
    assert _core_key(label) == expected, label


@pytest.mark.parametrize("label", [
    "Full Legal Name in Native Language",                     # a different string, not the English name
    "Name of your reference",
    "Reference's full name",
    "Legal Name of Employer",
    "Name of the person who referred you",
])
def test_a_name_that_is_not_the_applicants_is_never_the_stored_name(label):
    """Neither alias nor pattern may reach `full_name` for someone else's name
    or for the name in another script. These go to review or to the model
    gate, which costs one glance; the alternative is the applicant's English
    name typed into a native-language box or a reference slot."""
    assert _core_key(label)[1] != "full_name", label
    assert _core_key(label)[0] != "core", label


# --- R50 live round, item 2: a stored month-year against a month menu ---------

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")


def _month_field(*labels):
    return FormField(key="m", label="Please select the month you will be able to start",
                     kind=FieldKind.SINGLE_SELECT, options=_options(*labels))


@pytest.mark.parametrize("value", ["August 2027", "Aug 2027", "Aug. 2027", "August 1, 2027", "08/2027", "8/2027", "2027-08", "2027-08-01"])
def test_a_stored_month_year_matches_a_bare_month_option_by_its_month(value):
    """"Please select the month you will be able to start" (January … December)
    against a stored earliest start in month-year shape. Full names already
    matched by accident through the reverse-prefix pass; abbreviated and
    numeric shapes did not. The month is the answer, in both spellings."""
    assert match_option(value, _month_field(*MONTHS)).label == "August"
    assert match_option(value, _month_field(*[m[:3] for m in MONTHS])).label == "Aug"


def test_a_stored_month_year_still_matches_a_year_only_menu_by_its_year():
    """The other half, kept from §53: the year alone on a list of years."""
    assert match_option("August 2027", _month_field("2026", "2027", "2028")).label == "2027"
    assert match_option("08/2027", _month_field("2026", "2027", "2028")).label == "2027"


def test_a_month_range_menu_matches_only_when_exactly_one_range_holds_the_month():
    quarters = _month_field("January–March", "April–June", "July–September", "October–December")
    assert match_option("August 2027", quarters).label == "July–September"
    assert match_option("Dec 2027", quarters).label == "October–December"
    assert match_option("Nov 2027", _month_field("November to February", "March to October")).label == "November to February"
    # Two ranges hold May: the applicant's call, not the engine's.
    overlapping = _month_field("January - June", "April - September", "October - December")
    assert match_option("May 2027", overlapping) is None
    assert match_option("August 2027", overlapping).label == "April - September"


@pytest.mark.parametrize("value", ["Summer 2027", "2027", "ASAP", "N/A", "Immediately", "Fall 2027"])
def test_a_value_that_names_no_month_never_selects_a_month(value):
    """A season, a bare year or a non-date carries no month: nothing is
    selected and the field goes to the option-translation gate or review."""
    assert match_option(value, _month_field(*MONTHS)) is None
    assert match_option(value, _month_field("January–March", "April–June", "July–September", "October–December")) is None


def test_a_month_year_menu_is_matched_by_month_and_year_never_by_season():
    """Pre-existing, found while adding the month pass: a stored "Aug 2027"
    against "June 2027 / July 2027 / August 2027" picked June 2027 — the
    term buckets read both sides as "summer 2027" and took the first. Same
    month and year, or nothing."""
    dated = _month_field(*["{} 2027".format(m) for m in MONTHS])
    assert match_option("Aug 2027", dated).label == "August 2027"
    assert match_option("August 1, 2027", dated).label == "August 2027"
    assert match_option("08/2027", dated).label == "August 2027"
    assert match_option("August 2028", dated) is None                       # right month, wrong year
    assert match_option("Aug 2027", _month_field("June 2027", "July 2027", "Other")) is None
    # Season menus keep the bucket arithmetic (DECISIONS: Spring/Summer buckets).
    assert match_option("May 2028", _month_field("Fall 2027", "Spring 2028", "Later than Summer 2028")).label == "Spring 2028"


def test_a_bare_month_option_menu_does_not_catch_other_dated_answers():
    """"Graduation Date" as a text field and a stored date on a non-month
    menu are unchanged: the pass only runs when some option *is* a month."""
    from app.autofill.memory import Memory
    text = FormField(key="g", label="Graduation Date", kind=FieldKind.TEXT, field_class=FieldClass.SCREENING)
    assert text.options == [] and match_option("May 2028", text) is None
    assert match_option("May 2028", _month_field("Yes", "No")) is None


# --- R50 live round, item 3: graduation yes/no computed from the stored date --

def _yes_no(label, kind=FieldKind.SINGLE_SELECT):
    options = _options("Yes", "No") if kind == FieldKind.SINGLE_SELECT else []
    return FormField(key="g", label=label, kind=kind, options=options, field_class=FieldClass.SCREENING)


def _grad(label, memory, kind=FieldKind.SINGLE_SELECT):
    from app.autofill.binder import _graduation_by_date_answer
    return _graduation_by_date_answer(_yes_no(label, kind), memory)


BS_MAY_2028 = {"graduation_date": "May 2028", "degree": "BS Computer Science"}


@pytest.mark.parametrize("label,expected", [
    ("Will you be graduating in 2028?", "Yes"),                                        # Lendable (Ashby)
    ("Will you be graduating in 2027?", "No"),
    ("Are you graduating in December 2027 or May 2028?", "Yes"),
    ("Are you graduating in December 2027 or June 2028?", "No"),
    ("Are you expecting to graduate in 2028 or 2029?", "Yes"),
    ("If you are in a Bachelor's program, do you graduate by June 2027?", "No"),       # Enova
    ("If you are in a Bachelor's program, do you graduate by June 2028?", "Yes"),
    ("Do you graduate before May 2028?", "No"),                                        # strict bound
    ("Will you graduate after January 2028?", "Yes"),
    ("Will you graduate no earlier than 2028?", "Yes"),
    ("Will you graduate between December 2027 and August 2028?", "Yes"),
    ("Will you graduate between June 2028 and December 2028?", "No"),
])
def test_a_graduation_yes_no_that_names_a_date_is_computed_from_the_stored_date(label, expected):
    """Three live boards asked a calendar comparison the model gate was left
    to make. A year, a month-year, a set of them, or a bound: compared with
    the stored graduation date at the granularity the question uses."""
    from app.autofill.memory import Memory

    memory = Memory(education=BS_MAY_2028)
    for kind in (FieldKind.SINGLE_SELECT, FieldKind.BOOLEAN):
        resolution = _grad(label, memory, kind)
        assert resolution is not None and not resolution.needs_review, label
        assert resolution.value == expected, (label, kind)
        assert resolution.reason == "computed: you graduate May 2028"


@pytest.mark.parametrize("label,education,why", [
    # A date the question refers to but never states.
    ("Will you have graduated before the start date?", BS_MAY_2028, "does not state"),
    ("Will you have graduated by the time the internship begins?", BS_MAY_2028, "does not state"),
    # A bare stored year cannot settle a month-level comparison in that year.
    ("Are you graduating in December 2027 or May 2028?", {"graduation_date": "2028"}, "does not settle"),
    ("Do you graduate by June 2028?", {"graduation_date": "2028"}, "does not settle"),
    # Conditional on a degree level the applicant is not in.
    ("If you are in a Bachelor's program, do you graduate by June 2027?",
     {"graduation_date": "May 2028", "degree": "MS Computer Science"}, "bachelor's programme"),
    ("Are you graduating with a Master's degree by 2028?", BS_MAY_2028, "graduate programme"),
    # "before X or after Y": two bounds that do not compose into one test.
    ("Will you graduate before May 2027 or after May 2029?", BS_MAY_2028, "does not settle"),
])
def test_a_graduation_question_the_stored_date_does_not_settle_goes_to_review(label, education, why):
    from app.autofill.memory import Memory

    resolution = _grad(label, Memory(education=education))
    assert resolution is not None and resolution.needs_review and resolution.value is None, label
    assert why in resolution.reason, (label, resolution.reason)


@pytest.mark.parametrize("label", [
    "Have you graduated?",                                      # no date named: the existing paths
    "Did you graduate from high school before 2020?",           # another graduation
    "Did you graduate from a coding bootcamp in 2024?",
    "Will you attend the 2028 graduation ceremony?",
    "Are you enrolled in a graduate program through 2028?",     # the graduate-programme theme's question
])
def test_a_graduation_question_outside_the_rule_is_left_to_the_existing_paths(label):
    from app.autofill.memory import Memory

    assert _grad(label, Memory(education=BS_MAY_2028)) is None, label
    assert _grad(label, Memory(education=BS_MAY_2028), FieldKind.BOOLEAN) is None, label


def test_a_graduation_question_with_no_stored_date_or_no_yes_no_control_is_untouched():
    from app.autofill.binder import _graduation_by_date_answer
    from app.autofill.memory import Memory

    assert _grad("Will you be graduating in 2028?", Memory()) is None
    # Not a yes/no control: a date menu, a three-way select, a text field.
    years = FormField(key="g", label="Will you be graduating in 2028?", kind=FieldKind.SINGLE_SELECT,
                      options=_options("2027", "2028", "2029"), field_class=FieldClass.SCREENING)
    three = FormField(key="g", label="Will you be graduating in 2028?", kind=FieldKind.SINGLE_SELECT,
                      options=_options("Yes", "No", "Not sure"), field_class=FieldClass.SCREENING)
    text = FormField(key="g", label="Graduation Date", kind=FieldKind.TEXT, field_class=FieldClass.SCREENING)
    memory = Memory(education=BS_MAY_2028)
    for field in (years, three, text):
        assert _graduation_by_date_answer(field, memory) is None, field.label


def test_graduation_yes_no_end_to_end_leaves_the_date_fill_alone():
    """The whole plan: the Lendable and Enova shapes fill, "expected graduation
    date" keeps the existing date fill, "Graduation Date" on a text field is
    unchanged, and the unstated-date question is a review, not a date typed
    into a yes/no."""
    from app.autofill.binder import resolve_form
    from app.autofill.memory import Memory
    from app.autofill.schema import FormSchema

    fields = [
        _yes_no("Will you be graduating in 2028?"),
        FormField(key="e", label="If you are in a Bachelor's program, do you graduate by June 2027?",
                  kind=FieldKind.SINGLE_SELECT, options=_options("Yes", "No"), field_class=FieldClass.SCREENING),
        FormField(key="d", label="What is your expected graduation date?", kind=FieldKind.TEXT, field_class=FieldClass.SCREENING),
        FormField(key="t", label="Graduation Date", kind=FieldKind.TEXT, field_class=FieldClass.SCREENING),
        _yes_no("Will you have graduated before the start date?"),
    ]
    for i, f in enumerate(fields):
        f.key = "q{}".format(i)
    plan = resolve_form(FormSchema(source="t", ats="ashby", posting_id="1", fields=fields),
                        Memory(education=BS_MAY_2028), binder=DeterministicBinder())
    by_label = {e.label: e for e in plan.entries}
    assert by_label["Will you be graduating in 2028?"].value == "Yes"
    assert by_label["If you are in a Bachelor's program, do you graduate by June 2027?"].value == "No"
    assert by_label["What is your expected graduation date?"].value == "May 2028"
    assert by_label["Graduation Date"].value == "May 2028"
    unstated = by_label["Will you have graduated before the start date?"]
    assert unstated.needs_review and unstated.value is None and "does not state" in unstated.reason
