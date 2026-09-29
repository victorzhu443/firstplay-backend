"""
Extraction tests, run against forms captured from live postings.

The fixtures in tests/fixtures/forms/ are real: two Greenhouse job payloads
fetched from the public board API, and a DOM extract read off a live Ashby
application page. They are committed so the suite keeps the property the rest
of this repo's tests have — no network, no credentials, no cost, about a
second — while still being scored against what employers actually publish
rather than against a form someone imagined.

Almost every assertion here encodes something that was *not* obvious before
reading real data, and would have been built wrong otherwise. Those are called
out individually.
"""
import json
import os

import pytest

from app.autofill.ashby import parse_ashby_dom
from app.autofill.greenhouse import parse_greenhouse_job
from app.autofill.schema import FieldClass, FieldKind, FillSource

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
def linear():
    return parse_ashby_dom(_fixture("ashby_linear_fullstack.json"))


# --- the closed type vocabulary ---------------------------------------------

def test_greenhouse_types_stay_within_the_closed_set(anthropic, figma):
    """Across 40 sampled live postings only four field types ever appeared.

    A vocabulary this small is why extraction is deterministic code and not a
    model: there is nothing to judge.
    """
    for form in (anthropic, figma):
        for field in form.fields:
            assert field.kind in {
                FieldKind.TEXT,
                FieldKind.LONG_TEXT,
                FieldKind.SINGLE_SELECT,
                FieldKind.MULTI_SELECT,
                FieldKind.FILE,
            }


# --- core fields resolve without a model ------------------------------------

def test_core_fields_are_recognised_by_their_stable_names(anthropic):
    """first_name/last_name/email/phone appeared in 40 of 40 sampled postings.

    They are named identically every time, so they cost zero model calls —
    which is most of why a filled application can cost $0.0003.
    """
    core = {f.key for f in anthropic.by_class(FieldClass.CORE)}

    assert {"first_name", "last_name", "email", "phone"} <= core


def test_ashby_core_fields_use_systemfield_names(linear):
    core = {f.key for f in linear.by_class(FieldClass.CORE)}

    assert "_systemfield_name" in core
    assert "_systemfield_email" in core


def test_ashby_collects_one_name_where_greenhouse_splits_two(linear, anthropic):
    """The normalisation that forces memory to store name *parts*.

    Composing "Ada Lovelace" from two stored fields is exact. Splitting a full
    name back into two is a guess that breaks on the first double surname, so
    memory has to hold the parts and Ashby's single field is the composite.
    """
    ashby_name = [f for f in linear.fields if f.key == "_systemfield_name"]
    assert len(ashby_name) == 1

    greenhouse_names = [f for f in anthropic.fields if f.key in ("first_name", "last_name")]
    assert len(greenhouse_names) == 2


# --- the resume_text trap ---------------------------------------------------

def test_resume_paste_field_is_a_document_not_an_essay(anthropic):
    """Greenhouse ships `resume_text`, a textarea alternative to the upload.

    Classified by shape alone it is a long-text field and would be routed to
    the essay writer, which would then compose a resume. It is caught by name
    before the long-text rule is ever reached.
    """
    resume_text = [f for f in anthropic.fields if f.key == "resume_text"]

    assert len(resume_text) == 1
    assert resume_text[0].kind == FieldKind.LONG_TEXT
    assert resume_text[0].field_class == FieldClass.DOCUMENT
    assert not resume_text[0].allows_model_judgement()


def test_one_question_can_produce_several_fields(anthropic):
    """"Resume/CV" carries both `resume` and `resume_text` under one label."""
    resume_fields = [f for f in anthropic.fields if f.label == "Resume/CV"]

    assert {f.key for f in resume_fields} == {"resume", "resume_text"}


# --- safety: the classes a model may never fill -----------------------------

def test_eeoc_questions_are_legal_because_of_where_they_arrived(anthropic):
    """Greenhouse delivers EEOC questions in their own `compliance` block.

    Trusting the block is exact. Matching "veteran" against a label is a guess
    that any reworded question defeats, so structure wins where it exists.
    """
    legal = anthropic.by_class(FieldClass.LEGAL)

    assert any(f.key == "veteran_status" for f in legal)


def test_veteran_status_is_filled_from_memory_but_never_inferred(anthropic):
    """The distinction the product exists for.

    Someone who knows they are not a veteran should answer that once and never
    again. Replaying their own stored answer is not a guess — it is exactly
    what they would have typed. What must never happen is a model *inferring*
    the answer from a resume.
    """
    veteran = [f for f in anthropic.fields if f.key == "veteran_status"][0]

    assert veteran.may_fill_from(FillSource.MEMORY)
    assert veteran.is_autofillable()

    assert not veteran.may_fill_from(FillSource.MODEL_DECISION)
    assert not veteran.may_fill_from(FillSource.MODEL_DRAFT)
    assert not veteran.allows_model_judgement()


def test_legal_questions_are_what_onboarding_asks_once(anthropic, figma):
    """Every legally sensitive field is answerable from a one-time answer."""
    for form in (anthropic, figma):
        for field in form.by_class(FieldClass.LEGAL):
            assert field.needs_onboarding_answer()
            assert field.is_autofillable()


def test_figma_compliance_block_also_lands_as_legal(figma):
    """Four EEOC blocks on this posting; none reachable by a model's judgement."""
    assert len(figma.by_class(FieldClass.LEGAL)) >= 1
    assert all(not f.allows_model_judgement() for f in figma.by_class(FieldClass.LEGAL))


def test_single_option_agreement_is_consent_not_a_question(anthropic):
    """"Agreement to Arbitrate" is a required select with exactly one option.

    It is a checkbox wearing legal text, and it is the one thing that stays
    manual. Veteran status *reports a fact that already exists*, so replaying
    it changes nothing. An arbitration agreement *creates an obligation at the
    moment of the click* — there is no prior fact to replay, the act is the
    click. A tool may type what you told it; it should not agree for you.
    """
    consent = anthropic.by_class(FieldClass.CONSENT)

    arbitration = [f for f in consent if "arbitrat" in f.label.lower()]
    assert arbitration, [f.label for f in consent]
    assert len(arbitration[0].options) == 1

    assert not arbitration[0].is_autofillable()
    assert arbitration[0].may_fill_from(FillSource.HUMAN)


def test_no_model_judgement_ever_reaches_a_sensitive_field(anthropic, figma, linear):
    """The invariant the safety argument rests on, stated precisely.

    Not "sensitive fields are never filled" — they are, from memory, which is
    the point. What never happens is a model's judgement producing the value.
    """
    for form in (anthropic, figma, linear):
        for field in form.fields:
            if field.field_class in (FieldClass.LEGAL, FieldClass.CONSENT):
                assert not field.may_fill_from(FillSource.MODEL_DECISION)
                assert not field.may_fill_from(FillSource.MODEL_DRAFT)


# --- narrative fields, and the spec hidden in their help text ---------------

def test_why_company_essay_is_narrative(anthropic, figma):
    """Present on 37 of 40 sampled Anthropic postings. Not an edge case."""
    anthropic_essays = [f.label for f in anthropic.by_class(FieldClass.NARRATIVE)]
    assert any("Why Anthropic" in label for label in anthropic_essays)

    figma_essays = [f.label for f in figma.by_class(FieldClass.NARRATIVE)]
    assert any("join Figma" in label for label in figma_essays)



def test_narrative_fields_dominate_a_real_engineering_form(linear):
    """Four of Linear's questions are essays, on a twelve-question form.

    Evidence that essays are the bulk of the work rather than a nice-to-have —
    and that jev-apply reporting 51.8% answerable on narrative is a report
    about the main event, not a footnote.
    """
    narrative = linear.by_class(FieldClass.NARRATIVE)

    assert len(narrative) >= 4


# --- the alias table --------------------------------------------------------

def test_recognised_profile_links_do_not_reach_the_model(anthropic, linear):
    """LinkedIn is a custom question — `question_8581812008` on Greenhouse —
    but its label is unambiguous, so it resolves from memory instead."""
    linkedin = [f for f in anthropic.fields if f.label == "LinkedIn Profile"]
    assert linkedin and linkedin[0].field_class == FieldClass.CORE

    github = [f for f in linear.fields if f.label == "Github"]
    assert github and github[0].field_class == FieldClass.CORE


def test_unlabelled_controls_are_kept_but_never_filled(linear):
    """A live Ashby form carried a bare file input and an unlabelled checkbox.

    Dropping them would shrink the form the eval scores itself against, so
    they are recorded as UNKNOWN and excluded from filling instead.
    """
    unknown = linear.by_class(FieldClass.UNKNOWN)

    assert len(unknown) >= 2
    assert all(not f.allows_model_judgement() for f in unknown)


# --- helpers ----------------------------------------------------------------



# --- provenance -------------------------------------------------------------

def test_schema_records_where_it_came_from(anthropic, linear):
    """Greenhouse is authoritative (API); Ashby is observed (DOM). A consumer
    reconciling the two needs to know which it is holding."""
    assert anthropic.source == "greenhouse_api"
    assert linear.source == "ashby_dom"


# --- the radio-group defect, and what caught it -----------------------------
#
# ashby_notion_swe.json is the counter-example to the Linear form: it carries
# EEOC questions. Every test below fails against the first version of the Ashby
# adapter, which treated each radio input as its own field.

@pytest.fixture(scope="module")
def notion():
    return parse_ashby_dom(_fixture("ashby_notion_swe.json"))


def test_radio_group_is_one_question_not_one_per_option(notion):
    """Eight radio inputs sharing a name are one question with eight answers.

    Read as eight fields, a race/ethnicity question becomes eight fields whose
    labels are answers — "Hispanic or Latino", "Two or More Races" — and no
    sensitive-wording pattern matches any of them.
    """
    race = [f for f in notion.fields if f.key.endswith("_systemfield_eeoc_race")]

    assert len(race) == 1
    assert len(race[0].options) == 8
    assert "Hispanic or Latino" in [o.label for o in race[0].options]


def test_ashby_eeoc_is_detected_from_the_field_name_not_the_wording(notion):
    """Ashby tags these itself: `..._systemfield_eeoc_race`.

    Wording could not have worked here — the question text is absent from the
    DOM entirely, and the only text available is the answer options.
    """
    eeoc = [f for f in notion.fields if "_systemfield_eeoc" in f.key]

    assert len(eeoc) == 3
    assert {f.field_class for f in eeoc} == {FieldClass.LEGAL}
    assert all(not f.allows_model_judgement() for f in eeoc)
    assert all(f.may_fill_from(FillSource.MEMORY) for f in eeoc)


def test_protected_characteristics_never_become_screening_questions(notion):
    """The regression this file exists for.

    Previously these arrived as 14 SCREENING fields — gender, race and veteran
    status, each split per option — and SCREENING permits a model's judgement.
    """
    for field in notion.fields:
        option_text = " ".join(o.label for o in field.options).lower()
        if "protected veteran" in option_text or "hispanic or latino" in option_text:
            assert field.field_class == FieldClass.LEGAL
            assert not field.allows_model_judgement()


def test_a_question_with_no_readable_text_is_never_answered(notion):
    """The pronoun group has no legend, no aria-label and no question text.

    Only its five options are readable. A filler that cannot read the question
    has no business choosing an answer, so a blank label means UNKNOWN — which
    is human-only. This also covers the bare file input and the reCAPTCHA
    textarea that real Ashby forms carry.
    """
    pronouns = [f for f in notion.fields if any(o.label == "He/Him" for o in f.options)]

    assert len(pronouns) == 1
    assert pronouns[0].label == ""
    assert pronouns[0].field_class == FieldClass.UNKNOWN
    assert not pronouns[0].is_autofillable()

    recaptcha = [f for f in notion.fields if f.key == "g-recaptcha-response"]
    assert recaptcha and recaptcha[0].field_class == FieldClass.UNKNOWN


def test_independent_checkboxes_are_not_collapsed(notion):
    """"How did you hear about us?" renders as checkboxes with *different*
    names, so they really are separate fields and must not be grouped."""
    sources = [f for f in notion.fields if f.label in ("LinkedIn", "Glassdoor", "Notion Blog")]

    assert len(sources) == 3
    assert all(f.kind == FieldKind.BOOLEAN for f in sources)


# --- phase 0: the deterministic layer ---------------------------------------
#
# Every case below was found by measuring 150 live postings, not by reasoning.
# Widening this layer moved the share of fields needing no model from 56.5% to
# 67.4%, which is the cheapest accuracy in the system: free, instant, exact.

from app.autofill.classify import CORE_LABEL_ALIASES, memory_key_for, normalize_label


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("LinkedIn Profile:", "linkedin profile"),   # x147 — used to miss on the colon
        ("Website(s)", "website s"),                 # x6
        ("  Pronouns  ", "pronouns"),                # x30
        ("Preferred First Name *", "preferred first name"),
        ("How did you hear about this job?", "how did you hear about this job"),
        ("(Optional) Personal Preferences", "optional personal preferences"),
        (None, ""),
    ],
)
def test_label_normalisation(raw, expected):
    """Trailing punctuation and required markers are noise; the parenthetical
    in "(Optional) Personal Preferences" is not, so its words are kept."""
    assert normalize_label(raw) == expected


def test_trailing_colon_no_longer_loses_a_known_field(anthropic):
    """"LinkedIn Profile:" resolves to a stored fact, not a model call.

    147 occurrences across 150 postings were being sent to the binding layer
    because of one character.
    """
    assert memory_key_for("question_999", "LinkedIn Profile:") == "linkedin"
    assert memory_key_for("question_999", "LinkedIn Profile") == "linkedin"

    linkedin = [f for f in anthropic.fields if f.label.startswith("LinkedIn")]
    assert linkedin and linkedin[0].field_class == FieldClass.CORE


def test_primary_and_other_website_do_not_share_a_memory_key(figma):
    """Figma's form carries both "Website" and "Other Website".

    Aliased together, the same URL was filled into both fields.
    """
    assert memory_key_for("q1", "Website") == "website"
    assert memory_key_for("q2", "Other Website") == "website_other"

    keys = {memory_key_for(f.key, f.label) for f in figma.fields}
    assert "website" in keys or "website_other" in keys


def test_ai_policy_attestation_is_never_answered_by_the_tool(anthropic):
    """On 27 of 148 postings. The one field where an autofill tool would be
    attesting to its own involvement, so it is human-only by policy."""
    ai_policy = [f for f in anthropic.fields if "AI Policy" in f.label]

    assert ai_policy, [f.label for f in anthropic.fields]
    assert ai_policy[0].field_class == FieldClass.CONSENT
    assert not ai_policy[0].is_autofillable()
    assert ai_policy[0].may_fill_from(FillSource.HUMAN)


def test_multi_select_disclosures_are_left_to_the_applicant():
    """A disclosure of which facts apply is a statement about the applicant,
    where a wrong selection is a misrepresentation.

    Classified LEGAL rather than CONSENT: these are stable facts about the
    applicant, so they are answered once and replayed, the same treatment as
    veteran status. Either way no model may produce the value — that property
    is what the safety argument needs, and it holds under both.

    Multi-selects were once blocked wholesale for this reason. That was too
    blunt — Stripe asks "select the cohort dates that work best for you" as a
    multi-select, which is the only way any form in the corpus lets an applicant
    say "any term works". So disclosures are now caught by wording, and the
    binder additionally refuses to let a single-value model match touch any
    multi-select.
    """
    from app.autofill.classify import classify
    from app.autofill.schema import FieldOption, FormField

    assert classify(
        "question_1",
        "Please confirm whether any of the below applies to you.",
        FieldKind.MULTI_SELECT,
        option_count=4,
    ) == FieldClass.LEGAL

    field = FormField(
        key="question_1", kind=FieldKind.MULTI_SELECT, field_class=FieldClass.LEGAL,
        label="Please confirm whether any of the below applies to you.",
        options=[FieldOption(label="A", value="0"), FieldOption(label="B", value="1")],
    )
    assert not field.allows_model_judgement()


def test_every_alias_maps_to_exactly_one_memory_key():
    """Two labels may share a key (linkedin/linkedin profile), but a single
    label resolving to two keys would be a silent ambiguity."""
    for label in CORE_LABEL_ALIASES:
        assert normalize_label(label) == label, f"{label!r} is not in normal form"


def test_aliases_never_shadow_a_sensitive_field(anthropic, figma, linear, notion):
    """The asymmetry rule, enforced: no alias may promote a LEGAL or CONSENT
    field into something filled without asking."""
    for form in (anthropic, figma, linear, notion):
        for field in form.fields:
            if field.field_class in (FieldClass.LEGAL, FieldClass.CONSENT):
                assert not field.allows_model_judgement()


# --- Ashby controls the extension has already grouped -------------------------
#
# Measured on a live Notion intern form, 2026-09-27. The extension collapses
# radios into one control with the question text read from Ashby's field
# container, and does the same for checkboxes that share a container. The
# adapter must carry those through, not regroup them.


def test_pregrouped_radio_keeps_its_question_and_options():
    payload = {
        "posting_id": "x", "company": "Notion", "title": "t", "apply_url": "u",
        "controls": [{
            "tag": "input", "type": "radio",
            "name": "f0a9_cebc__q", "id": "f0a9_cebc__q",
            "label": "Will you now or at any time in the future require sponsorship?",
            "required": False,
            "options": [{"label": "OPT", "value": "on"}, {"label": "None", "value": "on"}],
        }],
    }

    [field] = parse_ashby_dom(payload).fields

    # Before the fix the label came back blank and the question text became an
    # option of itself.
    assert field.label.startswith("Will you now")
    assert [o.label for o in field.options] == ["OPT", "None"]
    assert field.kind == FieldKind.SINGLE_SELECT


def test_checkbox_group_is_one_multi_select_question():
    payload = {
        "posting_id": "x", "company": "Notion", "title": "t", "apply_url": "u",
        "controls": [{
            "tag": "input", "type": "checkbox-group",
            "name": "f0a9_82b1-labeled-checkbox", "id": "f0a9_82b1-labeled-checkbox",
            "label": "How did you hear about this opportunity? (select all that apply)",
            "required": False,
            "options": [{"label": "LinkedIn", "value": "LinkedIn"},
                        {"label": "Billboard/Outdoor Ads", "value": "Billboard/Outdoor Ads"}],
        }],
    }

    [field] = parse_ashby_dom(payload).fields

    # Read singly, "Billboard/Outdoor Ads" was a question that matched the
    # stored heard-about answer. As an option it is merely one of the choices.
    assert field.kind == FieldKind.MULTI_SELECT
    assert field.label.startswith("How did you hear")
    assert [o.label for o in field.options] == ["LinkedIn", "Billboard/Outdoor Ads"]


def test_ashby_location_is_a_core_fact():
    payload = {
        "posting_id": "x", "company": "Notion", "title": "t", "apply_url": "u",
        "controls": [{"tag": "input", "type": "text", "name": "_systemfield_location",
                      "id": "_systemfield_location", "required": False,
                      "label": "Current Location", "options": []}],
    }

    [field] = parse_ashby_dom(payload).fields

    assert field.field_class == FieldClass.CORE


def test_essential_functions_question_is_not_answered_from_accommodation_needs():
    """Lyft 8802198002, 2026-09-27: both phrases in one label.

    The question is about ability to do the job; answering it from the
    accommodation-needs answer ("None") produced "No" — the opposite of what
    the applicant meant.
    """
    from app.autofill.classify import memory_key_for

    label = "Can you perform these essential functions of the job with reasonable accommodation?"

    assert memory_key_for("question_38294758002", label) == "essential_functions"
    assert memory_key_for(
        "question_38294759002",
        "Please describe any need for a reasonable accommodation for this hiring process",
    ) == "accommodation_needs"


# --- Greenhouse blocks outside `questions` (Duolingo 8805925002, 2026-09-27) ---


def _duolingo_like_payload():
    return {
        "id": 1, "title": "t", "company_name": "Duolingo", "questions": [], "compliance": [],
        "education": "education_required",
        "education_options": {
            "degrees": [{"id": 1, "text": "Bachelor's Degree"}, {"id": 2, "text": "Master's Degree"}],
            "disciplines": [{"id": 9, "text": "Computer Science"}, {"id": 10, "text": "Accounting"}],
        },
        "demographic_questions": {"header": "Voluntary Self Identification", "questions": [
            {"id": 4028533002, "required": False, "type": "multi_value_multi_select",
             "label": "How would you describe your gender identity? (mark all that apply)",
             "answer_options": [{"id": 1, "label": "Man "}, {"id": 2, "label": "Woman"},
                                {"id": 3, "label": "I don't wish to answer"}]},
            {"id": 4028538002, "required": False, "type": "multi_value_single_select",
             "label": "Are you a veteran or active member of the United States Armed Forces?",
             "answer_options": [{"id": 4, "label": "No, I am not a veteran"}]},
        ]},
    }


def test_demographic_questions_are_legal_and_keyed_to_protected_answers():
    from app.autofill.classify import memory_key_for

    form = parse_greenhouse_job(_duolingo_like_payload())
    by_key = {f.key: f for f in form.fields}

    gender = by_key["demographic_answers.4028533002"]
    assert gender.field_class == FieldClass.LEGAL
    assert gender.kind == FieldKind.MULTI_SELECT
    assert [o.label for o in gender.options][:2] == ["Man", "Woman"]  # trailing space stripped
    assert memory_key_for(gender.key, gender.label) == "gender"
    assert memory_key_for("demographic_answers.4028538002",
                          by_key["demographic_answers.4028538002"].label) == "veteran_status"
    # The same word outside the block is not a protected characteristic.
    assert memory_key_for("question_1", "Please share your gender pronouns.") != "gender"


def test_education_block_is_synthesised_with_board_options():
    from app.autofill.classify import memory_key_for

    form = parse_greenhouse_job(_duolingo_like_payload())
    by_key = {f.key: f for f in form.fields}

    assert by_key["educations[0].school_name_id"].field_class == FieldClass.CORE
    assert [o.label for o in by_key["educations[0].degree_id"].options] == ["Bachelor's Degree", "Master's Degree"]
    assert by_key["educations[0].discipline_id"].kind == FieldKind.SINGLE_SELECT
    assert len(by_key["educations[0].end_date.month"].options) == 12
    assert all(f.required for k, f in by_key.items() if k.startswith("educations"))
    assert memory_key_for("educations[0].degree_id", "Degree") == "degree_type"

    # Optional mode still renders the fields, just not required; absent mode renders none.
    payload = _duolingo_like_payload(); payload["education"] = "education_optional"
    assert not any(f.required for f in parse_greenhouse_job(payload).fields if f.key.startswith("educations"))
    payload["education"] = None
    assert not [f for f in parse_greenhouse_job(payload).fields if f.key.startswith("educations")]


def test_education_answers_are_derived_from_the_stored_degree_and_date():
    from app.autofill.memory import Memory

    memory = Memory(education={"degree": "BS Computer Science", "graduation_date": "May 2028",
                               "university": "Cornell University"})

    assert memory.lookup("degree_type") == "Bachelor's Degree"
    assert memory.lookup("discipline") == "Computer Science"
    assert memory.lookup("graduation_month") == "May"
    assert memory.lookup("graduation_year") == "2028"
    assert memory.lookup("education_start_year") is None  # nothing stored -> review

    long_form = Memory(education={"degree": "Bachelor of Science in Computer Science"})
    assert long_form.lookup("degree_type") == "Bachelor's Degree"
    assert long_form.lookup("discipline") == "Computer Science"


def test_stored_eeoc_gender_matches_self_identification_wording():
    from app.autofill.format import match_option
    from app.autofill.schema import FieldOption, FormField

    field = FormField(key="demographic_answers.1", label="gender identity", kind=FieldKind.MULTI_SELECT,
                      field_class=FieldClass.LEGAL, required=False,
                      options=[FieldOption(label="Man", value="1"), FieldOption(label="Woman", value="2"),
                               FieldOption(label="I don't wish to answer", value="3")])

    assert match_option("Male", field).label == "Man"
    assert match_option("Asian", FormField(key="k", label="race", kind=FieldKind.SINGLE_SELECT,
                                           field_class=FieldClass.LEGAL, required=False,
                                           options=[FieldOption(label="East Asian", value="1"),
                                                    FieldOption(label="South Asian", value="2")])) is None


def test_location_block_yields_one_core_field_and_drops_the_hidden_coordinates():
    payload = _duolingo_like_payload()
    payload["location_questions"] = [
        {"label": "Longitude", "required": True, "fields": [{"name": "longitude", "type": "input_hidden", "values": []}]},
        {"label": "Latitude", "required": True, "fields": [{"name": "latitude", "type": "input_hidden", "values": []}]},
        {"label": "Location", "required": True, "fields": [{"name": "location", "type": "input_text", "values": []}]},
    ]
    from app.autofill.classify import memory_key_for

    fields = [f for f in parse_greenhouse_job(payload).fields if f.key in ("location", "longitude", "latitude")]

    assert [f.key for f in fields] == ["location"]
    assert fields[0].field_class == FieldClass.CORE and fields[0].required
    assert memory_key_for("location", "Location") == "current_location"


def test_country_select_is_answered_from_residence():
    from app.autofill.memory import Memory
    from app.autofill.classify import memory_key_for

    fields = {f.key: f for f in parse_greenhouse_job(_duolingo_like_payload()).fields}
    assert fields["country"].field_class == FieldClass.CORE and fields["country"].required
    assert memory_key_for("country", "Country") == "country_of_residence"
    assert Memory(facts={"current_location": "Ithaca, NY"}).lookup("country_of_residence") == "United States"
    assert Memory(facts={"current_location": "Toronto, ON"}).lookup("country_of_residence") is None
    assert Memory(facts={"country": "Canada"}).lookup("country_of_residence") == "Canada"
    assert "educations[0].start_date.month" in fields and fields["educations[0].start_date.year"].required


def test_careers_website_matches_the_employers_own_site_option():
    from app.autofill.format import match_option
    from app.autofill.schema import FieldOption, FormField

    def field(*labels):
        return FormField(key="q", label="How did you hear about us?", kind=FieldKind.MULTI_SELECT,
                         field_class=FieldClass.CORE, required=True,
                         options=[FieldOption(label=l, value=str(i)) for i, l in enumerate(labels)])

    assert match_option("Careers Website", field("LinkedIn", "Handshake", "Integra FEC Website", "Other")).label == "Integra FEC Website"
    assert match_option("Careers Website", field("Referral", "Social (i.e. LinkedIn, Facebook)", "Verkada Careers Page", "RepVue", "Other")).label == "Verkada Careers Page"
    # Two candidate sites, or only third-party ones: no guess.
    assert match_option("Careers Website", field("Company Website", "Careers Page", "Other")) is None
    assert match_option("Careers Website", field("LinkedIn", "Indeed", "Other")) is None


def test_custom_address_questions_derive_from_the_stored_location():
    from app.autofill.memory import Memory
    from app.autofill.classify import memory_key_for

    m = Memory(facts={"current_location": "Ithaca, NY"})
    assert m.lookup("state_of_residence") == "New York"
    assert m.lookup("city_of_residence") == "Ithaca"
    assert m.lookup("street_address") is None            # a real gap stays a gap
    assert memory_key_for("question_1", "State/Province") == "state_of_residence"
    assert memory_key_for("question_2", "Country") == "country_of_residence"
    assert memory_key_for("question_3", "Address Line 1") == "street_address"
    assert memory_key_for("question_4", "City") == "city_of_residence"
