"""
Binding tests: theme classification, option matching, and the FillPlan.

The bias throughout is that ambiguity must produce a review, never a guess. A
needless review costs one glance; a confident wrong selection on work
authorisation is a false statement on an application.
"""
import json
import os

import pytest

from app.autofill.ashby import parse_ashby_dom
from app.autofill.binder import (
    AUTOFILL_CONFIDENCE,
    DeterministicBinder,
    resolve_form,
)
from app.autofill.format import match_option, normalize_value, option_for_bool
from app.autofill.greenhouse import parse_greenhouse_job
from app.autofill.memory import Memory
from app.autofill.schema import FieldClass, FieldKind, FieldOption, FillSource, FormField
from app.autofill.themes import QuestionTheme, country_in_label

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "forms")


def _fixture(name):
    with open(os.path.join(FIXTURE_DIR, name)) as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def anthropic():
    return parse_greenhouse_job(_fixture("greenhouse_anthropic_ae.json"), board="anthropic")


@pytest.fixture(scope="module")
def figma():
    return parse_greenhouse_job(_fixture("greenhouse_figma_ae.json"), board="figma")


@pytest.fixture(scope="module")
def notion():
    return parse_ashby_dom(_fixture("ashby_notion_swe.json"))


@pytest.fixture
def memory():
    return Memory(
        facts={
            "first_name": "Ada", "last_name": "Lovelace",
            "email": "ada@example.com", "phone": "555-0100",
            "linkedin": "https://linkedin.com/in/ada",
            "current_location": "London, UK", "current_state": "California",
        },
        preferences={
            "heard_about": "LinkedIn", "open_to_relocation": "Yes",
            "in_office_tolerance": "Yes", "earliest_start": "Two weeks",
        },
        per_country={
            "work_authorization": {"US": "Yes", "CA": "No"},
            "needs_sponsorship": {"US": "No"},
        },
        employers=["Analytical Engines, Inc."],
    )


def _select(label, *options, **kw):
    return FormField(
        key=kw.get("key", "q1"), label=label, kind=FieldKind.SINGLE_SELECT,
        field_class=kw.get("field_class", FieldClass.SCREENING),
        options=[FieldOption(label=o, value=str(i)) for i, o in enumerate(options)],
    )


# --- option matching --------------------------------------------------------

def test_exact_option_match():
    field = _select("Authorized?", "Yes", "No")
    assert match_option("Yes", field).label == "Yes"


def test_stored_yes_matches_a_longer_option_label():
    """Real forms write "Yes, I am authorized to work in the US" where memory
    holds a bare "Yes"."""
    field = _select("Authorized?", "Yes, I am authorized to work in the US", "No")
    assert match_option("Yes", field).label.startswith("Yes")


def test_ambiguous_value_matches_nothing_rather_than_guessing():
    """Two options both mean yes. Picking either would be a coin flip on a
    statement the applicant is making about themselves."""
    field = _select("Status", "Yes, currently", "Yes, previously", "No")
    assert match_option("Yes", field) is None


def test_option_matching_needs_options_and_a_value():
    assert match_option("Yes", _select("Q")) is None
    assert match_option(None, _select("Q", "Yes", "No")) is None


def test_boolean_becomes_this_forms_option():
    field = _select("Worked here before?", "Yes", "No")
    assert option_for_bool(True, field).label == "Yes"
    assert option_for_bool(False, field).label == "No"


def test_value_normalisation_keeps_technical_characters():
    assert normalize_value("C++") == "c++"
    assert normalize_value("  Yes,  really ") == "yes really"


# --- country parameterisation ------------------------------------------------

@pytest.mark.parametrize(
    "label,expected",
    [
        ("Are you legally authorized to work in the United States?", "US"),
        ("Are you legally entitled to work in Canada?", "CA"),
        ("Do you require visa sponsorship?", None),
    ],
)
def test_country_is_read_from_the_question(label, expected):
    """US and Canada variants both appear in the corpus, with different
    answers. One stored boolean would answer one of them wrongly."""
    assert country_in_label(label) == expected


def test_work_authorisation_is_answered_per_country(memory):
    binder = DeterministicBinder()

    us = _select("Are you legally authorized to work in the United States?", "Yes", "No")
    ca = _select("Are you legally entitled to work in Canada?", "Yes", "No")

    from app.autofill.schema import FormSchema
    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", fields=[us, ca]),
        memory, binder=binder,
    )

    by_label = {e.label: e for e in plan.entries}
    assert by_label[us.label].value == "Yes"
    assert by_label[ca.label].value == "No"


# --- computed themes --------------------------------------------------------

def test_worked_here_before_is_computed_from_history(memory):
    from app.autofill.schema import FormSchema

    field = _select("Have you ever worked for Analytical Engines before?", "Yes", "No")
    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1",
                   company="Analytical Engines", fields=[field]),
        memory, binder=DeterministicBinder(),
    )

    entry = plan.entries[0]
    assert entry.value == "Yes"
    assert entry.source == FillSource.MEMORY
    assert "employment history" in entry.reason
    assert not entry.needs_review


def test_worked_here_before_with_no_history_asks(memory):
    from app.autofill.schema import FormSchema

    field = _select("Have you ever worked for Figma before?", "Yes", "No")
    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", company="Figma", fields=[field]),
        Memory(), binder=DeterministicBinder(),
    )

    assert plan.entries[0].needs_review


# --- theme pattern precision -------------------------------------------------

@pytest.mark.parametrize(
    "label,expected",
    [
        ("Do you require visa sponsorship?", QuestionTheme.VISA_SPONSORSHIP),
        ("Are you legally authorized to work in the United States?",
         QuestionTheme.WORK_AUTHORIZATION),
        ("Have you ever interviewed at Anthropic before?",
         QuestionTheme.INTERVIEWED_HERE_BEFORE),
        ("Have you ever worked for Figma before, as an employee or a contractor?",
         QuestionTheme.WORKED_HERE_BEFORE),
        ("Which state or province do you currently live in?", QuestionTheme.CURRENT_STATE),
        ("Are you open to relocation for this role?", QuestionTheme.WILLING_TO_RELOCATE),
        ("Are you open to working in-person in one of our offices 25% of the time?",
         QuestionTheme.IN_OFFICE_TOLERANCE),
        ("How did you hear about this job?", QuestionTheme.HEARD_ABOUT),
        ("When is the earliest you would want to start working with us?",
         QuestionTheme.EARLIEST_START),
        ("Do you have any deadlines or timeline considerations we should be aware of?",
         QuestionTheme.TIMELINE_NOTES),
        ("(Optional) Personal Preferences", QuestionTheme.PERSONAL_PREFERENCES),
        ("What is your favourite programming language?", QuestionTheme.UNKNOWN),
    ],
)
def test_theme_patterns_land_on_the_right_theme(label, expected):
    """Real labels from the measured corpus. Precision matters more than
    coverage: an unmatched label becomes a review, a mismatched one becomes a
    wrong answer."""
    theme, _confidence = DeterministicBinder().classify_themes([label])[label]
    assert theme == expected


def test_sponsorship_outranks_authorisation():
    """"Will you require sponsorship to work here?" contains both patterns.
    Sponsorship is checked first because it is the narrower claim."""
    label = "Will you now or in the future require sponsorship to work in the US?"
    theme, _ = DeterministicBinder().classify_themes([label])[label]
    assert theme == QuestionTheme.VISA_SPONSORSHIP


# --- the whole plan ---------------------------------------------------------

def test_plan_covers_every_field_in_exactly_one_state(anthropic, memory):
    """Three states, not two: filled, satisfied by a sibling, or needs review.

    "Satisfied" exists because Greenhouse offers "Resume/CV" as both a file
    upload and a textarea — supplying either answers the question, so the unused
    control is neither filled nor outstanding.
    """
    plan = resolve_form(anthropic, memory, binder=DeterministicBinder())
    summary = plan.summary()

    assert len(plan.entries) == len(anthropic.fields)
    assert summary["total"] == len(anthropic.fields)
    assert (summary["filled"] + summary["satisfied"] + summary["attach"]
            + summary["review"] == len(anthropic.fields))

    # The three sets are disjoint.
    groups = (plan.filled(), plan.satisfied(), plan.to_attach(), plan.for_review())
    keys = [{e.field_key for e in group} for group in groups]
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            assert not (keys[i] & keys[j])


def test_no_field_is_ever_silently_blank(anthropic, figma, notion, memory):
    """Every field is filled, explicitly satisfied by a sibling, or flagged —
    always with a reason. A silently empty field is one nobody notices going
    unanswered."""
    for form in (anthropic, figma, notion):
        for entry in resolve_form(form, memory, binder=DeterministicBinder()).entries:
            accounted = (entry.value is not None
                         or entry.needs_review
                         or entry.satisfied_by is not None
                         or entry.attach is not None
                         or entry.skipped is not None)   # a stated decision to leave it blank
            assert accounted, entry.label
            assert entry.reason, entry.label


def test_the_invariant_holds_across_the_whole_plan(anthropic, figma, notion, memory):
    """No model judgement ever produces a value for a protected or consent
    field — the rule the entire safety argument rests on."""
    for form in (anthropic, figma, notion):
        classes = {f.key: f.field_class for f in form.fields}
        for entry in resolve_form(form, memory, binder=DeterministicBinder()).entries:
            if classes.get(entry.field_key) in (FieldClass.LEGAL, FieldClass.CONSENT):
                assert entry.source != FillSource.MODEL_DECISION
                assert entry.source != FillSource.MODEL_DRAFT


def test_consent_is_always_left_for_the_applicant(anthropic, memory):
    plan = resolve_form(anthropic, memory, binder=DeterministicBinder())
    consent_keys = {f.key for f in anthropic.by_class(FieldClass.CONSENT)}

    for entry in plan.entries:
        if entry.field_key in consent_keys:
            assert entry.needs_review
            assert entry.value is None


def test_without_a_binder_unmatched_fields_go_to_review(anthropic, memory):
    """Gate 2 off: memory still answers what it can, and nothing is guessed."""
    plan = resolve_form(anthropic, memory, binder=None)

    assert plan.summary()["filled"] > 0
    assert all(e.theme is None for e in plan.entries)


# --- protected answers are replayed, not re-asked ---------------------------

def test_protected_fields_resolve_from_the_protected_memory_section(anthropic):
    """Without this wiring, 212 fields across 149 postings went to review on
    every application — the exact chore the product exists to remove."""
    memory = Memory(protected={
        "veteran_status": "I am not a protected veteran",
        "race_ethnicity": "Decline to self-identify",
        "gender": "Decline to self-identify",
    })

    plan = resolve_form(anthropic, memory, binder=DeterministicBinder())
    by_key = {e.field_key: e for e in plan.entries}

    veteran = by_key["veteran_status"]
    assert veteran.value == "I am not a protected veteran"
    assert veteran.source == FillSource.MEMORY
    assert not veteran.needs_review


def test_ashby_protected_fields_resolve_through_their_suffix(notion):
    """Ashby suffixes the marker onto a per-posting UUID, so exact-key matching
    alone would never find them."""
    memory = Memory(protected={"race_ethnicity": "Decline to self-identify"})

    plan = resolve_form(notion, memory, binder=DeterministicBinder())
    race = [e for e in plan.entries if e.field_key.endswith("_systemfield_eeoc_race")]

    assert race and race[0].value == "Decline to self-identify"
    assert race[0].source == FillSource.MEMORY


def test_replaying_a_protected_answer_is_still_barred_to_any_model(anthropic):
    """Filling from memory and permitting inference are different things."""
    veteran = [f for f in anthropic.fields if f.key == "veteran_status"][0]

    assert veteran.may_fill_from(FillSource.MEMORY)
    assert not veteran.allows_model_judgement()


@pytest.mark.parametrize(
    "label",
    [
        "Have you ever worked for Figma before, as an employee or a contractor?",
        "Do you currently or have you previously worked for Databricks?",
        "Are you currently, or have you previously, worked for Instacart?",
    ],
)
def test_worked_here_before_matches_either_word_order(label):
    theme, _ = DeterministicBinder().classify_themes([label])[label]
    assert theme == QuestionTheme.WORKED_HERE_BEFORE


def test_in_office_question_is_not_mistaken_for_prior_employment():
    """"open to working in-person" contains a work word but is a different
    question; "worked" is past tense so it does not match."""
    label = "Are you open to working in-person in one of our offices 25% of the time?"
    theme, _ = DeterministicBinder().classify_themes([label])[label]
    assert theme == QuestionTheme.IN_OFFICE_TOLERANCE


def test_authorisation_falls_back_to_the_postings_country():
    """28 postings ask "authorized to work in the country for which you
    applied" and name no country, so the posting's location is the only source."""
    from app.autofill.schema import FormSchema

    memory = Memory(per_country={"work_authorization": {"US": "Yes", "CA": "No"}})
    field = _select("Are you authorized to work in the country for which you applied?",
                    "Yes", "No")

    us_plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", country="US", fields=[field]),
        memory, binder=DeterministicBinder())
    ca_plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="2", country="CA", fields=[field]),
        memory, binder=DeterministicBinder())

    assert us_plan.entries[0].value == "Yes"
    assert ca_plan.entries[0].value == "No"


def test_a_country_named_in_the_question_beats_the_postings_country():
    """A US-based posting can still ask about Canadian entitlement."""
    from app.autofill.schema import FormSchema

    memory = Memory(per_country={"work_authorization": {"US": "Yes", "CA": "No"}})
    field = _select("Are you legally entitled to work in Canada?", "Yes", "No")

    plan = resolve_form(
        FormSchema(source="t", ats="t", posting_id="1", country="US", fields=[field]),
        memory, binder=DeterministicBinder())

    assert plan.entries[0].value == "No"


def test_greenhouse_country_is_derived_from_the_posting(anthropic):
    assert anthropic.country in ("US", None)


def test_a_file_upload_is_an_attachment_not_a_fill(anthropic, memory):
    """Browsers forbid setting input[type=file] from a script. Reporting the
    résumé as "filled" would draw a green outline over an empty upload."""
    memory.facts["resume_file"] = "/tmp/resume.pdf"
    plan = resolve_form(anthropic, memory, binder=DeterministicBinder())
    resume = [e for e in plan.entries if e.field_key == "resume"][0]

    assert resume.attach == "/tmp/resume.pdf"
    assert resume not in plan.filled()
    assert resume in plan.to_attach()
    assert resume not in plan.for_review()


def test_an_attachment_still_satisfies_its_paste_alternative(anthropic, memory):
    """Greenhouse pairs the upload with a `resume_text` textarea; the attachment
    answers the question, so the textarea is a sibling, not a gap."""
    memory.facts["resume_file"] = "/tmp/resume.pdf"
    plan = resolve_form(anthropic, memory, binder=DeterministicBinder())
    paste = [e for e in plan.entries if e.field_key == "resume_text"][0]

    assert paste.satisfied_by == "resume"


def test_blank_labels_are_never_grouped_as_one_question():
    """On a real Ashby form a filled gender radio group "satisfied" a bare
    file input, two unlabelled checkboxes, the pronoun radios and the
    reCAPTCHA textarea, because all of them share the empty label."""
    from app.autofill.schema import FormSchema
    memory = Memory(protected={"gender": "Male"})
    gender = FormField(key="x__systemfield_eeoc_gender", label="", kind=FieldKind.SINGLE_SELECT,
                       field_class=FieldClass.LEGAL,
                       options=[FieldOption(label="Male", value="1"), FieldOption(label="Female", value="2")])
    captcha = FormField(key="g-recaptcha-response", label="", kind=FieldKind.LONG_TEXT,
                        field_class=FieldClass.UNKNOWN)
    plan = resolve_form(FormSchema(source="t", ats="t", posting_id="1", fields=[gender, captcha]),
                        memory, binder=None)
    by_key = {e.field_key: e for e in plan.entries}
    assert by_key["x__systemfield_eeoc_gender"].value == "Male"
    assert by_key["g-recaptcha-response"].satisfied_by is None


def test_a_checkbox_labelled_linkedin_is_not_a_url_field():
    """A "how did you hear about us" checkbox, not a profile link. Aiming a URL
    at it is a misclassification even though the fill would fail."""
    from app.autofill.classify import classify, memory_key_for
    assert classify("LinkedIn", "LinkedIn", FieldKind.BOOLEAN) != FieldClass.CORE
    assert memory_key_for("LinkedIn", "LinkedIn", FieldKind.BOOLEAN) is None
    assert memory_key_for("q1", "LinkedIn", FieldKind.TEXT) == "linkedin"


def test_computed_answer_outside_the_options_is_never_marked_filled():
    """Lyft 8802198002, 2026-09-27: "Work Authorization" with sentence options.

    The stored answer is "Yes"; the form offers "I am authorized to work for
    any employer…" and two alternatives. "Yes" is not selectable, so the entry
    must not claim FILL — it goes to the option gate as a mismatch.
    """
    from app.autofill.binder import _resolve_computed, _within_options
    from app.autofill.memory import Memory
    from app.autofill.schema import FieldClass, FieldKind, FieldOption, FormField
    from app.autofill.themes import QuestionTheme
    from app.autofill.format import OPTION_MISMATCH

    field = FormField(
        key="question_38294760002", label="Work Authorization",
        kind=FieldKind.SINGLE_SELECT, field_class=FieldClass.SCREENING, required=True,
        options=[
            FieldOption(label="I am authorized to work for any employer in the country in which this position is based.", value="1"),
            FieldOption(label="I require/will require Lyft's sponsorship to obtain work authorization", value="2"),
            FieldOption(label="My status to work in the country in which this position is based is unknown.", value="3"),
        ],
    )
    memory = Memory(legal_status={"work_authorization": "Yes"})

    # Since round 1 of §48, polarity is read from the opening words, so the one
    # option that opens affirmatively ("I am authorized…") is selected exactly.
    raw = _resolve_computed(QuestionTheme.WORK_AUTHORIZATION, field, memory, "Lyft", "US")
    assert raw is not None and raw.value == field.options[0].label and not raw.needs_review
    assert _within_options(raw, field).needs_review is False

    # Two affirmative options ("for any employer" / "for my present employer
    # only") carry the same polarity, so nothing is unique and the guard holds.
    field.options.append(FieldOption(label="I am authorized to work for my present employer only.", value="4"))
    raw = _resolve_computed(QuestionTheme.WORK_AUTHORIZATION, field, memory, "Lyft", "US")
    assert raw is not None and raw.value == "Yes" and not raw.needs_review  # the defect

    guarded = _within_options(raw, field)
    assert guarded.needs_review and guarded.reason == OPTION_MISMATCH
    assert guarded.value == "Yes"  # kept, so the model gate can translate it


def test_inapplicable_follow_up_selects_the_forms_not_applicable_option():
    """Duolingo 8805925002: sponsorship "No" -> "If so, are you eligible for OPT?"

    The follow-up is required and offers Yes / No / NA. Leaving it blank (the
    old behaviour) blocked submission; NA is the true answer.
    """
    from app.autofill.binder import FillPlanEntry, _resolve_conditionals
    from app.autofill.schema import FieldClass, FieldKind, FieldOption, FormField, FormSchema, FillSource

    yn = [FieldOption(label="Yes", value="1"), FieldOption(label="No", value="0")]
    form = FormSchema(source="t", ats="greenhouse", posting_id="1", fields=[
        FormField(key="q1", label="Will you now, or in the future, require sponsorship?",
                  kind=FieldKind.SINGLE_SELECT, field_class=FieldClass.SCREENING, required=True, options=yn),
        FormField(key="q2", label="If so, are you eligible or currently in a period of OPT?",
                  kind=FieldKind.SINGLE_SELECT, field_class=FieldClass.SCREENING, required=True,
                  options=yn + [FieldOption(label="NA", value="2")]),
        FormField(key="q3", label='If you answered "Yes" to the above question, please provide details.',
                  kind=FieldKind.LONG_TEXT, field_class=FieldClass.SCREENING, required=False, options=[]),
    ])
    entries = [
        FillPlanEntry(field_key="q1", label=form.fields[0].label, value="No", source=FillSource.MEMORY),
        FillPlanEntry(field_key="q2", label=form.fields[1].label, value=None, source=FillSource.HUMAN, needs_review=True),
        FillPlanEntry(field_key="q3", label=form.fields[2].label, value=None, source=FillSource.HUMAN, needs_review=True),
    ]

    _resolve_conditionals(entries, form)

    assert entries[1].value == "NA" and not entries[1].needs_review and entries[1].satisfied_by is None
    # No such option on the free-text follow-up: blank stays the answer, and it
    # hangs off the question immediately above it (q2, now "NA").
    assert entries[2].value is None and entries[2].satisfied_by == "q2"


def test_profile_answer_gate_fills_only_confident_screening_choices():
    """The gate is last, bounded to the form's options, and barred from LEGAL."""
    from app.autofill.binder import FillPlanEntry, _answer_from_profile
    from app.autofill.memory import Memory
    from app.autofill.schema import FieldClass, FieldKind, FieldOption, FormField, FormSchema, FillSource

    opts = [FieldOption(label="Yes", value="1"), FieldOption(label="No", value="0"), FieldOption(label="NA", value="2")]
    form = FormSchema(source="t", ats="greenhouse", posting_id="1", fields=[
        FormField(key="opt2", label="After the OPT, are you eligible for a 24-month OPT extension?",
                  kind=FieldKind.SINGLE_SELECT, field_class=FieldClass.SCREENING, required=True, options=opts),
        FormField(key="vet", label="Veteran status", kind=FieldKind.SINGLE_SELECT,
                  field_class=FieldClass.LEGAL, required=False, options=opts),
        FormField(key="track", label="Which track?", kind=FieldKind.SINGLE_SELECT,
                  field_class=FieldClass.SCREENING, required=False, options=opts),
    ])
    entries = [FillPlanEntry(field_key=f.key, label=f.label, value=None, source=FillSource.HUMAN, needs_review=True)
               for f in form.fields]

    class FakeBinder:
        seen = None
        def answer_from_profile(self, state, requests):
            FakeBinder.seen = (state, [r for r, _f in requests])
            return {"ans_0": ("NA", 0.97), "ans_2": ("Yes", 0.6)}

    memory = Memory(legal_status={"citizenship": "US Citizen", "needs_sponsorship": "No"},
                    protected={"veteran_status": "I am not a protected veteran"})
    _answer_from_profile(entries, form, memory, FakeBinder())

    state, asked = FakeBinder.seen
    assert asked == ["ans_0", "ans_2"]                      # the LEGAL field was never asked
    assert "protected" not in json.dumps(state)              # and protected answers never sent
    assert state["applicant"]["legal_status"]["citizenship"] == "US Citizen"

    assert entries[0].value == "NA" and not entries[0].needs_review
    assert entries[0].source == FillSource.MODEL_DECISION and entries[0].confidence == 0.97
    assert entries[1].needs_review and entries[1].value is None
    assert entries[2].needs_review and entries[2].value == "Yes" and "60%" in entries[2].reason


def test_applicant_state_states_degree_level_and_never_protected_data():
    from app.autofill.binder import _applicant_state
    from app.autofill.memory import Memory

    state = _applicant_state(Memory(education={"degree": "BS Computer Science"},
                                    protected={"gender": "Male"}, facts={"email": "x@y.z"}))
    assert any("Bachelor's Degree only" in n and "Master's" in n for n in state["notes"])
    assert "Male" not in json.dumps(state) and "x@y.z" not in json.dumps(state)


def test_a_required_follow_up_without_a_not_applicable_option_keeps_its_own_answer():
    """Xaira 5225658007: "If you answered 'No' ... will you require sponsorship?"

    Required, options Yes / No only. The sponsorship theme had already answered
    "No" from the stored fact; marking it "answered by a sibling" left it blank
    and the form refused to submit. A confident answer of its own stands.
    """
    from app.autofill.binder import FillPlanEntry, _resolve_conditionals
    from app.autofill.schema import FieldClass, FieldKind, FieldOption, FormField, FormSchema, FillSource

    yn = [FieldOption(label="Yes", value="1"), FieldOption(label="No", value="0")]
    form = FormSchema(source="t", ats="greenhouse", posting_id="1", fields=[
        FormField(key="q1", label="Are you legally authorized to work in the country for this position?",
                  kind=FieldKind.SINGLE_SELECT, field_class=FieldClass.SCREENING, required=True, options=yn),
        FormField(key="q2", label="If you answered 'No' to the previous question, will you require sponsorship for a work visa?",
                  kind=FieldKind.SINGLE_SELECT, field_class=FieldClass.SCREENING, required=True, options=yn),
    ])
    entries = [
        FillPlanEntry(field_key="q1", label=form.fields[0].label, value="Yes", source=FillSource.MEMORY),
        FillPlanEntry(field_key="q2", label=form.fields[1].label, value="No", source=FillSource.MEMORY, confidence=1.0),
    ]

    _resolve_conditionals(entries, form)

    assert entries[1].value == "No"
    assert entries[1].satisfied_by is None and not entries[1].needs_review
    assert "required" in entries[1].reason


def test_a_required_follow_up_with_nothing_to_say_goes_to_review_not_blank():
    """Compeer 5404994008: a required "If yes, please explain" text field.

    The applicant answered No above, so there is nothing to explain — but the
    form still refuses a blank. It must be outlined for the applicant, not
    hidden as answered.
    """
    from app.autofill.binder import FillPlanEntry, _resolve_conditionals
    from app.autofill.schema import FieldClass, FieldKind, FieldOption, FormField, FormSchema, FillSource

    yn = [FieldOption(label="Yes", value="1"), FieldOption(label="No", value="0")]
    form = FormSchema(source="t", ats="greenhouse", posting_id="1", fields=[
        FormField(key="q1", label="Do you have any relatives employed here?",
                  kind=FieldKind.SINGLE_SELECT, field_class=FieldClass.SCREENING, required=True, options=yn),
        FormField(key="q2", label="If yes, please explain.",
                  kind=FieldKind.LONG_TEXT, field_class=FieldClass.SCREENING, required=True, options=[]),
        FormField(key="q3", label="If yes, please provide details.",
                  kind=FieldKind.LONG_TEXT, field_class=FieldClass.SCREENING, required=False, options=[]),
    ])
    entries = [
        FillPlanEntry(field_key="q1", label=form.fields[0].label, value="No", source=FillSource.MEMORY),
        FillPlanEntry(field_key="q2", label=form.fields[1].label, value=None, source=FillSource.HUMAN, needs_review=True),
        FillPlanEntry(field_key="q3", label=form.fields[2].label, value=None, source=FillSource.HUMAN, needs_review=True),
    ]

    _resolve_conditionals(entries, form)

    assert entries[1].needs_review and entries[1].satisfied_by is None
    # The optional one is still simply inapplicable.
    assert entries[2].satisfied_by == "q1" and not entries[2].needs_review


def test_profile_gate_asks_multi_selects_and_fills_only_a_decided_set():
    """General Matter 5377118008: "When are you available … Check all that apply"."""
    from app.autofill.binder import FillPlanEntry, _answer_from_profile, MULTI_SELECT_OPTION_CAP
    from app.autofill.memory import Memory
    from app.autofill.schema import FieldClass, FieldKind, FieldOption, FormField, FormSchema, FillSource

    terms = [FieldOption(label=t, value=t) for t in ("Fall 2026", "Spring 2027", "Summer 2027")]
    offices = [FieldOption(label="Office {}".format(i), value=str(i)) for i in range(MULTI_SELECT_OPTION_CAP + 1)]
    form = FormSchema(source="t", ats="greenhouse", posting_id="1", fields=[
        FormField(key="terms", label="When are you available for a 12 week internship? Check all that apply",
                  kind=FieldKind.MULTI_SELECT, field_class=FieldClass.SCREENING, required=True, options=terms),
        FormField(key="offices", label="Please select office location(s) you are open to working in:",
                  kind=FieldKind.MULTI_SELECT, field_class=FieldClass.SCREENING, required=True, options=offices),
        FormField(key="clear", label="Active Security Clearance(s)",
                  kind=FieldKind.MULTI_SELECT, field_class=FieldClass.SCREENING, required=True, options=terms),
    ])
    entries = [FillPlanEntry(field_key=f.key, label=f.label, value=None, source=FillSource.HUMAN, needs_review=True)
               for f in form.fields]

    class FakeBinder:
        asked = None
        def answer_from_profile(self, state, requests):
            FakeBinder.asked = [r for r, _f in requests]
            return {"ans_0": (["Summer 2027"], 0.96), "ans_2": (["Fall 2026", "Spring 2027"], 0.62)}

    memory = Memory(preferences={"internship_term": "Summer", "earliest_start": "June 2027"})
    _answer_from_profile(entries, form, memory, FakeBinder())

    assert FakeBinder.asked == ["ans_0", "ans_2"]           # 13 offices exceed the cap; never asked
    assert entries[0].values == ["Summer 2027"] and entries[0].value is None
    assert entries[0].source == FillSource.MODEL_DECISION and not entries[0].needs_review
    assert entries[1].needs_review and not entries[1].values
    # Partial certainty is a suggestion the applicant sees, never a fill.
    assert entries[2].needs_review and entries[2].values == ["Fall 2026", "Spring 2027"]
    assert "62%" in entries[2].reason


def test_applicant_state_carries_standing_availability_and_whitelisted_background():
    from datetime import date
    from app.autofill.binder import _applicant_state, _class_standing
    from app.autofill.memory import Memory

    memory = Memory(
        education={"start_date": "August 2024", "graduation_date": "May 2028", "gpa": "3.8"},
        preferences={"internship_term": "Summer", "term_flexible": "No", "earliest_start": "June 2027"},
        facts={"security_clearance": "None", "prior_internships": "1", "email": "x@y.z",
               "phone": "555", "street_address": "1 Main St"},
        protected={"gender": "Male"},
    )
    state = _applicant_state(memory, today=date(2026, 9, 28))

    assert state["today"] == "2026-09-28"
    assert any("third-year (junior)" in n and "May 2028" in n for n in state["notes"])
    assert any("Summer terms only" in n and "June 2027" in n and "Spring = January" in n for n in state["notes"])
    assert state["background"] == {"security_clearance": "None", "prior_internships": "1"}
    blob = json.dumps(state)
    assert "x@y.z" not in blob and "555" not in blob and "1 Main St" not in blob and "Male" not in blob

    # Standing from the graduation date alone assumes four years; graduation
    # in the past reads as graduated; a flexible applicant is told so.
    assert _class_standing({"graduation_date": "May 2028"}, date(2026, 9, 28)) == "third-year (junior)"
    assert _class_standing({"graduation_date": "May 2027"}, date(2027, 2, 1)) == "fourth-year (senior)"
    assert _class_standing({"graduation_date": "May 2026"}, date(2026, 9, 28)).startswith("graduated")
    assert _class_standing({}, date(2026, 9, 28)) is None
    flexible = _applicant_state(Memory(preferences={"internship_term": "Summer", "term_flexible": "Yes"}),
                                today=date(2026, 9, 28))
    assert any("on or after the earliest start date" in n for n in flexible["notes"])


def test_the_engine_fingerprint_is_stable_and_short():
    from app.autofill import ENGINE, engine_fingerprint
    assert ENGINE == engine_fingerprint() and len(ENGINE) == 12 and int(ENGINE, 16) >= 0


def test_the_answer_gate_is_asked_alongside_classification_and_not_again():
    """A first-visit plan was 0.7–0.95 s of sequential Jev round trips; the
    answer gate now runs concurrently with classification, and fields it
    already answered are not asked a second time."""
    import threading
    from app.autofill.binder import resolve_form
    from app.autofill.memory import Memory
    from app.autofill.schema import FieldClass, FieldKind, FieldOption, FormField, FormSchema, FillSource
    from app.autofill.themes import QuestionTheme

    yn = [FieldOption(label="Yes", value="1"), FieldOption(label="No", value="0")]
    form = FormSchema(source="t", ats="greenhouse", posting_id="1", fields=[
        FormField(key="q1", label="Are you seeking a Spring internship?", kind=FieldKind.SINGLE_SELECT,
                  field_class=FieldClass.SCREENING, required=True, options=yn),
        FormField(key="q2", label="Tell us about yourself", kind=FieldKind.LONG_TEXT,
                  field_class=FieldClass.NARRATIVE, required=True, options=[]),
    ])

    class FakeBinder:
        calls = []
        def classify_themes(self, labels):
            FakeBinder.calls.append(("classify", threading.get_ident(), tuple(labels)))
            return {l: (QuestionTheme.UNKNOWN, 0.0) for l in labels}
        def answer_from_profile(self, state, requests):
            FakeBinder.calls.append(("answer", threading.get_ident(), tuple(r for r, _f in requests)))
            return {r: ("No", 0.95) for r, _f in requests}

    plan = resolve_form(form, Memory(preferences={"internship_term": "Summer"}), binder=FakeBinder())

    kinds = [c[0] for c in FakeBinder.calls]
    assert kinds.count("answer") == 1 and kinds.count("classify") == 1
    answer_call = next(c for c in FakeBinder.calls if c[0] == "answer")
    classify_call = next(c for c in FakeBinder.calls if c[0] == "classify")
    assert answer_call[1] != classify_call[1]                 # ran on another thread, i.e. concurrently
    assert answer_call[2] == ("spec_q1",)                      # only the answerable field was asked
    q1 = next(e for e in plan.entries if e.field_key == "q1")
    assert q1.value == "No" and q1.source == FillSource.MODEL_DECISION and not q1.needs_review


def test_a_yes_no_control_never_receives_a_non_yes_no_value():
    """Color Health (Ashby): "Are you based in the San Francisco Bay Area?" is a
    checkbox; the location theme produced "Ithaca, NY" and the filler refused."""
    from app.autofill.binder import Resolution, _within_options
    from app.autofill.schema import FieldClass, FieldKind, FormField, FillSource

    question = FormField(key="q1", label="Are you based in the San Francisco Bay Area?", kind=FieldKind.BOOLEAN,
                         field_class=FieldClass.SCREENING, required=True, options=[])
    guarded = _within_options(Resolution(field_key="q1", value="Ithaca, NY", source=FillSource.MEMORY), question)
    assert guarded.value is None and guarded.needs_review and "yes/no" in guarded.reason

    kept = _within_options(Resolution(field_key="q1", value="No", source=FillSource.MEMORY), question)
    assert kept.value == "No" and not kept.needs_review
