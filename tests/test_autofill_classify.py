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


@pytest.mark.parametrize("label", [
    # Review of 2026-10-12, finding 1: between the protected pattern's
    # accommodation exclusion and `_ACCOMMODATION`'s narrower wording, these
    # were SCREENING with no key, and the model gate could answer them.
    "Do you have a disability or require an accommodation?",
    "Do you have a disability? If you need an accommodation to complete this application, please email us.",
    "Disability status (if you need an accommodation, let us know)",
])
def test_a_disability_question_with_accommodation_wording_is_legal_and_keyless(label):
    from app.autofill.binder import DeterministicBinder, resolve_form
    from app.autofill.memory import Memory
    from app.autofill.schema import FormSchema

    field = _field(label, options=("Yes", "No", "I don't wish to answer"))
    assert field.field_class == FieldClass.LEGAL
    assert not field.allows_model_judgement()
    # Neither the EEOC answer nor the accommodation note answers it.
    assert memory_key_for("question_1", label, FieldKind.SINGLE_SELECT) is None
    memory = Memory(protected={"disability_status": "I don't wish to answer"},
                    legal_status={"accommodation_needs": "None"})
    form = FormSchema(source="t", ats="greenhouse", posting_id="1", fields=[field])
    entry = resolve_form(form, memory, binder=DeterministicBinder()).entries[0]
    assert entry.needs_review and entry.value is None and entry.needs_fact is None


def test_the_vendor_marker_outranks_accommodation_wording():
    """Greenhouse's compliance block names the EEOC disability question by key
    and its official text mentions accommodation. The vendor's marker is
    exact and wins; only custom keys are decided by wording."""
    label = ("Please check one of the boxes below: ... a disability is a condition that "
             "substantially limits ... reasonable accommodation")
    assert memory_key_for("disability_status", label) == "disability_status"
    assert memory_key_for("abc__systemfield_eeoc_disability_status", label) == "disability_status"
    assert memory_key_for("question_1", "Will you need housing accommodation in Seattle?") is None

    # Greenhouse's numbered demographic block is vendor-marked too, and only
    # the label says which question it is. Review of 2026-10-12, finding 4:
    # the custom table's accommodation exclusion let this fall through to
    # the memory-only key, and the stored accommodation note ("No") was
    # selected as the EEOC answer over a stored decline.
    from app.autofill.memory import Memory
    eeoc = "Do you have a disability? (If you need a reasonable accommodation for the application process, contact us)"
    options = ("Yes, I have a disability", "No, I don't have a disability", "I don't wish to answer")
    memory = Memory(protected={"disability_status": "I don't wish to answer"},
                    legal_status={"accommodation_needs": "No"})
    vendor = _field(eeoc, options=options, key="demographic_answers.123")
    assert vendor.field_class == FieldClass.LEGAL
    assert memory_key_for(vendor.key, eeoc, FieldKind.SINGLE_SELECT) == "disability_status"
    replayed = memory.resolve(vendor)
    assert replayed.value == "I don't wish to answer" and not replayed.needs_review

    # The same wording on a custom key and a choice control is neither the
    # accommodation box nor safely the EEOC question: no key, review-only.
    custom = _field(eeoc, options=options)
    assert custom.field_class == FieldClass.LEGAL
    assert memory_key_for(custom.key, eeoc, FieldKind.SINGLE_SELECT) is None
    asked = memory.resolve(custom)
    assert asked.needs_review and asked.value is None
    # A free-text accommodation box that mentions a disability keeps its key.
    assert memory_key_for("question_1", eeoc, FieldKind.TEXT) == "accommodation_needs"
    assert memory_key_for("question_1", "If you require any support or adjustments during the recruitment process, "
                          "including those related to a disability, please indicate here", FieldKind.SINGLE_SELECT) == "accommodation_needs"


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


# --- H1: Hispanic/Latino is its own protected answer, replayed exactly -------

@pytest.mark.parametrize("key,label", [
    ("hispanic_ethnicity", "Hispanic/Latino"),                 # Greenhouse's page key for the EEO yes/no
    ("question_1", "Are you Hispanic or Latino?"),
    ("question_1", "Hispanic/Latino"),
    ("question_1", "Hispanic or Latino?"),
    ("question_1", "Do you identify as Hispanic, Latino or Latinx?"),
])
def test_hispanic_latino_resolves_to_its_own_protected_key(key, label):
    """Audit H1: the extension derived this answer from the stored race by
    substring — Yes for Ashby's "White (Not Hispanic or Latino)", No for a
    stored decline. A protected characteristic produced by string logic is a
    judgement, not replay. It is its own stored answer now."""
    from app.autofill.memory import Memory
    from app.autofill.schema import FillSource

    assert protected_key_for(key, label) == "hispanic_latino"
    assert memory_key_for(key, label, FieldKind.SINGLE_SELECT) == "hispanic_latino"
    field = _field(label, options=("Yes", "No", "Decline To Self Identify"), key=key)
    assert field.field_class == FieldClass.LEGAL
    assert not field.allows_model_judgement()
    assert not field.allows_option_translation()

    # Replay only. A stored race, whatever its wording, never answers it.
    race_only = Memory(protected={"race_ethnicity": "White (Not Hispanic or Latino)"})
    assert race_only.resolve(field).needs_review
    decline = Memory(protected={"hispanic_latino": "Decline To Self Identify"}).resolve(field)
    assert decline.value == "Decline To Self Identify" and decline.source == FillSource.MEMORY
    assert Memory(protected={"hispanic_latino": "No"}).resolve(field).value == "No"


def test_the_race_question_listing_hispanic_as_an_option_stays_the_race_question():
    assert protected_key_for("question_1", "Race/Ethnicity (Hispanic or Latino, White, Asian, ...)") == "race_ethnicity"
    assert protected_key_for("race", "Race") == "race_ethnicity"
    assert protected_key_for("question_1", "Please indicate your race or ethnicity") == "race_ethnicity"


# --- M9: an address line reads the stored street address ---------------------

def test_address_lines_resolve_from_the_stored_street_address():
    """Audit M9: "Address 1" (12 required fields in the R48 gap list) and
    "Address" reached theme WORK_ADDRESS, whose key `work_address` exists in
    no profile; the stored street_address was never consulted."""
    from app.autofill.binder import DeterministicBinder, resolve_form
    from app.autofill.memory import Memory
    from app.autofill.schema import FillSource, FormSchema
    from app.autofill.themes import QuestionTheme, THEME_MEMORY_KEYS

    assert THEME_MEMORY_KEYS[QuestionTheme.WORK_ADDRESS] == "street_address"
    memory = Memory(facts={"street_address": "1 Main St", "email": "ada@example.com"})
    fields = [_field(label, FieldKind.TEXT, options=(), key="q{}".format(i))
              for i, label in enumerate(["Address 1", "Address", "Street address", "Home Address",
                                         "Address 2", "What address would you work from?"])]
    for f in fields[:4]:
        assert f.field_class == FieldClass.CORE and memory_key_for(f.key, f.label, FieldKind.TEXT) == "street_address", f.label
    assert memory_key_for("q4", "Address 2", FieldKind.TEXT) == "street_address_2"
    assert memory_key_for("q4", "Street Address (Optional Second Line)", FieldKind.TEXT) == "street_address_2"

    form = FormSchema(source="t", ats="greenhouse", posting_id="1", fields=fields)
    plan = resolve_form(form, memory, binder=DeterministicBinder())
    by = {e.label: e for e in plan.entries}
    for label in ("Address 1", "Address", "Street address", "Home Address", "What address would you work from?"):
        assert by[label].value == "1 Main St" and not by[label].needs_review, label
        assert by[label].source == FillSource.MEMORY
    # The second line stays its own optional fact: blank, not the first line again.
    assert by["Address 2"].skipped and by["Address 2"].value is None


def test_an_unset_street_address_is_asked_for_under_its_own_key():
    from app.autofill.binder import resolve_form
    from app.autofill.memory import Memory
    from app.autofill.schema import FormSchema

    field = _field("Address 1", FieldKind.TEXT, options=())
    field.required = True
    entry = resolve_form(FormSchema(source="t", ats="greenhouse", posting_id="1", fields=[field]), Memory(), binder=None).entries[0]
    assert entry.needs_review and entry.needs_fact and entry.needs_fact["key"] == "street_address"


def test_a_university_email_address_is_not_a_street_address():
    from app.autofill.binder import DeterministicBinder
    from app.autofill.themes import QuestionTheme

    label = "Please provide your university email address"
    assert DeterministicBinder().classify_themes([label])[label][0] != QuestionTheme.WORK_ADDRESS
    assert memory_key_for("question_1", label, FieldKind.TEXT) != "street_address"
