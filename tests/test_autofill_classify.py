"""
Classification rules from the outside audit of 2026-10-12 (AUDIT_REPORT.md).

The audit traced 58 labels through `classify` and the deterministic binder
and found the wrong rows in one place: a keyword read in another sense. Each
test here names the audit item it closes and the label that was wrong, so a
later pattern change that re-opens the row fails with the measurement
attached.
"""
import pytest

from app.autofill.classify import (
    classify,
    consent_key_for,
    memory_key_for,
    memory_only_key_for,
    protected_key_for,
)
from app.autofill.schema import FieldClass, FieldKind, FieldOption, FormField


def _field(label, kind=FieldKind.SINGLE_SELECT, options=("Yes", "No"), key="question_1"):
    opts = [FieldOption(label=o, value=o) for o in options]
    return FormField(key=key, label=label, kind=kind, options=opts,
                     field_class=classify(key, label, kind, option_count=len(opts)))


# --- H3: an accommodation request is not the EEOC disability question --------

@pytest.mark.parametrize("label", [
    # The three wordings MEMORY_ONLY_LABEL_KEYS was written against.
    "Please describe any need for a reasonable accommodation for this hiring process",
    "If you require any support or adjustments during the recruitment process, please let us know",
    "Do you require any accommodations for the interview?",
    # The two the audit traced to `disability_status` (H3, §2 rows 3 and 4).
    "If you require accommodations due to a disability, please describe them",
    "Do you have a disability that requires accommodation during the interview?",
    "If you need assistance during the interview process, please let us know",
])
def test_accommodation_wording_never_replays_the_eeoc_disability_answer(label):
    """Audit H3: after the 2026-10-12 protected-label rule, any label with
    "disabilit" replayed the stored EEOC sentence — into a free-text
    accommodation box. The accommodation wording is the question; it has its
    own memory-only key, and it is read before the protected patterns."""
    assert memory_only_key_for(label) == "accommodation_needs"
    assert memory_key_for("question_1", label, FieldKind.TEXT) == "accommodation_needs"
    assert memory_key_for("question_1", label, FieldKind.SINGLE_SELECT) != "disability_status"
    assert classify("question_1", label, FieldKind.TEXT) == FieldClass.LEGAL


@pytest.mark.parametrize("label", [
    "Disability Status",
    "Do you have a disability?",
    "Please indicate your disability status",
])
def test_the_eeoc_disability_question_as_a_custom_label_still_replays(label):
    """The rule H3 corrected stays: a disability question with no accommodation
    wording is the EEOC question wherever it sits (Canonical, §52)."""
    assert protected_key_for("question_1", label) == "disability_status"
    assert memory_key_for("question_1", label, FieldKind.SINGLE_SELECT) == "disability_status"
    field = _field(label, options=("Yes", "No", "I don't wish to answer"))
    assert field.field_class == FieldClass.LEGAL
    assert not field.allows_model_judgement()


def test_the_vendor_marker_outranks_accommodation_wording():
    """Greenhouse's compliance block names the EEOC disability question by key
    and its official text mentions accommodation. The vendor's marker is
    exact and wins; only custom keys are decided by wording."""
    label = ("Please check one of the boxes below: ... a disability is a condition that "
             "substantially limits ... reasonable accommodation")
    assert memory_key_for("disability_status", label) == "disability_status"
    assert memory_key_for("abc__systemfield_eeoc_disability_status", label) == "disability_status"
    assert memory_key_for("question_1", "Will you need housing accommodation in Seattle?") is None


# --- M1: a standing consent asked as a question is a consent -----------------

@pytest.mark.parametrize("label,bucket", [
    ("Do you consent to a background check?", "background_check"),                 # audit probe m5
    ("Are you willing to undergo a background check?", "background_check"),         # audit probe m6
    ("Are you willing to submit to a background screening if offered the position?", "background_check"),
    ("Would you like to join our talent community for future opportunities?", "marketing_communications"),
])
def test_a_consent_bucket_asked_as_a_question_is_consent_not_screening(label, bucket):
    """Audit M1: these named a bucket in CONSENT_BUCKETS yet classified
    SCREENING, so the model gate could be asked about them. The applicant's
    standing decision for the bucket answers them; nothing else does."""
    from app.autofill.memory import Memory
    from app.autofill.schema import FillSource

    assert consent_key_for(label) == bucket
    field = _field(label)
    assert field.field_class == FieldClass.CONSENT
    assert not field.allows_model_judgement()
    assert not field.allows_option_translation()
    # Human until the decision is recorded; then the decision, from memory.
    untouched = Memory().resolve(field)
    assert untouched.needs_review and untouched.source == FillSource.HUMAN
    decided = Memory(consents={bucket: "No"}).resolve(field)
    assert decided.value == "No" and decided.source == FillSource.MEMORY and not decided.needs_review


@pytest.mark.parametrize("label", ["Agreement to Arbitrate", "AI Policy for Application",
                                   "I agree to binding arbitration of any dispute"])
def test_an_agreement_naming_no_bucket_is_human_whatever_the_profile_says(label):
    """The docstrings now say what the code does: a consent is replayed per
    bucket from the applicant's standing decisions; an attestation matching no
    bucket has no prior decision to replay and the act is the click."""
    from app.autofill.memory import Memory
    from app.autofill.schema import FillSource

    field = _field(label, options=("I Agree",))
    assert field.field_class == FieldClass.CONSENT
    assert consent_key_for(label) is None
    assert not field.is_autofillable()
    every_yes = Memory(consents={"background_check": "Yes", "terms_and_conditions": "Yes",
                                 "truthful_certification": "Yes", "privacy_notice": "Yes"})
    assert every_yes.resolve(field).source == FillSource.HUMAN


def test_a_yes_no_screening_question_without_a_bucket_still_falls_through():
    for label in ("Are you willing to work weekends?", "Do you have a valid driver's license?"):
        assert classify("question_1", label, FieldKind.SINGLE_SELECT, option_count=2) == FieldClass.SCREENING


@pytest.mark.parametrize("label,kind,options", [
    # The bucket regexes alone (written for fields already CONSENT) matched all
    # of these in the corpora; none asks for a consent.
    ("Please confirm when you will complete your university studies.", FieldKind.SINGLE_SELECT, 5),   # x13
    ("Do you have any agreement with your current employer (such as a non-compete or confidentiality agreement)?",
     FieldKind.SINGLE_SELECT, 3),                                                                     # x5
    ("Please indicate your level of experience coding in Python for data processing, scripting or automation.",
     FieldKind.SINGLE_SELECT, 5),
    # Mentions the check as a condition of the offer and asks something else.
    ("Any offer of employment with Rocket Lab is contingent on a successful criminal background check. "
     "Are you able to meet this requirement?", FieldKind.SINGLE_SELECT, 2),
])
def test_a_label_that_mentions_a_consent_subject_without_asking_stays_screening(label, kind, options):
    assert classify("question_1", label, kind, option_count=options) == FieldClass.SCREENING


def test_an_sms_opt_in_asked_as_a_phone_number_box_is_a_consent_and_so_never_a_fact():
    """"If you wish to receive information via text/SMS, please indicate the
    cell number below." (x2): the applicant opts in by typing the number. As a
    CONSENT on a text control the standing decision must not be typed into it
    (memory.py's rule, audit M1); as SCREENING it would reach the second pass.
    CONSENT is the class that keeps a model out either way."""
    label = "If you wish to receive information via text/SMS, please indicate the cell number below."
    assert classify("question_1", label, FieldKind.TEXT) == FieldClass.CONSENT
    assert consent_key_for(label) == "sms_messages"


def test_a_one_option_checkbox_naming_a_bucket_is_a_consent():
    """"Privacy Policy" as a one-option multi-select (x6) is a checkbox with
    legal text; the single-option rule covers single-selects only."""
    for label in ("Privacy Policy", "Snowflake will process your personal information in accordance with the Candidate Privacy Notice."):
        assert classify("question_1", label, FieldKind.MULTI_SELECT, option_count=1) == FieldClass.CONSENT
    assert classify("question_1", "Privacy Policy Acknowledgment", FieldKind.SINGLE_SELECT, option_count=2) == FieldClass.CONSENT


# --- M2: sensitive topics no vendor marks are LEGAL, keyless, review-only ----

SENSITIVE = [
    "What is your date of birth?", "Date of Birth", "DOB", "What is your age?", "Age",
    "Religion", "What is your religious affiliation?",
    "Marital Status", "Are you married?",
    "Nationality", "What is your national origin?", "Citizenship", "What is your citizenship?",
    "Country of citizenship", "Place of birth",
    "Have you ever been convicted of a felony?", "Have you ever been arrested?", "Criminal history",
    "Do you identify as LGBTQ+?",
    "Are you a first-generation college student?",
    "Are you a military spouse?", "Are you a veteran or the spouse of a veteran?",   # audit §2 row 5
]


@pytest.mark.parametrize("label", SENSITIVE)
def test_a_sensitive_topic_is_legal_with_no_key_and_nothing_fills_it(label):
    """Audit M2: fourteen such labels were SCREENING and reached the model gate
    or the second pass; the model declined each, which is the model behaving,
    not the design. LEGAL with no key: no model, no translation, no fact."""
    from app.autofill.binder import DeterministicBinder, resolve_form
    from app.autofill.memory import Memory
    from app.autofill.schema import FormSchema

    field = _field(label, options=("Yes", "No", "Prefer not to say"))
    assert field.field_class == FieldClass.LEGAL
    assert memory_key_for("question_1", label, FieldKind.SINGLE_SELECT) is None
    assert memory_key_for("question_1", label, FieldKind.TEXT) is None
    assert not field.allows_model_judgement()
    assert not field.allows_option_translation()

    full = Memory(facts={"first_name": "Ada", "country_of_residence": "United States"},
                  legal_status={"citizenship": "US citizen", "age_18_plus": "Yes", "work_authorization": "Yes"},
                  protected={"veteran_status": "No", "race_ethnicity": "Asian"},
                  preferences={"earliest_start": "June 2027"})
    form = FormSchema(source="t", ats="greenhouse", posting_id="1", company="Acme", fields=[field])
    entry = resolve_form(form, full, binder=DeterministicBinder()).entries[0]
    assert entry.needs_review and entry.value is None and not entry.values
    assert entry.needs_fact is None


@pytest.mark.parametrize("label,expected_class", [
    # Age eligibility is a computed theme with its own key.
    ("Are you 18 years of age or older?", FieldClass.SCREENING),
    ("Are you at least 18 years of age?", FieldClass.SCREENING),
    # Authorisation, sponsorship and residence status resolve deterministically.
    ("Are you legally authorized to work in the United States?", FieldClass.SCREENING),
    ("Will you now or in the future require sponsorship for employment visa status?", FieldClass.SCREENING),
    ("Are you a U.S. citizen or permanent resident?", FieldClass.SCREENING),
    # A criminal background *check* is a consent, not a history question.
    ("Do you consent to a criminal background check?", FieldClass.CONSENT),
    # Core aliases keep their class.
    ("Country", FieldClass.CORE),
])
def test_the_deterministic_neighbours_of_a_sensitive_topic_keep_their_class(label, expected_class):
    assert classify("question_1", label, FieldKind.SINGLE_SELECT, option_count=2) == expected_class


def test_age_eligibility_still_fills_from_the_stored_answer():
    from app.autofill.binder import DeterministicBinder, resolve_form
    from app.autofill.memory import Memory
    from app.autofill.schema import FormSchema
    from app.autofill.themes import QuestionTheme

    label = "Are you 18 years of age or older?"
    assert DeterministicBinder().classify_themes([label])[label][0] == QuestionTheme.AGE_ELIGIBILITY
    field = _field(label)
    form = FormSchema(source="t", ats="greenhouse", posting_id="1", fields=[field])
    entry = resolve_form(form, Memory(legal_status={"age_18_plus": "Yes"}), binder=DeterministicBinder()).entries[0]
    assert entry.value == "Yes" and not entry.needs_review
