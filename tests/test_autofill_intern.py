"""
SWE-intern-specific behaviour.

The general corpus (150 postings, mostly senior and sales roles) and the intern
corpus (57 SWE-intern postings) ask materially different questions, and the
differences are not cosmetic:

    screening   39.9% intern  vs  26.6% general   (interns are gated harder)
    narrative    2.9% intern  vs   5.9% general   (fewer essays)

Everything here covers something the general corpus never exposed.
"""
import json
import os

import pytest

from app.autofill.ashby import parse_ashby_dom
from app.autofill.binder import DeterministicBinder, resolve_form
from app.autofill.classify import classify, memory_key_for, normalize_label
from app.autofill.format import match_option
from app.autofill.greenhouse import parse_greenhouse_job
from app.autofill.memory import (
    PROFILE_FIELDS,
    Memory,
    load_memory,
    memory_from_resume,
    profile_template,
    save_memory,
)
from app.autofill.schema import (
    FieldClass,
    FieldKind,
    FieldOption,
    FillSource,
    FormField,
    FormSchema,
)
from app.autofill.themes import QuestionTheme
from app.schemas import EducationItem, ExperienceItem, ResumeParsed

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "forms")


def _fixture(name):
    with open(os.path.join(FIXTURE_DIR, name)) as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def anthropic():
    return parse_greenhouse_job(_fixture("greenhouse_anthropic_ae.json"), board="anthropic")


@pytest.fixture(scope="module")
def notion():
    return parse_ashby_dom(_fixture("ashby_notion_swe.json"))


def _select(label, *options, **kw):
    return FormField(
        key=kw.get("key", "q1"), label=label, kind=FieldKind.SINGLE_SELECT,
        field_class=kw.get("field_class", FieldClass.SCREENING),
        options=[FieldOption(label=o, value=str(i)) for i, o in enumerate(options)],
    )


# --- the decline-variant bug -------------------------------------------------
#
# Greenhouse writes "I don't wish to answer"; Ashby writes "Decline to
# self-identify". Before the fix, whatever the applicant stored matched one
# vendor and silently went to review on the other — and "Prefer not to say",
# the phrasing most people would actually type, missed both.

@pytest.mark.parametrize(
    "stored",
    [
        "Decline to self-identify",
        "I don't wish to answer",
        "Prefer not to say",
        "I would rather not answer",
        "I do not want to answer",
    ],
)
def test_declining_matches_however_the_form_words_it(stored, anthropic, notion):
    greenhouse_veteran = [f for f in anthropic.fields if f.key == "veteran_status"][0]
    ashby_race = [f for f in notion.fields
                  if f.key.endswith("_systemfield_eeoc_race")][0]

    assert match_option(stored, greenhouse_veteran) is not None, "Greenhouse"
    assert match_option(stored, ashby_race) is not None, "Ashby"


def test_the_decline_class_is_matched_after_normalisation():
    """`normalize_value` strips apostrophes, so "don't" arrives as "don t". A
    pattern written against the raw text would never fire."""
    from app.autofill.format import normalize_value

    assert normalize_value("I don't wish to answer") == "i don t wish to answer"


def test_a_real_answer_is_not_swallowed_by_the_decline_class(anthropic):
    veteran = [f for f in anthropic.fields if f.key == "veteran_status"][0]

    matched = match_option("I am not a protected veteran", veteran)

    assert matched is not None
    assert "not a protected veteran" in matched.label


def test_an_exact_match_wins_even_beside_another_decline_option():
    """The stored text is literally one of the options, so there is nothing
    ambiguous about it."""
    field = _select("Status", "Decline to answer", "Prefer not to say", "Yes")

    assert match_option("Prefer not to say", field).label == "Prefer not to say"


def test_two_decline_options_with_no_exact_match_is_left_to_the_applicant():
    """Ambiguity produces a review, never a coin flip."""
    field = _select("Status", "Decline to answer", "Prefer not to say", "Yes")

    assert match_option("I would rather not answer", field) is None


# --- education: the largest intern-specific cluster --------------------------

@pytest.mark.parametrize(
    "label,expected",
    [
        ("What is your anticipated graduation month and year?", QuestionTheme.GRADUATION_DATE),
        ("When do you graduate?", QuestionTheme.GRADUATION_DATE),
        ("What is your GPA?", QuestionTheme.GPA),
        ("Undergraduate GPA", QuestionTheme.GPA),
        ("Current University", QuestionTheme.CURRENT_UNIVERSITY),
        ("Are you currently enrolled in a degree programme?", QuestionTheme.ENROLLMENT_STATUS),
        ("Do you prefer a winter or summer internship?", QuestionTheme.INTERNSHIP_TERM),
        ("Which type of engineering work are you most excited to do?",
         QuestionTheme.ENGINEERING_TRACK),
        ("Zip / postal code", QuestionTheme.POSTAL_CODE),
        ("Please select the country where you currently reside.",
         QuestionTheme.COUNTRY_OF_RESIDENCE),
        ("Are you at least 18 years of age?", QuestionTheme.AGE_ELIGIBILITY),
        ("Who is your current or previous employer?", QuestionTheme.CURRENT_EMPLOYER),
        ("What is your current or previous job title?", QuestionTheme.CURRENT_JOB_TITLE),
    ],
)
def test_intern_theme_patterns(label, expected):
    """Real labels from the 57-posting intern corpus, with their occurrence
    counts in the module docstring of themes.py."""
    theme, _confidence = DeterministicBinder().classify_themes([label])[label]

    assert theme == expected


def test_education_is_seeded_from_the_resume_pipeline():
    """ResumeParsed.education already carries exactly what intern forms ask for,
    so none of it has to be retyped."""
    resume = ResumeParsed(
        name="Ada Lovelace",
        education=[EducationItem(institution="Cornell University",
                                 degree="BS Computer Science",
                                 graduation_date="May 2028", gpa="3.8")],
        experience=[ExperienceItem(company="Analytical Engines",
                                   title="Software Engineering Intern")],
    )

    memory = memory_from_resume(resume)

    assert memory.education["university"] == "Cornell University"
    assert memory.education["graduation_date"] == "May 2028"
    assert memory.education["gpa"] == "3.8"
    assert memory.facts["current_employer"] == "Analytical Engines"
    assert memory.facts["current_job_title"] == "Software Engineering Intern"


def test_education_resolves_a_graduation_question():
    memory = Memory(education={"graduation_date": "May 2028"})
    field = FormField(key="q1", label="What is your anticipated graduation month and year?",
                      kind=FieldKind.TEXT, field_class=FieldClass.SCREENING)

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=[field]),
        memory, binder=DeterministicBinder())

    assert plan.entries[0].value == "May 2028"
    assert plan.entries[0].source == FillSource.MEMORY


# --- legal status ------------------------------------------------------------

def test_one_general_answer_serves_every_country():
    """Most applicants have a single status and should not have to enumerate
    countries to get a form filled."""
    memory = Memory(legal_status={"work_authorization": "Yes"})
    field = _select("Are you legally authorized to work in the United States?", "Yes", "No")

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=[field]),
        memory, binder=DeterministicBinder())

    assert plan.entries[0].value == "Yes"


def test_a_per_country_answer_overrides_the_general_one():
    """Convenience must never override the specific."""
    memory = Memory(
        legal_status={"work_authorization": "Yes"},
        per_country={"work_authorization": {"CA": "No"}},
    )
    field = _select("Are you legally entitled to work in Canada?", "Yes", "No")

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=[field]),
        memory, binder=DeterministicBinder())

    assert plan.entries[0].value == "No"


# --- safety reclassifications the intern corpus exposed ----------------------

@pytest.mark.parametrize(
    "label",
    [
        "Please describe any need for a reasonable accommodation for the interview",
        "Can you perform these essential functions of the job with or without accommodation?",
        "Are you a current government official or were you a government official?",
        "Do you have any Personal/Familial Relationships with a current employee?",
        "To your knowledge, do you or a close relative currently hold shares?",
    ],
)
def test_stable_facts_are_answered_once_and_replayed(label):
    """Accommodation needs and conflict-of-interest answers are facts about the
    applicant, not per-application acts.

    An earlier version made these permanently manual, which cost ~70 fields of
    coverage across 57 intern postings and asked the same questions on every
    single application. They are LEGAL instead: the applicant's own answer is
    replayed, and no model may ever produce one.
    """
    field_class = classify("question_1", label, FieldKind.TEXT)

    assert field_class == FieldClass.LEGAL

    field = FormField(key="q1", label=label, kind=FieldKind.TEXT, field_class=field_class)
    assert field.may_fill_from(FillSource.MEMORY)
    assert field.is_autofillable()
    assert field.needs_onboarding_answer()
    assert not field.allows_model_judgement()


@pytest.mark.parametrize(
    "label",
    [
        "I certify that the facts set forth in this Application for Employment are true",
        "I attest that the information provided is accurate",
        "I understand that any false statement may result in dismissal",
    ],
)
def test_per_application_attestations_stay_manual(label):
    """Certifying is an act *made* at submission, not a fact reported about the
    applicant — the same distinction that keeps arbitration agreements manual.
    There is no prior fact to replay."""
    field_class = classify("question_1", label, FieldKind.TEXT)

    assert field_class == FieldClass.CONSENT

    field = FormField(key="q1", label=label, kind=FieldKind.TEXT, field_class=field_class)
    assert not field.is_autofillable()
    assert field.may_fill_from(FillSource.HUMAN)


def test_memory_only_questions_resolve_from_the_profile():
    """The point of the reclassification: answered once, filled thereafter."""
    memory = Memory(protected={"government_official": "No",
                               "accommodation_needs": "None"})

    field = FormField(key="q1", label="Are you a current government official?",
                      kind=FieldKind.TEXT,
                      field_class=classify("q1", "Are you a current government official?",
                                           FieldKind.TEXT))
    resolution = memory.resolve(field)

    assert resolution.value == "No"
    assert resolution.source == FillSource.MEMORY


def test_a_none_answer_leaves_the_field_blank_rather_than_typing_none():
    """"None" recorded for an optional free-text field is a decision to leave
    it blank. Typing the word "None" into an accommodation box is worse than
    an empty one, and 8 fields across the corpus were about to get it."""
    memory = Memory(protected={"accommodation_needs": "None"})
    label = "Please describe any need for a reasonable accommodation"
    field = FormField(key="q1", label=label, kind=FieldKind.TEXT,
                      field_class=classify("q1", label, FieldKind.TEXT))

    resolution = memory.resolve(field)

    assert resolution.value is None
    assert resolution.skipped
    assert not resolution.needs_review


def test_a_negative_answer_is_not_mistaken_for_a_blank():
    """"No" is a real answer to a yes/no question. Collapsing it into "blank"
    would silently drop a legitimate negative."""
    memory = Memory(protected={"government_official": "No"})
    field = FormField(key="q1", label="Are you a current government official?",
                      kind=FieldKind.TEXT, field_class=FieldClass.LEGAL)

    assert memory.resolve(field).value == "No"


def test_a_required_field_with_no_stored_answer_still_surfaces():
    """Silently blanking a required field would block submission."""
    memory = Memory(facts={"website": "None"})
    field = FormField(key="q1", label="Website", kind=FieldKind.TEXT,
                      field_class=FieldClass.CORE, required=True)

    resolution = memory.resolve(field)

    assert resolution.needs_review
    assert "requires it" in resolution.reason


def test_employer_ai_tool_notices_are_left_alone():
    """The intern corpus phrases these differently from the general corpus."""
    for label in ["I understand that Coinbase may use AI tools to assist in the review",
                  "Which of the following best describes how you use AI tools?"]:
        assert classify("q1", label, FieldKind.SINGLE_SELECT, option_count=3) == (
            FieldClass.CONSENT
        )


# --- the combined links field -----------------------------------------------

def test_a_combined_links_field_has_its_own_key():
    """x8 in the intern corpus. Mapped separately from `linkedin`, because the
    applicant may want something different in a combined field."""
    label = "LinkedIn Profile, Github, Personal Website, or Portfolio"

    assert normalize_label(label) == "linkedin profile github personal website or portfolio"
    assert memory_key_for("q1", label) == "links_combined"
    assert memory_key_for("q1", "LinkedIn Profile") == "linkedin"


# --- the profile file --------------------------------------------------------

def test_the_template_lists_every_key_the_resolver_knows():
    template = profile_template()

    for section, entries in PROFILE_FIELDS.items():
        for name, *_ in entries:
            assert name in template[section], (section, name)


def test_a_profile_round_trips_and_is_owner_only(tmp_path):
    """0600 because this file holds protected characteristics and a phone
    number."""
    path = str(tmp_path / "profile.json")
    memory = Memory(
        facts={"first_name": "Ada"},
        education={"gpa": "3.8"},
        legal_status={"work_authorization": "Yes"},
        protected={"veteran_status": "Prefer not to say"},
    )

    save_memory(memory, path)
    reloaded = load_memory(path)

    assert reloaded.facts["first_name"] == "Ada"
    assert reloaded.education["gpa"] == "3.8"
    assert reloaded.legal_status["work_authorization"] == "Yes"
    assert oct(os.stat(path).st_mode)[-3:] == "600"


def test_a_missing_profile_is_an_empty_memory_not_an_error(tmp_path):
    """First run, before onboarding. Nothing fills, nothing crashes."""
    memory = load_memory(str(tmp_path / "absent.json"))

    assert memory.facts == {}
    assert memory.lookup("email") is None


def test_the_default_profile_path_is_outside_the_repository():
    """A profile inside the working tree is one `git add .` from publication."""
    from app.autofill.memory import DEFAULT_PROFILE_PATH

    assert DEFAULT_PROFILE_PATH.startswith(os.path.expanduser("~"))
    assert "firstplay-backend" not in DEFAULT_PROFILE_PATH


# --- conditional follow-ups --------------------------------------------------

def _yes_no(label, key):
    return _select(label, "Yes", "No", key=key)


def test_a_follow_up_is_not_applicable_when_the_question_above_said_no():
    """x20 across 57 postings, and the largest label no theme can ever cover —
    its meaning lives in the previous field, not its own words.

    Most applicants answer No to the conflict-of-interest screens these hang
    off, so the follow-up is answered rather than outstanding.
    """
    memory = Memory(protected={"government_official": "No"})
    fields = [
        _yes_no("Are you a current government official?", "q1"),
        FormField(key="q2", kind=FieldKind.TEXT, field_class=FieldClass.SCREENING,
                  label='If you answered "Yes" to the above question, please provide details'),
    ]

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=fields),
        memory, binder=DeterministicBinder())

    follow_up = plan.entries[1]
    assert follow_up.satisfied_by == "q1"
    assert not follow_up.needs_review
    assert "not applicable" in follow_up.reason


def test_a_follow_up_after_yes_is_left_for_the_applicant():
    """One-directional on purpose. The detail wanted is free prose only the
    applicant can supply, and inventing a disclosure is the worst precision
    failure available."""
    memory = Memory(protected={"government_official": "Yes"})
    fields = [
        _yes_no("Are you a current government official?", "q1"),
        FormField(key="q2", kind=FieldKind.TEXT, field_class=FieldClass.SCREENING,
                  label='If you answered "Yes" to the above question, please provide details'),
    ]

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=fields),
        memory, binder=DeterministicBinder())

    follow_up = plan.entries[1]
    assert follow_up.needs_review
    assert follow_up.value is None
    assert follow_up.satisfied_by is None


def test_a_follow_up_with_nothing_resolved_above_is_left_alone():
    """No preceding answer means no basis to call it inapplicable."""
    fields = [
        _yes_no("Some unanswerable question?", "q1"),
        FormField(key="q2", kind=FieldKind.TEXT, field_class=FieldClass.SCREENING,
                  label='If you answered "Yes" above, please explain'),
    ]

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=fields),
        Memory(), binder=DeterministicBinder())

    assert plan.entries[1].needs_review


@pytest.mark.parametrize(
    "stored,expected",
    [
        ("3019063249", "301-906-3249"),
        ("301-906-3249", "301-906-3249"),
        ("(301) 906-3249", "301-906-3249"),
        ("+1 301 906 3249", "301-906-3249"),
        ("301.906.3249", "301-906-3249"),
        ("+44 20 7946 0958", "+44 20 7946 0958"),
        ("", ""),
        (None, None),
    ],
)
def test_phone_is_normalised_at_fill_time(stored, expected):
    """So the stored format is whatever was convenient to type.

    A number that is not a recognisable 10-digit US one passes through
    unchanged — international numbers have their own conventions and guessing
    at them is worse than leaving them alone.
    """
    from app.autofill.format import format_phone

    assert format_phone(stored) == expected


def test_a_phone_field_emits_the_canonical_form():
    memory = Memory(facts={"phone": "3019063249"})
    field = FormField(key="phone", label="Phone", kind=FieldKind.TEXT,
                      field_class=FieldClass.CORE)

    assert memory.resolve(field).value == "301-906-3249"


# --- the first precision failure found, and its regression -------------------

@pytest.mark.parametrize(
    "degree,expected",
    [
        ("BS Computer Science", "No"),
        ("Bachelor of Science, Computer Engineering", "No"),
        ("MS Computer Science", "Yes"),
        ("PhD in Machine Learning", "Yes"),
    ],
)
def test_graduate_programme_is_computed_from_the_stored_degree(degree, expected):
    """A generic "Currently Enrolled" was being matched onto "Are you enrolled
    in a Master's or PhD Program?", producing a confident wrong "Yes" for an
    undergraduate at 0.92.

    The degree answers it exactly, so it is computed rather than matched.
    """
    memory = Memory(education={"degree": degree, "enrollment_status": "Currently Enrolled"})
    field = _select("Are you currently enrolled in a Master's or PhD Program?", "Yes", "No")

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=[field]),
        memory, binder=DeterministicBinder())

    assert plan.entries[0].value == expected
    assert plan.entries[0].source == FillSource.MEMORY


def test_an_ambiguous_degree_is_left_for_review():
    """Neither marker, or both. Better a review than a coin flip on a
    qualification claim."""
    memory = Memory(education={"degree": "Computer Science"})
    field = _select("Are you enrolled in a PhD program?", "Yes", "No")

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=[field]),
        memory, binder=DeterministicBinder())

    assert plan.entries[0].needs_review


def test_generic_enrolment_still_answers_the_generic_question():
    """The narrower theme must not swallow the broad one."""
    memory = Memory(education={"enrollment_status": "Currently Enrolled"})
    field = FormField(key="q1", label="Are you currently enrolled as a student?",
                      kind=FieldKind.TEXT, field_class=FieldClass.SCREENING)

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=[field]),
        memory, binder=DeterministicBinder())

    assert plan.entries[0].value == "Currently Enrolled"


# --- shrinking the risky path ------------------------------------------------
#
# Every model-decided fill is a case the deterministic layer could not answer
# precisely. 20 of 43 turned out to be two fixable shapes, and moving them off
# the model path is worth more than a better model guarding it: a lookup cannot
# misinterpret, and a judgement fails confidently.

@pytest.mark.parametrize(
    "graduation,expected",
    [
        ("May 2028", "Spring/Summer 2028"),
        ("Spring 2028", "Spring/Summer 2028"),
        ("June 2028", "Spring/Summer 2028"),
        ("October 2027", "Fall/Winter 2027"),
    ],
)
def test_compound_term_options_match_deterministically(graduation, expected):
    """"Spring/Summer 2028" covers two terms in one option, and intern forms use
    that constantly. Parsed as the single term "Summer 2028" it missed a spring
    graduation — 9 of 43 model-decided fills were this one shape."""
    from app.autofill.buckets import match_term_bucket

    field = _select("When do you graduate?", "Fall/Winter 2027",
                    "Spring/Summer 2028", "Fall/Winter 2028")

    assert match_term_bucket(graduation, field.options).label == expected


@pytest.mark.parametrize(
    "stored,expected",
    [
        ("United States", "US"),
        ("USA", "US"),
        ("united states of america", "US"),
        ("England", "United Kingdom"),
        ("Great Britain", "United Kingdom"),
    ],
)
def test_country_aliases_match_deterministically(stored, expected):
    """Forms list "US", "USA" and "United States" interchangeably."""
    from app.autofill.format import match_country

    field = _select("Country of residence", "US", "Canada", "United Kingdom")

    assert match_country(stored, field).label == expected


def test_an_unlisted_country_is_left_for_review():
    from app.autofill.format import match_country

    field = _select("Country of residence", "US", "Canada")

    assert match_country("France", field) is None


# --- decisions that are not facts --------------------------------------------

@pytest.mark.parametrize(
    "label",
    [
        "To ensure we can focus fully on our conversation, we use Brighthire to record",
        "Do you opt-in to receive WhatsApp messages from Stripe Recruiting?",
        "[Compensation] Do you accept the listed salary range for this role?",
    ],
)
def test_granting_something_is_a_decision_not_a_stored_fact(label):
    """Each of these grants something — a recording, a marketing channel,
    acceptance of terms. None is a fact about the applicant to replay, so none
    is auto-filled however confidently a theme could be assigned."""
    assert classify("q1", label, FieldKind.SINGLE_SELECT, option_count=2) == FieldClass.CONSENT


def test_transcripts_resolve_to_the_stored_academic_record():
    """x8 across the corpus in three wordings."""
    for label in ["Undergraduate Transcript", "Graduate Transcript",
                  "Please upload your academic record document"]:
        assert memory_key_for("q1", label) == "transcript_file", label


def test_studying_in_country_is_computed_not_matched():
    """"Are you currently attending a university in Canada?" answered from a
    generic "Currently Enrolled" is the same generic-onto-specific error that
    produced a wrong Masters/PhD answer."""
    memory = Memory(education={"university_country": "United States",
                               "enrollment_status": "Currently Enrolled"})

    canada = _select("Are you currently attending a university in Canada?", "Yes", "No")
    usa = _select("Are you currently attending a university in the United States?",
                  "Yes", "No")

    for field, expected in ((canada, "No"), (usa, "Yes")):
        plan = resolve_form(
            FormSchema(source="t", ats="t", posting_id="1", fields=[field]),
            memory, binder=DeterministicBinder())
        assert plan.entries[0].value == expected, field.label
        assert plan.entries[0].source == FillSource.MEMORY


def test_studying_in_country_needs_the_stored_country():
    """Never guessed from the institution name — a wrong answer here is a false
    statement about where the applicant studies."""
    memory = Memory(education={"university": "Cornell University"})
    field = _select("Are you currently attending a university in Canada?", "Yes", "No")

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=[field]),
        memory, binder=DeterministicBinder())

    assert plan.entries[0].needs_review


# --- country determination ---------------------------------------------------

@pytest.mark.parametrize(
    "location,expected",
    [
        ("San Mateo, CA United States", "US"),
        ("San Francisco, CA", "US"),
        ("Menlo Park, CA; New York, NY", "US"),
        ("Toronto, Canada", "CA"),
        ("Montreal, Canada", "CA"),
        ("Mexico City, Mexico", None),
        ("San Francisco - SF9", None),
    ],
)
def test_country_from_a_posting_location(location, expected):
    """Greenhouse locations name a state far more often than a country, and
    "CA" is both California and the ISO code for Canada — so explicit country
    names are checked first and a state code only after."""
    from app.autofill.themes import country_in_text

    assert country_in_text(location) == expected


def test_a_general_answer_applies_when_no_country_is_named():
    """"Are you legally authorized to work in the country in which this job is
    based?" names no country, and the posting location often names only a city.
    An early return on that left two of the most common questions in the corpus
    unanswered on every application."""
    memory = Memory(legal_status={"work_authorization": "Yes",
                                  "needs_sponsorship": "No"})
    fields = [
        _select("Are you currently legally authorized to work in the country in "
                "which this job is based?", "Yes", "No", key="q1"),
        _select("Will you now or in the future require sponsorship?", "Yes", "No",
                key="q2"),
    ]

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=fields),
        memory, binder=DeterministicBinder())

    assert [e.value for e in plan.entries] == ["Yes", "No"]


def test_conflicting_per_country_answers_block_the_general_fallback():
    """If eligibility genuinely differs by country, "which country?" is
    load-bearing and guessing would be a false statement about work
    eligibility."""
    memory = Memory(
        legal_status={"work_authorization": "Yes"},
        per_country={"work_authorization": {"US": "Yes", "CA": "No"}},
    )
    field = _select("Are you authorized to work in the country for which you "
                    "applied?", "Yes", "No")

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=[field]),
        memory, binder=DeterministicBinder())

    assert plan.entries[0].needs_review
    assert "differs by country" in plan.entries[0].reason


# --- a conditional preference ------------------------------------------------
#
# "I'd work any term; if forced, winter when remote and summer otherwise."
# No form in the corpus offers "no preference" — all 9 term questions force a
# choice — so the stated ideal cannot be expressed and the fallback rule is
# what actually gets used.

def _term_form(remote, *options):
    return FormSchema(
        source="t", ats="t", posting_id="1", remote=remote,
        fields=[_select("Do you prefer a winter or summer internship?", *options)],
    )


def test_term_preference_defaults_to_summer():
    memory = Memory(preferences={"internship_term": "Summer",
                                 "internship_term_if_remote": "Winter"})

    plan = resolve_form(_term_form(False, "Winter 2027", "Summer 2027"),
                        memory, binder=DeterministicBinder())

    assert plan.entries[0].value == "Summer 2027"
    assert plan.entries[0].source == FillSource.MEMORY


def test_a_remote_posting_flips_the_term_preference():
    memory = Memory(preferences={"internship_term": "Summer",
                                 "internship_term_if_remote": "Winter"})

    plan = resolve_form(_term_form(True, "Winter 2027", "Summer 2027"),
                        memory, binder=DeterministicBinder())

    assert plan.entries[0].value == "Winter 2027"
    assert "remote" in plan.entries[0].reason


def test_the_conditional_is_one_key_not_a_rules_engine():
    """Only remoteness is conditioned on. It was true of 5 of 57 postings, none
    of which asked this question — so a general mechanism would be built for a
    branch that never fires. A suboptimal *preference* is also a choice to
    revisit in review, not a false statement, which is why a conditional is
    acceptable here and would not be for work authorisation."""
    memory = Memory(preferences={"internship_term": "Summer"})

    plan = resolve_form(_term_form(True, "Winter 2027", "Summer 2027"),
                        memory, binder=DeterministicBinder())

    assert plan.entries[0].value == "Summer 2027"


@pytest.mark.parametrize(
    "label",
    [
        "I confirm my availability for a Summer 2027 (May/June starts) internship",
        "Do you have flexibility to consider a second cohort option?",
    ],
)
def test_availability_confirmation_is_not_a_term_preference(label):
    """These name a season but ask yes/no. Routed to the term preference they
    were answered with "Summer", which matches neither option."""
    theme, _ = DeterministicBinder().classify_themes([label])[label]

    assert theme == QuestionTheme.INTERNSHIP_AVAILABILITY


def test_being_flexible_answers_an_availability_confirmation():
    memory = Memory(preferences={"term_flexible": "Yes"})
    field = _select("I confirm my availability for a Summer 2027 internship",
                    "Yes", "No")

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=[field]),
        memory, binder=DeterministicBinder())

    assert plan.entries[0].value == "Yes"


def test_remote_is_read_from_the_posting():
    from app.autofill.greenhouse import parse_greenhouse_job

    remote = parse_greenhouse_job({"id": 1, "offices": [{"name": "US - Remote Zone 1"}]})
    onsite = parse_greenhouse_job({"id": 2, "location": {"name": "San Francisco, CA"}})
    unknown = parse_greenhouse_job({"id": 3})

    assert remote.remote is True
    assert onsite.remote is False
    assert unknown.remote is None


# --- "any term works", where a form allows saying it -------------------------

def test_a_flexible_applicant_selects_every_workable_cohort():
    """Stripe's cohort question is a MULTI-select, which is the only place in
    the corpus where "any term works" is expressible. Ticking all the real
    options says exactly that."""
    memory = Memory(preferences={"term_flexible": "Yes"})
    field = FormField(
        key="q1", kind=FieldKind.MULTI_SELECT, field_class=FieldClass.SCREENING,
        label="Internship Information: please select the cohort dates that work best for you.",
        options=[FieldOption(label=l, value=str(i)) for i, l in enumerate([
            "January to June (6 months)", "May to July (12 weeks)",
            "June to August (12 weeks)", "None of these options work for me"])],
    )

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=[field]),
        memory, binder=DeterministicBinder())

    entry = plan.entries[0]
    assert entry.values == ["January to June (6 months)", "May to July (12 weeks)",
                            "June to August (12 weeks)"]
    assert "None of these options work for me" not in entry.values
    assert not entry.needs_review


def test_an_inflexible_applicant_is_not_given_every_cohort():
    memory = Memory(preferences={"internship_term": "Summer"})
    field = FormField(
        key="q1", kind=FieldKind.MULTI_SELECT, field_class=FieldClass.SCREENING,
        label="Please select the cohort dates that work best for you.",
        options=[FieldOption(label=l, value=str(i)) for i, l in enumerate([
            "May to July (12 weeks)", "None of these options work for me"])],
    )

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=[field]),
        memory, binder=DeterministicBinder())

    assert plan.entries[0].values == []


def test_a_legal_multi_select_is_unreachable_by_the_model_gate():
    """The gate may match one stored value to one option on a multi-select —
    the same shape and the same risk as a single-select. What it must never
    reach is a disclosure.

    Verified against the corpus: both multi-select disclosures classify LEGAL,
    and `may_fill_from(MODEL_DECISION)` is false for LEGAL. So the guard is the
    fill-source policy itself rather than a special case about field kind,
    which is the more durable place for it.
    """
    from app.autofill.classify import classify

    for label in ["Please confirm whether any of the below applies to you.",
                  "If you selected a response to the prior question other than \u201cnone of "
                  "the above,\u201d please confirm whether any of the following also applies "
                  "to you."]:
        field_class = classify("q1", label, FieldKind.MULTI_SELECT, option_count=4)
        field = FormField(
            key="q1", label=label, kind=FieldKind.MULTI_SELECT,
            field_class=field_class,
            options=[FieldOption(label="A", value="0"), FieldOption(label="B", value="1")],
        )
        assert field_class == FieldClass.LEGAL, label
        assert not field.may_fill_from(FillSource.MODEL_DECISION), label


def test_the_model_gate_never_produces_a_set_of_options():
    """Selecting several options is only ever a deterministic decision — the
    cohort case, driven by a stated preference for flexibility."""
    import inspect

    from app.autofill.binder import _resolve_option_mismatches

    assert "entry.values" not in inspect.getsource(_resolve_option_mismatches)


def test_pronouns_match_across_form_wordings():
    """Figma writes "he/him/his"; Lyft writes "He / Him". Requiring an exact
    match left 13 fields unanswered across the corpus."""
    from app.autofill.format import match_option

    lyft = FormField(key="q", label="Please share your gender pronouns.",
                     kind=FieldKind.MULTI_SELECT,
                     options=[FieldOption(label=l, value=str(i)) for i, l in enumerate(
                         ["She / Her", "He / Him", "They / Them", "Ae / Aer"])])

    assert match_option("he/him/his", lyft).label == "He / Him"
    assert match_option("they/them/theirs", lyft).label == "They / Them"


def test_a_stored_pronoun_fills_a_multi_select():
    memory = Memory(facts={"pronouns": "he/him/his"})
    field = FormField(key="q1", label="Please share your gender pronouns.",
                      kind=FieldKind.MULTI_SELECT, field_class=FieldClass.CORE,
                      options=[FieldOption(label=l, value=str(i)) for i, l in enumerate(
                          ["She / Her", "He / Him", "They / Them"])])

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=[field]),
        memory, binder=DeterministicBinder())

    assert plan.entries[0].values == ["He / Him"]
    assert not plan.entries[0].needs_review
