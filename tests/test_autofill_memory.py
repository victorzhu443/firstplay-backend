"""
Memory store tests.

Memory is where 67.4% of a real application gets answered, so these cover the
resolution rules rather than the data shape. The cases that matter are the ones
where "no answer" and "the answer is no" must not be confused, and where policy
has to beat whatever happens to be stored.
"""
import json
import os

import pytest

from app.autofill.ashby import parse_ashby_dom
from app.autofill.greenhouse import parse_greenhouse_job
from app.autofill.memory import (
    Memory,
    Story,
    memory_from_resume,
    normalize_company,
    onboarding_questions,
)
from app.autofill.schema import FieldClass, FieldKind, FillSource, FormField
from app.schemas import (
    ExperienceItem,
    ImprovedExperienceItem,
    ImprovedProjectItem,
    ImprovedResumeParsed,
    ResumeParsed,
)

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


@pytest.fixture
def memory():
    return Memory(
        facts={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "phone": "555-0100",
            "linkedin": "https://linkedin.com/in/ada",
            "pronouns": "She/Her",
        },
        preferences={"heard_about": "LinkedIn", "open_to_relocation": "Yes"},
        per_country={
            "work_authorization": {"US": "Yes", "CA": "No"},
            "needs_sponsorship": {"US": "No"},
        },
        protected={"veteran_status": "I am not a protected veteran"},
        employers=["Analytical Engines, Inc.", "Babbage Labs"],
    )


# --- company matching -------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Databricks, Inc.", "databricks"),
        ("Databricks", "databricks"),
        ("Acme Labs, Inc.", "acme"),          # two suffixes, stripped repeatedly
        ("Figma", "figma"),
        ("AT&T", "at&t"),                      # & survives; it is part of the name
        ("", ""),
    ],
)
def test_company_normalisation(raw, expected):
    assert normalize_company(raw) == expected


def test_worked_here_before_is_computed_not_guessed(memory):
    """~109 occurrences across postings, in four phrasings.

    The answer depends on the company being applied to, so it is a lookup
    against the applicant's own history — exact, where a model would guess.
    """
    assert memory.has_worked_at("Analytical Engines") is True
    assert memory.has_worked_at("Analytical Engines, Inc.") is True
    assert memory.has_worked_at("Figma") is False


def test_unknown_employment_history_is_not_the_same_as_no(memory):
    """"I have no history recorded" must never be reported as "no, I have not
    worked there" — that is a claim the applicant did not make."""
    assert Memory().has_worked_at("Figma") is None
    assert memory.has_worked_at(None) is None
    assert memory.has_worked_at("") is None


# --- name parts -------------------------------------------------------------

def test_full_name_is_composed_from_parts(memory):
    """Ashby asks for one Name, Greenhouse for two. Composing is exact;
    splitting a stored full name breaks on the first double surname."""
    assert memory.full_name() == "Ada Lovelace"


def test_full_name_falls_back_when_only_a_composite_is_known():
    assert Memory(facts={"full_name": "Ada Lovelace"}).full_name() == "Ada Lovelace"


# --- the answer log ---------------------------------------------------------

def test_the_same_question_is_never_asked_twice(memory):
    """Fingerprints key on the normalised label, not the ATS field name.

    Greenhouse allocates `question_8581808008` per posting, so keying on the
    field name would forget the answer on every new job.
    """
    memory.remember_answer("Are you authorized to work in the United States?", "Yes")

    assert memory.recall_answer("are you authorized to work in the united states") == "Yes"
    assert memory.recall_answer("Are you authorized to work in the United States?:") == "Yes"


def test_a_remembered_answer_beats_a_stored_fact(memory):
    """A correction made in review is the applicant's own words about this
    exact question, so it outranks a general stored fact."""
    field = FormField(key="q1", label="Pronouns", kind=FieldKind.TEXT,
                      field_class=FieldClass.CORE)

    assert memory.resolve(field).value == "She/Her"

    memory.remember_answer("Pronouns", "they/them")
    assert memory.resolve(field).value == "they/them"


# --- resolution policy ------------------------------------------------------

def test_consent_is_never_resolved_from_memory(anthropic, memory):
    """Policy beats storage: a CONSENT field is not filled even if something
    is stored for it."""
    arbitration = [f for f in anthropic.by_class(FieldClass.CONSENT)
                   if "arbitrat" in f.label.lower()][0]

    resolution = memory.resolve(arbitration)

    assert resolution.source == FillSource.HUMAN
    assert resolution.needs_review
    assert resolution.value is None


def test_protected_characteristics_resolve_from_memory(anthropic, memory):
    """The correction that makes the product worth using: an answer given once
    is replayed, including for veteran status."""
    veteran = [f for f in anthropic.fields if f.key == "veteran_status"][0]
    memory.remember_answer(veteran.label, "I am not a protected veteran")

    resolution = memory.resolve(veteran)

    assert resolution.value == "I am not a protected veteran"
    assert resolution.source == FillSource.MEMORY


def test_a_recognised_field_with_nothing_stored_asks_rather_than_guesses(memory):
    """A gap in onboarding is not a question for a model."""
    field = FormField(key="q1", label="GitHub", kind=FieldKind.TEXT,
                      field_class=FieldClass.CORE)

    resolution = memory.resolve(field)

    assert resolution.value is None
    assert resolution.needs_review
    assert "github" in resolution.reason


def test_an_unrecognised_screening_field_routes_onward(memory):
    """None means "not answerable from memory", which is the signal to hand the
    field to the theme router — not a failure."""
    field = FormField(key="q1", label="What is your notice period?",
                      kind=FieldKind.TEXT, field_class=FieldClass.SCREENING)

    assert memory.resolve(field) is None


def test_memory_never_produces_a_model_sourced_resolution(anthropic, notion, memory):
    """The invariant: memory resolution is replay, never judgement."""
    for form in (anthropic, notion):
        for field in form.fields:
            resolution = memory.resolve(field)
            if resolution is not None:
                assert resolution.source in (FillSource.MEMORY, FillSource.HUMAN)


# --- seeding from the existing pipeline -------------------------------------

def test_memory_is_seeded_from_the_existing_resume_pipeline():
    """`improve_resume` already emits action-verb + tech + metric bullets, so
    node 5 produces the essay evidence pool for free."""
    resume = ResumeParsed(
        name="Ada Lovelace",
        email="ada@example.com",
        phone="555-0100",
        skills=["Python"],
        experience=[ExperienceItem(company="Analytical Engines", title="Engineer")],
    )
    improved = ImprovedResumeParsed(
        name="ADA LOVELACE",
        contact="ada@example.com",
        experience=[ImprovedExperienceItem(
            company="Analytical Engines",
            bullets=["Built a loom compiler in Python, cutting run time 40%"],
        )],
        projects=[ImprovedProjectItem(
            name="Difference Engine",
            technologies=["Brass"],
            bullets=["Computed polynomial tables to 6 decimal places"],
        )],
    )

    memory = memory_from_resume(resume, improved)

    assert memory.facts["first_name"] == "Ada"
    assert memory.facts["last_name"] == "Lovelace"
    assert memory.facts["email"] == "ada@example.com"
    assert memory.employers == ["Analytical Engines"]
    assert memory.has_worked_at("Analytical Engines") is True
    assert len(memory.stories) == 2
    assert any("loom compiler" in s.text for s in memory.stories)


def test_seeding_survives_a_sparse_resume():
    """Student resumes routinely omit a phone or an employer."""
    memory = memory_from_resume(ResumeParsed(name="Ada"))

    assert memory.facts.get("first_name") is None or memory.facts["full_name"] == "Ada"
    assert memory.employers == []
    assert memory.stories == []


# --- the onboarding screen, derived from data -------------------------------

def test_onboarding_questions_are_ranked_by_recurrence(anthropic, notion):
    """What to ask on day one, derived from postings rather than guessed."""
    ranked = onboarding_questions([anthropic, notion], min_occurrences=1)

    assert ranked
    assert ranked == sorted(ranked, key=lambda row: (-row[1], row[0]))

    labels = [row[0] for row in ranked]
    assert any("veteran" in label for label in labels)


def test_onboarding_only_lists_questions_memory_alone_can_answer(anthropic, notion):
    """A question needing a model is not an onboarding question."""
    for label, _count, field_class in onboarding_questions([anthropic, notion],
                                                           min_occurrences=1):
        assert field_class in ("core", "document", "legal"), (label, field_class)


def test_onboarding_groups_variant_spellings_into_one_question(anthropic):
    """"Website", "Website(s)" and "Websites" are one stored fact, so the
    screen asks for a website once. "Other Website" has its own key and stays
    separate, correctly."""
    from app.autofill.classify import memory_key_for

    assert memory_key_for("q", "Website") == memory_key_for("q", "Website(s)")
    assert memory_key_for("q", "Other Website") != memory_key_for("q", "Website")


def test_resume_upload_and_paste_are_one_onboarding_question(anthropic):
    """Greenhouse's "Resume/CV" ships `resume` and `resume_text` — two fill
    values from one document. Listing it twice would ask for the same file
    twice."""
    ranked = onboarding_questions([anthropic], min_occurrences=1)
    resume_rows = [row for row in ranked if "resume" in row[0]]

    assert len(resume_rows) == 1, resume_rows
