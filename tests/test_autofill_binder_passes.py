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
        # CORE and SCREENING answers still travel, so the gate has its context.
        assert "Email" in questions or "First Name" in questions
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
    # answer either, which is shown as its shape.
    for kind, state in binder.captured:
        if kind in ("second_pass", "verify_values"):
            assert "ada@example.com" not in repr(state), kind
    second = next(state for kind, state in binder.captured if kind == "second_pass")
    assert {"question": "Email", "answer": "an email address"} in second["form"]["earlier_answers"]


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
