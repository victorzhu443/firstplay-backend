"""
Regression tests for the binder's passes, built from the 2026-10-12 code
audit's probes (CODE_AUDIT_REPORT.md §4, §5.8). Every profile here is
synthetic; nothing is read from ~/.config/firstplay.

The shape throughout: a stub binder that looks like a JevBinder to
`resolve_form` (it has the gate methods), records what it is handed, and
answers nothing or a fixed answer — so what would cross the model boundary
is observable without a model.
"""
import pytest

from app.autofill.binder import (
    AUTOFILL_CONFIDENCE,
    DeterministicBinder,
    _profile_catalog,
    resolve_form,
)
from app.autofill.greenhouse import parse_greenhouse_job
from app.autofill.memory import Memory
from app.autofill.schema import FieldClass, FieldKind, FieldOption, FillSource, FormField, FormSchema
from app.autofill.themes import QuestionTheme


def _opts(*labels):
    return [FieldOption(label=label, value=label) for label in labels]


def _form(fields, company="Acme", country=None):
    return FormSchema(source="test", ats="greenhouse", posting_id="p1", company=company,
                      fields=fields, country=country)


def _field(key, label, kind=FieldKind.TEXT, field_class=FieldClass.SCREENING, options=(), required=False):
    return FormField(key=key, label=label, kind=kind, field_class=field_class,
                     options=_opts(*options), required=required)


class SpyBinder(DeterministicBinder):
    """Records every state handed to a model gate; answers nothing."""

    name = "spy"
    calls = 0
    cost_usd = 0.0
    tripped = False

    def __init__(self):
        self.captured = []

    def answer_from_profile(self, state, requests):
        self.captured.append(("answer_from_profile", state))
        return {}

    def match_options(self, requests):
        self.captured.append(("match_options", {"questions": [(f.label, v) for _r, v, f in requests]}))
        return {}

    def second_pass(self, state, requests):
        self.captured.append(("second_pass", state))
        return {}

    def verify_values(self, state, items):
        self.captured.append(("verify_values", state))
        return {}


# --- F1: earlier_answers never carries protected or consent answers ----------

PROTECTED = {
    "veteran_status": "I am not a protected veteran",
    "race_ethnicity": "Asian",
    "gender": "Male",
    "disability_status": "No, I do not have a disability",
    "hispanic_latino": "No",
}
CONSENTS = {"privacy_notice": "Agree", "sms_messages": "No"}


def _p01_form():
    """A Greenhouse-shaped payload: core fields, a consent, a custom select,
    a custom text field, and the EEOC compliance block."""
    payload = {
        "id": 1, "company_name": "Acme", "title": "SWE Intern", "location": {"name": "Ithaca, NY"},
        "questions": [
            {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text"}]},
            {"label": "Email", "required": True, "fields": [{"name": "email", "type": "input_text"}]},
            {"label": "Phone", "required": True, "fields": [{"name": "phone", "type": "input_text"}]},
            {"label": "Do you opt-in to receive SMS messages from Acme Recruiting?", "required": False,
             "fields": [{"name": "question_1", "type": "multi_value_single_select",
                         "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
            {"label": "Which of our offices would you like to be considered for?", "required": False,
             "fields": [{"name": "question_2", "type": "multi_value_single_select",
                         "values": [{"label": "Ithaca", "value": 1}, {"label": "Remote", "value": 2}]}]},
            {"label": "Please tell us anything about your availability", "required": False,
             "fields": [{"name": "question_3", "type": "input_text"}]},
        ],
        "compliance": [{"type": "eeoc", "questions": [
            {"label": "Gender", "required": False, "fields": [{"name": "gender", "type": "multi_value_single_select",
             "values": [{"label": "Male", "value": "1"}, {"label": "Female", "value": "2"}, {"label": "Decline To Self Identify", "value": "3"}]}]},
            {"label": "Race", "required": False, "fields": [{"name": "race", "type": "multi_value_single_select",
             "values": [{"label": "Asian", "value": "1"}, {"label": "White", "value": "2"}, {"label": "Decline To Self Identify", "value": "3"}]}]},
            {"label": "Veteran Status", "required": False, "fields": [{"name": "veteran_status", "type": "multi_value_single_select",
             "values": [{"label": "I am not a protected veteran", "value": "1"}, {"label": "I identify as one or more of the classifications of a protected veteran", "value": "2"}]}]},
            {"label": "Disability Status", "required": False, "fields": [{"name": "disability_status", "type": "multi_value_single_select",
             "values": [{"label": "Yes, I have a disability", "value": "1"}, {"label": "No, I do not have a disability", "value": "2"}]}]},
        ]}],
    }
    return parse_greenhouse_job(payload)


def _p01_memory():
    return Memory(
        facts={"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com", "phone": "555-010-0100",
               "street_address": "1 Example St", "postal_code": "14850", "current_location": "Ithaca, NY"},
        education={"university": "Cornell University", "degree": "BS Computer Science", "graduation_date": "May 2028"},
        legal_status={"work_authorization": "Yes", "needs_sponsorship": "No", "citizenship": "US citizen"},
        preferences={"heard_about": "LinkedIn"},
        protected=dict(PROTECTED),
        consents=dict(CONSENTS),
    )


def test_earlier_answers_exclude_legal_and_consent_answers():
    """§5.8 test 1 (F1). The EEOC block resolves from `protected` in gate 1;
    none of it, and no consent, may appear in any state the model sees."""
    form = _p01_form()
    legal = {f.label for f in form.fields if f.field_class == FieldClass.LEGAL}
    consent = {f.label for f in form.fields if f.field_class == FieldClass.CONSENT}
    assert legal and consent, "the fixture must carry both classes"

    spy = SpyBinder()
    plan = resolve_form(form, _p01_memory(), binder=spy)

    # The protected answers were used: they are filled deterministically.
    by_key = {e.field_key: e for e in plan.entries}
    assert by_key["gender"].value == "Male" and not by_key["gender"].needs_review

    gates = [kind for kind, _ in spy.captured]
    assert "answer_from_profile" in gates and "second_pass" in gates, gates

    for kind, state in spy.captured:
        earlier = (state.get("form") or {}).get("earlier_answers") or []
        questions = {row["question"] for row in earlier}
        assert not (questions & legal), (kind, questions & legal)
        assert not (questions & consent), (kind, questions & consent)
        # The core contact fields are not earlier answers either: a name, an
        # email or a phone answers no screening question (F8).
        assert not (questions & {"First Name", "Email", "Phone"}), (kind, questions)
        # Not the labels, and not the values either, anywhere in the state.
        flat = repr(state)
        for value in list(PROTECTED.values()) + list(CONSENTS.values()):
            if value in ("No", "Agree"):
                continue  # too short to be evidence either way
            assert value not in flat, (kind, value)
        assert "sms" not in flat.lower()
        # Nor the contact details the core fields answered (F8): shapes only.
        for value in ("ada@example.com", "555-010-0100", "1 Example St"):
            assert value not in flat, (kind, value)


def test_earlier_answers_do_not_carry_attachment_paths():
    """A resolved file field carries the résumé path as its value; a path is
    not an earlier answer."""
    form = _form([
        _field("resume", "Resume/CV", kind=FieldKind.FILE, field_class=FieldClass.DOCUMENT),
        _field("question_1", "Which office?", kind=FieldKind.SINGLE_SELECT, options=("Ithaca", "Remote")),
    ])
    spy = SpyBinder()
    resolve_form(form, Memory(facts={"resume_file": "~/Documents/resume.pdf"}), binder=spy)
    for _kind, state in spy.captured:
        assert "resume.pdf" not in repr(state)


# --- F8: the second-pass catalog carries no raw contact values ---------------

CONTACT = {
    "first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com", "phone": "555-010-0100",
    "street_address": "1 Example St", "linkedin": "https://linkedin.com/in/ada", "postal_code": "14850",
}


def test_second_pass_catalog_names_contact_keys_without_their_values():
    memory = Memory(facts=dict(CONTACT, current_employer="Analytical Engines"),
                    education={"university": "Cornell University"})
    catalog = _profile_catalog(memory)
    # The keys are offered, so a "university email" field can still pick `email`...
    for key in ("email", "phone", "street_address", "linkedin", "first_name", "last_name", "full_name"):
        assert key in catalog, key
    # ...but no value of theirs is in what the model sees.
    flat = " ".join(catalog.values())
    for value in CONTACT.values():
        assert value not in flat, value
    assert "Ada Lovelace" not in flat
    # Non-contact facts are still sent as values: the model answers from them.
    assert catalog["university"] == "Cornell University"
    assert catalog["current_employer"] == "Analytical Engines"


def test_second_pass_writes_the_stored_email_when_the_model_picks_the_email_key():
    """The Choice returns a key; the binder writes the stored value verbatim,
    not the shape description it showed the model."""

    class PicksEmail(SpyBinder):
        def second_pass(self, state, requests):
            self.captured.append(("second_pass", state))
            return {rid: ("email", 0.97, 0.95) for rid, _f, _c in requests}

        def verify_values(self, state, items):
            self.captured.append(("verify_values", {"items": items}))
            return {rid: 0.96 for rid, _l, _v in items}

    form = _form([
        _field("email", "Email", field_class=FieldClass.CORE),
        _field("question_1", "University email address (if different)"),
    ])
    binder = PicksEmail()
    plan = resolve_form(form, Memory(facts=dict(CONTACT)), binder=binder)
    entry = {e.field_key: e for e in plan.entries}["question_1"]
    assert entry.value == "ada@example.com"
    assert entry.source == FillSource.MODEL_DECISION
    assert not entry.needs_review
    assert entry.theme == "profile.email"
    # Neither the second-pass state nor the verify items carried the address —
    # not in the catalog, and not through the core "Email" field's earlier
    # answer either, which is dropped.
    for kind, state in binder.captured:
        if kind in ("second_pass", "verify_values"):
            assert "ada@example.com" not in repr(state), kind
    second = next(state for kind, state in binder.captured if kind == "second_pass")
    assert all(row["question"] != "Email" for row in second["form"]["earlier_answers"])


# --- §5.8 test 9: a low-confidence theme is a suggestion, not an answer ------

def test_a_low_confidence_theme_is_downgraded_to_a_suggestion():
    class Unsure(DeterministicBinder):
        def classify_themes(self, labels):
            return {label: (QuestionTheme.HEARD_ABOUT, AUTOFILL_CONFIDENCE - 0.2) for label in labels}

    form = _form([_field("question_1", "Where did you first come across us?", kind=FieldKind.SINGLE_SELECT,
                         options=("LinkedIn", "Referral"))])
    plan = resolve_form(form, Memory(preferences={"heard_about": "LinkedIn"}), binder=Unsure())
    entry = plan.entries[0]
    assert entry.needs_review
    assert entry.value == "LinkedIn"  # shown as the suggestion
    assert entry.confidence == pytest.approx(AUTOFILL_CONFIDENCE - 0.2)
    assert "low confidence" in (entry.reason or "")
    assert entry.theme == "heard_about"


# --- §5.8 test 9: the sanctions disclosure branches, through the plan --------

_SANCTIONS_OPTIONS = ("Citizen or resident of Cuba", "Citizen or resident of Iran",
                      "Citizen or resident of North Korea", "None of the above")


@pytest.mark.parametrize("kind", [FieldKind.SINGLE_SELECT, FieldKind.MULTI_SELECT])
def test_sanctions_disclosure_picks_none_of_the_above_from_stored_facts(kind):
    form = _form([_field("question_1", "Export control: select all that apply", kind=kind,
                         field_class=FieldClass.LEGAL, options=_SANCTIONS_OPTIONS, required=True)])
    memory = Memory(facts={"current_location": "Ithaca, NY"}, legal_status={"citizenship": "US citizen"})
    entry = resolve_form(form, memory, binder=DeterministicBinder()).entries[0]
    assert not entry.needs_review
    assert entry.source == FillSource.MEMORY
    if kind == FieldKind.MULTI_SELECT:
        assert entry.values == ["None of the above"] and entry.value is None
    else:
        assert entry.value == "None of the above" and entry.values == []


def test_sanctions_disclosure_with_a_sanctioned_residence_stays_with_the_applicant():
    form = _form([_field("question_1", "Export control: select all that apply", kind=FieldKind.MULTI_SELECT,
                         field_class=FieldClass.LEGAL, options=_SANCTIONS_OPTIONS, required=True)])
    memory = Memory(facts={"current_location": "Havana, Cuba"}, legal_status={"citizenship": "US citizen"})
    entry = resolve_form(form, memory, binder=DeterministicBinder()).entries[0]
    assert entry.needs_review
    assert not entry.values and entry.value is None


# --- every entry names its class ---------------------------------------------

def test_every_entry_carries_its_fields_class():
    plan = resolve_form(_p01_form(), _p01_memory(), binder=DeterministicBinder())
    by_key = {e.field_key: e for e in plan.entries}
    assert by_key["gender"].field_class == "legal"
    assert by_key["veteran_status"].field_class == "legal"
    assert by_key["question_1"].field_class == "consent"
    assert by_key["email"].field_class == "core"
    assert by_key["question_2"].field_class == "screening"
    assert {e.field_class for e in plan.entries} <= {c.value for c in FieldClass}
    # Serialised as a plain string, with a default, so older clients ignore it.
    assert plan.model_dump()["entries"][0]["field_class"] in {c.value for c in FieldClass}
    from app.autofill.binder import FillPlanEntry
    assert FillPlanEntry(field_key="x", label="x", source=FillSource.HUMAN).field_class == "unknown"


# --- a tripped breaker finishes the plan deterministically --------------------

def test_a_model_error_in_a_sequential_gate_leaves_the_plan_complete():
    from app.exceptions import JevServiceError

    class Tripped(SpyBinder):
        tripped = True

        def match_options(self, requests):
            raise JevServiceError("model skipped: an earlier call in this plan timed out")

        def answer_from_profile(self, state, requests):
            raise JevServiceError("model skipped: an earlier call in this plan timed out")

        def second_pass(self, state, requests):
            raise JevServiceError("model skipped: an earlier call in this plan timed out")

    form = _form([
        _field("question_1", "Are you open to relocation?", kind=FieldKind.SINGLE_SELECT,
               options=("Open to relocation within the US", "Prefer to stay where I am")),
        _field("question_2", "Which office?", kind=FieldKind.SINGLE_SELECT, options=("Ithaca", "Remote")),
        _field("question_3", "Tell us about your availability"),
    ])
    plan = resolve_form(form, Memory(preferences={"open_to_relocation": "Yes"}, education={"university": "Cornell"}),
                        binder=Tripped())
    assert len(plan.entries) == 3
    assert all(e.needs_review for e in plan.entries)
    assert plan.summary()["review"] == 3


# --- F33: themes write only short answers on core/screening controls ---------

def test_a_narrative_textarea_never_takes_a_theme_value():
    form = _form([_field("question_1", "Please explain any gaps in your résumé or a low GPA",
                         kind=FieldKind.LONG_TEXT, field_class=FieldClass.NARRATIVE)])
    entry = resolve_form(form, Memory(education={"gpa": "3.8"}), binder=DeterministicBinder()).entries[0]
    assert entry.theme == "gpa"  # recognised, for the diagnostics
    assert entry.value is None and entry.needs_review and not entry.skipped


def test_a_file_field_never_becomes_an_attachment_from_a_theme_value():
    form = _form([_field("question_1", "Upload a zip of your code sample", kind=FieldKind.FILE,
                         field_class=FieldClass.DOCUMENT)])
    entry = resolve_form(form, Memory(facts={"postal_code": "14850"}), binder=DeterministicBinder()).entries[0]
    assert entry.attach is None and entry.value is None
    assert entry.needs_review and entry.document == "portfolio"


def test_a_screening_text_field_still_takes_the_theme_value():
    form = _form([_field("question_1", "What is your GPA?")])
    entry = resolve_form(form, Memory(education={"gpa": "3.8"}), binder=DeterministicBinder()).entries[0]
    assert entry.value == "3.8" and not entry.needs_review


# --- F13 / F14: the conditional's parent and polarity --------------------------

def test_a_follow_up_hangs_on_the_question_immediately_above_it():
    """p04 case A: a multi-select parent left for review; the follow-up must
    not be reasoned about the sponsorship answer two fields up."""
    form = _form([
        _field("q0", "Will you now or in the future require sponsorship?", kind=FieldKind.SINGLE_SELECT, options=("Yes", "No")),
        _field("q1", "Which of the following apply to you? (select all that apply)", kind=FieldKind.MULTI_SELECT,
               options=("I have a security clearance", "Other")),
        _field("q2", "If you selected 'Other' above, please specify", required=True),
    ])
    plan = resolve_form(form, Memory(legal_status={"needs_sponsorship": "No"}), binder=DeterministicBinder())
    e = {x.field_key: x for x in plan.entries}
    assert e["q0"].value == "No" and e["q1"].needs_review
    assert e["q2"].needs_review and e["q2"].satisfied_by is None
    assert "still unanswered" in e["q2"].reason and "'No'" not in e["q2"].reason


def test_a_follow_up_to_an_unanswered_parent_is_not_hidden_as_satisfied():
    """p04 case B."""
    form = _form([
        _field("q0", "Will you now or in the future require sponsorship?", kind=FieldKind.SINGLE_SELECT, options=("Yes", "No")),
        _field("q1", "Do you have any familial relationships with a current employee?", kind=FieldKind.SINGLE_SELECT,
               field_class=FieldClass.LEGAL, options=("Yes", "No")),
        _field("q2", "If you answered Yes, please provide details"),
    ])
    plan = resolve_form(form, Memory(legal_status={"needs_sponsorship": "No"}), binder=DeterministicBinder())
    e = {x.field_key: x for x in plan.entries}
    assert e["q1"].needs_review
    assert e["q2"].needs_review and e["q2"].satisfied_by is None
    assert e["q2"] in plan.for_review()


def test_a_follow_up_to_an_answered_parent_still_resolves_through_it():
    form = _form([
        _field("q1", "Do you have any familial relationships with a current employee?", kind=FieldKind.SINGLE_SELECT,
               field_class=FieldClass.LEGAL, options=("Yes", "No")),
        _field("q2", "If you answered Yes, please provide details"),
    ])
    plan = resolve_form(form, Memory(protected={"familial_relationship": "No"}), binder=DeterministicBinder())
    e = {x.field_key: x for x in plan.entries}
    assert e["q1"].value == "No"
    assert e["q2"].satisfied_by == "q1" and not e["q2"].needs_review


def test_i_am_not_is_read_as_no_and_the_protected_answer_is_never_quoted():
    """p04 case C: the shared polarity (format._as_bool) and no characters
    of a LEGAL parent's answer in the follow-up's reason."""
    stored = "I am not a protected veteran"
    form = _form([
        _field("veteran_status", "Veteran Status", kind=FieldKind.SINGLE_SELECT, field_class=FieldClass.LEGAL,
               options=(stored, "I identify as a protected veteran")),
        _field("q2", "If you answered yes, please describe your service"),
    ])
    plan = resolve_form(form, Memory(protected={"veteran_status": stored}), binder=DeterministicBinder())
    e = {x.field_key: x for x in plan.entries}
    assert e["q2"].satisfied_by == "veteran_status" and not e["q2"].needs_review
    assert "protected" not in e["q2"].reason and "I am" not in e["q2"].reason


def test_a_protected_yes_above_asks_for_detail_without_quoting_it():
    stored = "I identify as a protected veteran"
    form = _form([
        _field("veteran_status", "Veteran Status", kind=FieldKind.SINGLE_SELECT, field_class=FieldClass.LEGAL,
               options=("I am not a protected veteran", stored)),
        _field("q2", "If you answered yes, please describe your service"),
    ])
    plan = resolve_form(form, Memory(protected={"veteran_status": stored}), binder=DeterministicBinder())
    e = {x.field_key: x for x in plan.entries}
    # "I identify as …" has no polarity in format._as_bool, so the follow-up
    # stays with the applicant rather than being hidden; either way the
    # protected answer is not quoted.
    assert e["q2"].needs_review and e["q2"].satisfied_by is None
    assert "identify" not in e["q2"].reason and "veteran" not in e["q2"].reason


def test_a_parent_with_no_polarity_leaves_the_follow_up_with_the_applicant():
    form = _form([
        _field("q1", "Which of these describes you?", kind=FieldKind.SINGLE_SELECT,
               options=("Decline to self-identify", "Yes", "No")),
        _field("q2", "If you answered Yes, please provide details"),
    ])
    memory = Memory()
    memory.remember_answer("Which of these describes you?", "Decline to self-identify", "Acme")
    plan = resolve_form(form, memory, binder=DeterministicBinder())
    e = {x.field_key: x for x in plan.entries}
    assert e["q1"].value == "Decline to self-identify"
    assert e["q2"].needs_review and e["q2"].satisfied_by is None


# --- F20 / F18: a second "Email" is somebody else's ----------------------------

def test_a_references_email_is_neither_filled_nor_satisfied_by_the_applicants():
    """p10. The applicant's `email` is a vendor key; a later bare "Email" on a
    custom key is the reference's."""
    form = _form([
        _field("email", "Email", field_class=FieldClass.CORE, required=True),
        _field("question_9", "Reference 1", required=True),
        _field("question_10", "Email", required=True),
    ])
    memory = Memory(facts={"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"})
    plan = resolve_form(form, memory, binder=DeterministicBinder())
    e = {x.field_key: x for x in plan.entries}
    assert e["email"].value == "ada@example.com" and not e["email"].needs_review
    assert e["question_10"].value is None
    assert e["question_10"].needs_review and e["question_10"].satisfied_by is None
    assert e["question_10"].reason == "a second Email on this form — a reference's or an employer's?"
    assert e["question_10"] in plan.for_review()


def test_a_legal_first_name_still_fills_after_the_vendor_first_name():
    """Not a bare alias: a restatement of the applicant's own name."""
    form = _form([
        _field("first_name", "First Name", field_class=FieldClass.CORE, required=True),
        _field("question_3", "Legal First Name", field_class=FieldClass.CORE),
    ])
    plan = resolve_form(form, Memory(facts={"first_name": "Ada", "last_name": "Lovelace"}), binder=DeterministicBinder())
    e = {x.field_key: x for x in plan.entries}
    assert e["question_3"].value == "Ada" and not e["question_3"].needs_review


def test_a_bare_email_with_no_vendor_email_before_it_still_fills():
    form = _form([_field("question_1", "Email", required=True)])
    plan = resolve_form(form, Memory(facts={"email": "ada@example.com"}), binder=DeterministicBinder())
    assert plan.entries[0].value == "ada@example.com" and not plan.entries[0].needs_review


def test_vendor_alternates_still_satisfy_each_other_and_same_labels_need_the_same_shape():
    from app.autofill.binder import _mark_satisfied_alternates, FillPlanEntry
    form = _form([
        _field("resume", "Resume/CV", kind=FieldKind.FILE, field_class=FieldClass.DOCUMENT),
        _field("resume_text", "Resume/CV", kind=FieldKind.LONG_TEXT, field_class=FieldClass.DOCUMENT),
        _field("email", "Email", field_class=FieldClass.CORE),
        _field("question_10", "Email"),
    ])
    entries = [
        FillPlanEntry(field_key="resume", label="Resume/CV", attach="~/resume.pdf", source=FillSource.MEMORY),
        FillPlanEntry(field_key="resume_text", label="Resume/CV", source=FillSource.HUMAN, needs_review=True),
        FillPlanEntry(field_key="email", label="Email", value="ada@example.com", source=FillSource.MEMORY),
        FillPlanEntry(field_key="question_10", label="Email", source=FillSource.HUMAN, needs_review=True),
    ]
    _mark_satisfied_alternates(entries, form)
    assert entries[1].satisfied_by == "resume"
    assert entries[3].satisfied_by is None and entries[3].needs_review


# --- F37 / F15(a): authorisation and sponsorship ------------------------------

def _auth_memory(**legal):
    return Memory(legal_status=dict({"work_authorization": "Yes", "needs_sponsorship": "No",
                                     "citizenship": "US citizen"}, **legal))


@pytest.mark.parametrize("label, place", [
    ("Are you legally authorized to work in Japan?", "Japan"),
    ("Are you legally entitled to work in New Zealand?", "New Zealand"),
    ("Do you have the right to work in Hong Kong during the internship, without sponsorship?", "Hong Kong"),
    ("Will you now or in the future require DRW to sponsor you in the Netherlands for a work visa?", "Netherlands"),
])
def test_authorisation_for_an_unlisted_country_goes_to_review_naming_it(label, place):
    form = _form([_field("question_1", label, kind=FieldKind.SINGLE_SELECT, options=("Yes", "No"))], country="US")
    entry = resolve_form(form, _auth_memory(), binder=DeterministicBinder()).entries[0]
    assert entry.needs_review and entry.value is None
    assert place in entry.reason


@pytest.mark.parametrize("label", [
    "Are you legally authorized to work in the United States?",
    "Are you authorized to work for Acme in the U.S.?",
    "Will you require visa sponsorship to work for Acme?",            # the employer, not a place
    "Do you require any visas or work authorizations to work in California?",   # a US state
])
def test_a_known_country_a_state_or_the_employer_is_not_an_unlisted_place(label):
    form = _form([_field("question_1", label, kind=FieldKind.SINGLE_SELECT, options=("Yes", "No"))], country="US")
    entry = resolve_form(form, _auth_memory(), binder=DeterministicBinder()).entries[0]
    assert not entry.needs_review and entry.value in ("Yes", "No")


@pytest.mark.parametrize("label", [
    "Are you legally authorized to work in the US without requiring sponsorship?",
    "Are you legally authorized to work in the United States without employer sponsorship, now or in the future?",
    "Are you currently authorized to work in the United States? Acme DOES assist with visa sponsorship, so please share your status.",
    "This position requires candidates be U.S. citizens who are legally authorized to work in the United States "
    "without the need for employer sponsorship now or in the future. Do you meet this requirement?",
])
def test_authorised_without_sponsorship_is_yes_for_an_authorised_citizen_needing_none(label):
    """F15(a), on-corpus: 24 instances were filled "No" (the sponsorship
    answer) where a citizen's true answer is "Yes"."""
    form = _form([_field("question_1", label, kind=FieldKind.SINGLE_SELECT, options=("Yes", "No"))])
    entry = resolve_form(form, _auth_memory(), binder=DeterministicBinder()).entries[0]
    assert entry.theme == "work_authorization"
    assert entry.value == "Yes" and not entry.needs_review, entry.reason


def test_authorised_without_sponsorship_is_no_when_sponsorship_is_needed():
    """Authorised today (OPT) but needing sponsorship later: the composite
    question is a No, which neither stored answer alone gives."""
    label = "Are you legally authorized to work in the US without requiring sponsorship now or in the future?"
    form = _form([_field("question_1", label, kind=FieldKind.SINGLE_SELECT, options=("Yes", "No"))])
    entry = resolve_form(form, _auth_memory(needs_sponsorship="Yes", citizenship="Indian citizen"),
                         binder=DeterministicBinder()).entries[0]
    assert entry.value == "No" and not entry.needs_review


def test_authorised_without_sponsorship_needs_both_answers():
    label = "Are you legally authorized to work in the US without requiring sponsorship?"
    form = _form([_field("question_1", label, kind=FieldKind.SINGLE_SELECT, options=("Yes", "No"))])
    entry = resolve_form(form, Memory(legal_status={"work_authorization": "Yes"}), binder=DeterministicBinder()).entries[0]
    assert entry.needs_review and entry.value is None


def test_a_citizenship_requirement_is_not_answered_for_a_non_citizen():
    label = ("This position requires candidates be U.S. citizens who are legally authorized to work in the United "
             "States without the need for employer sponsorship. Do you meet this requirement?")
    form = _form([_field("question_1", label, kind=FieldKind.SINGLE_SELECT, options=("Yes", "No"))])
    entry = resolve_form(form, _auth_memory(citizenship="Permanent resident (green card)"),
                         binder=DeterministicBinder()).entries[0]
    assert entry.needs_review and "citizenship" in entry.reason


@pytest.mark.parametrize("label", [
    "Will you now or in the future require sponsorship to work in the US?",
    "Do you currently require employer-sponsored work authorization (e.g., employer visa sponsorship) to work in the country where this role is based?",
    "Do you now, or will you in the future, need sponsorship from an employer in order to obtain, extend or renew your authorization to work in the United States?",
    "If currently authorized to work legally in the country to which you are applying, will you in the future require employer-sponsored work authorization?",
])
def test_requiring_sponsorship_is_still_the_sponsorship_theme(label):
    form = _form([_field("question_1", label, kind=FieldKind.SINGLE_SELECT, options=("Yes", "No"))], country="US")
    entry = resolve_form(form, _auth_memory(), binder=DeterministicBinder()).entries[0]
    assert entry.theme == "visa_sponsorship"
    assert entry.value == "No" and not entry.needs_review


# --- F24 and the stored "None" -------------------------------------------------

def test_which_state_is_answered_from_the_stored_location():
    form = _form([_field("question_1", "Which state or province do you currently live in?", kind=FieldKind.SINGLE_SELECT,
                         options=("New York", "California", "Other"))])
    entry = resolve_form(form, Memory(facts={"current_location": "Ithaca, NY"}), binder=DeterministicBinder()).entries[0]
    assert entry.value == "New York" and not entry.needs_review


def test_which_state_with_no_location_asks_for_the_location():
    form = _form([_field("question_1", "Which state or province do you currently live in?", kind=FieldKind.SINGLE_SELECT,
                         options=("New York", "California", "Other"))])
    entry = resolve_form(form, Memory(), binder=DeterministicBinder()).entries[0]
    assert entry.needs_review and entry.needs_fact is not None
    assert entry.needs_fact["key"] == "current_location"


def test_a_stored_none_is_never_typed_into_a_text_field():
    optional = _form([_field("question_1", "Do you have any deadlines or timeline considerations we should know about?")])
    entry = resolve_form(optional, Memory(preferences={"timeline_notes": "None"}), binder=DeterministicBinder()).entries[0]
    assert entry.value is None and entry.skipped and not entry.needs_review

    required = _form([_field("question_1", "Do you have any deadlines or timeline considerations we should know about?",
                             required=True)])
    entry = resolve_form(required, Memory(preferences={"timeline_notes": "None"}), binder=DeterministicBinder()).entries[0]
    assert entry.value is None and entry.needs_review and not entry.skipped


# --- the condition names an option, or is the field's own -------------------

@pytest.mark.parametrize("heard, expect", [("LinkedIn", "satisfied"), ("A friend or relative", "detail")])
def test_a_condition_naming_an_option_is_read_against_the_parents_answer(heard, expect):
    form = _form([
        _field("q1", "How did you hear about us?", kind=FieldKind.SINGLE_SELECT,
               options=("LinkedIn", "A friend or relative", "Other")),
        _field("q2", "If you selected 'A friend or relative', please put their full name below."),
    ])
    plan = resolve_form(form, Memory(preferences={"heard_about": heard}), binder=DeterministicBinder())
    e = {x.field_key: x for x in plan.entries}
    assert e["q1"].value == heard
    if expect == "satisfied":
        assert e["q2"].satisfied_by == "q1" and not e["q2"].needs_review
    else:
        assert e["q2"].needs_review and "needs your detail" in e["q2"].reason


def test_a_question_with_its_own_condition_is_not_a_follow_up():
    form = _form([
        _field("q1", "How did you hear about us?", kind=FieldKind.SINGLE_SELECT, options=("LinkedIn", "Other")),
        _field("q2", "Do you have a Duolingo account? If yes, what is your username?"),
    ])
    plan = resolve_form(form, Memory(preferences={"heard_about": "LinkedIn"}), binder=DeterministicBinder())
    e = {x.field_key: x for x in plan.entries}
    assert e["q2"].needs_review and e["q2"].satisfied_by is None
    assert e["q2"] in plan.for_review()
