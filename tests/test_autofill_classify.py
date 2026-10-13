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


def test_a_compound_question_with_a_consent_rider_keeps_its_deterministic_answer():
    """Review of 2026-10-12, finding 2: `is_consent_asked` read a consent verb
    and a bucket noun anywhere in the label, so these were CONSENT and filled
    "Yes" from the standing consent — over the stored "No" the theme gives,
    and in the fourth case as a false work-authorisation claim."""
    from app.autofill.binder import DeterministicBinder, resolve_form
    from app.autofill.memory import Memory
    from app.autofill.schema import FormSchema

    memory = Memory(consents={"background_check": "Yes", "privacy_notice": "Agree", "terms_and_conditions": "Agree"},
                    preferences={"in_office_tolerance": "No", "open_to_relocation": "No"},
                    legal_status={"work_authorization": "No"})
    labels = {
        "This position requires a background check. Are you willing to work onsite 5 days a week in Austin?": None,
        "Are you willing to relocate? (Offers are contingent on a background check.)": "No",
        "Do you understand this role requires in-office attendance 5 days/week and acceptance of our code of conduct?": "No",
        "I understand my data will be processed per the privacy notice. Are you authorized to work in the US?": "No",
        "Are you willing to sign a confidentiality agreement and a non-compete?": None,
    }
    fields = [_field(label, key="q{}".format(i)) for i, label in enumerate(labels)]
    for field in fields:
        assert field.field_class == FieldClass.SCREENING, field.label
    plan = resolve_form(FormSchema(source="t", ats="greenhouse", posting_id="1", fields=fields),
                        memory, binder=DeterministicBinder())
    for entry, (label, expected) in zip(plan.entries, labels.items()):
        assert entry.value == expected, label
        assert entry.needs_review is (expected is None), label
        assert "standing consent" not in (entry.reason or ""), label


def test_a_consent_asked_in_one_sentence_beside_a_notice_is_still_a_consent():
    """The sentence rule must not undo M1: the ask and its subject sit in one
    sentence, and the label asks nothing else."""
    from app.autofill.memory import Memory
    sms = ("If you provided a phone number, do you consent to receiving follow-up communication "
           "via text message (or SMS) about your application? Message and data rates may apply.")   # x7
    for label in (sms,
                  "Will you consent to a background check? (All offers of employment are contingent on passing a background check.)",
                  "Privacy Policy Acknowledgment"):
        field = _field(label)
        assert field.field_class == FieldClass.CONSENT, label
    decided = Memory(consents={"sms_messages": "No"}).resolve(_field(sms))
    assert decided.value == "No" and not decided.needs_review


def test_an_sms_opt_in_asked_as_a_phone_number_box_is_a_consent_and_so_never_a_fact():
    """"If you wish to receive information via text/SMS, please indicate the
    cell number below." (x2): the applicant opts in by typing the number. As a
    CONSENT on a text control the standing decision must not be typed into it
    (memory.py's rule, audit M1); as SCREENING it would reach the second pass.
    CONSENT is the class that keeps a model out either way."""
    label = "If you wish to receive information via text/SMS, please indicate the cell number below."
    assert classify("question_1", label, FieldKind.TEXT) == FieldClass.CONSENT
    assert consent_key_for(label) == "sms_messages"

    # Review of 2026-10-12, finding 7: with the consent unset the plan asked
    # the applicant to set consents.sms_messages, and once set the field was
    # still theirs to type — a prompt for a fact that can never fill it.
    from app.autofill.binder import DeterministicBinder, resolve_form
    from app.autofill.memory import Memory
    from app.autofill.schema import FormSchema
    field = _field(label, FieldKind.TEXT, options=())
    form = FormSchema(source="t", ats="greenhouse", posting_id="1", fields=[field])
    unset = resolve_form(form, Memory(), binder=DeterministicBinder()).entries[0]
    assert unset.needs_review and unset.needs_fact is None
    decided = resolve_form(form, Memory(consents={"sms_messages": "Agree"}), binder=DeterministicBinder()).entries[0]
    assert decided.needs_review and decided.value is None and decided.needs_fact is None
    # A consent on a tick box still asks for the decision it would replay.
    box = _field("Do you consent to a background check?")
    asked = resolve_form(FormSchema(source="t", ats="greenhouse", posting_id="1", fields=[box]),
                         Memory(), binder=DeterministicBinder()).entries[0]
    assert asked.needs_fact and asked.needs_fact["key"] == "background_check"


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


# --- code audit of 2026-10-12, F2: "essential job functions" with the example value "None"

ESSENTIAL_FUNCTIONS = [
    # The corpus label (51 occurrences, 28 choice fields filled "No").
    "Do you understand the job requirements, and are you able to perform the essential job functions "
    "(with or without reasonable accommodation)?",
    "Can you perform all of the essential functions of this role with or without reasonable accommodation?",  # x6
    "Are you able to perform the essential functions of the job, with or without reasonable accommodation?",
    "Can you perform the essential job functions of this position?",
]

ACCOMMODATION_REQUESTS = [
    "Do you require any reasonable adjustments or accommodations to participate in the recruitment process?",
    "If you require any support or adjustments during the recruitment process for any reason, please indicate here",
    "Please describe any need for a reasonable accommodation",
]


@pytest.mark.parametrize("label", ESSENTIAL_FUNCTIONS)
def test_the_ability_question_keys_essential_functions_in_every_wording(label):
    """F2: `\\bessential functions\\b` missed the three-word form, which then
    fell to the accommodation key beside it in the same table."""
    assert memory_only_key_for(label) == "essential_functions"
    assert memory_key_for("question_1", label, FieldKind.SINGLE_SELECT) == "essential_functions"
    assert classify("question_1", label, FieldKind.SINGLE_SELECT, option_count=3) == FieldClass.LEGAL


@pytest.mark.parametrize("label", ACCOMMODATION_REQUESTS)
def test_an_accommodation_request_still_keys_accommodation_needs(label):
    assert memory_only_key_for(label) == "accommodation_needs"


@pytest.mark.parametrize("label", [
    "Describe the essential functions of your last role",
    "Which essential job functions of a backend engineer do you enjoy most?",
    "Essential functionality you would add to our product",
])
def test_essential_functions_in_another_sense_is_still_memory_only_and_never_a_fill(label):
    """A near miss lands on LEGAL (review-only) or on no key; what it must
    never do is reach a model. The third label has no key at all."""
    assert classify("question_1", label, FieldKind.TEXT) in (FieldClass.LEGAL, FieldClass.NARRATIVE, FieldClass.SCREENING)
    assert memory_only_key_for(label) in ("essential_functions", None)
    assert memory_only_key_for("Essential functionality you would add to our product") is None


def test_a_capable_applicant_is_never_written_as_unable():
    """Probe p18's shape: the documented example value for accommodation_needs
    is "None"; with essential_functions "Yes" the ability question fills
    "Yes", and the stored "None" is never read as the option "No"."""
    from app.autofill.binder import DeterministicBinder, resolve_form
    from app.autofill.memory import Memory
    from app.autofill.schema import FormSchema

    memory = Memory(protected={"accommodation_needs": "None", "essential_functions": "Yes"})
    three_way = ("Yes, without accommodations", "Yes, with accommodations", "No")
    fields = [_field(label, key="q{}".format(i)) for i, label in enumerate(ESSENTIAL_FUNCTIONS)]
    fields += [_field(label, options=three_way, key="t{}".format(i)) for i, label in enumerate(ESSENTIAL_FUNCTIONS)]
    fields += [_field(label, key="a{}".format(i)) for i, label in enumerate(ACCOMMODATION_REQUESTS[:2])]
    plan = resolve_form(FormSchema(source="t", ats="greenhouse", posting_id="1", fields=fields),
                        memory, binder=DeterministicBinder())
    for entry in plan.entries:
        assert entry.value != "No", entry.label
        if entry.field_key.startswith("q"):
            assert entry.value == "Yes" and not entry.needs_review, entry.label
        elif entry.field_key.startswith("t"):
            # Two "Yes" options: with or without accommodation is the
            # applicant's call, so review with the stored "Yes" intact (or
            # satisfied by the same question's yes/no sibling above).
            assert entry.value == "Yes" and (entry.needs_review or entry.satisfied_by), entry.label
        else:
            # "None" on a yes/no accommodation question is a decision the
            # matcher cannot make from spelling: review, with the value intact.
            assert entry.needs_review or entry.skipped, entry.label


# --- code audit of 2026-10-12, F36 / F15(b): `_DECISIONS_NOT_FACTS` under the compound-label guard

def test_a_consent_rider_on_a_theme_question_is_the_theme_question():
    """"Do you need sponsorship? By submitting you agree to our privacy
    policy." was CONSENT by `_DECISIONS_NOT_FACTS`, which ran before the
    guard §56 put on `is_consent_asked`, and filled "Yes" from privacy_notice
    while needs_sponsorship said "No" (probe p20)."""
    from app.autofill.binder import DeterministicBinder, resolve_form
    from app.autofill.memory import Memory
    from app.autofill.schema import FormSchema

    memory = Memory(consents={"privacy_notice": "Agree", "terms_and_conditions": "Agree", "sms_messages": "Yes"},
                    legal_status={"needs_sponsorship": "No", "work_authorization": "Yes", "age_18_plus": "Yes"})
    labels = {
        "Do you need sponsorship? By submitting you agree to our privacy policy.": "No",
        "Are you authorized to work in the US? Please acknowledge that you have read and agree to our Privacy Policy.": "Yes",
        # One "?", two asks: the age theme answers it (binder's theme-wins
        # rule, §56); what must not happen is the terms consent answering it.
        "Are you 18 or older and do you agree to the terms and conditions?": "Yes",
        "Do you have a GitHub? By applying you consent to the processing of your personal data.": None,
        "Do you agree to the terms and conditions and are you authorized to work in the US?": "Yes",   # F15(b)
    }
    fields = [_field(label, key="q{}".format(i)) for i, label in enumerate(labels)]
    for field in fields:
        assert field.field_class == FieldClass.SCREENING, field.label
    plan = resolve_form(FormSchema(source="t", ats="greenhouse", posting_id="1", fields=fields),
                        memory, binder=DeterministicBinder())
    for entry, (label, expected) in zip(plan.entries, labels.items()):
        assert "standing consent" not in (entry.reason or ""), label
        assert entry.theme != "none" and "consent" not in (entry.theme or ""), label
        if expected is None:
            assert entry.value is None and entry.needs_review, label
        else:
            assert entry.value == expected and not entry.needs_review, label


@pytest.mark.parametrize("label,options", [
    ("Please acknowledge that you have read and agree to our Privacy Policy", 1),            # x6
    ("Do you opt-in to receive WhatsApp messages from Stripe Recruiting?", 2),               # x2
    ("I consent to have my personal data disclosed to other Momentum Group entities", 2),    # x2
    ("[Compensation] Do you accept the listed salary range for this position?", 2),         # x2
    ("To ensure we can focus fully on our conversation, we use Brighthire to record interviews. Do you consent?", 2),
])
def test_the_decisions_not_facts_table_still_catches_a_plain_consent(label, options):
    assert classify("question_1", label, FieldKind.SINGLE_SELECT, option_count=options) == FieldClass.CONSENT
