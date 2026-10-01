"""
Round 1 of "fill everything except the essays" (DECISIONS §48).

Each test names the cluster from the 2026-09-30 coverage baseline it closes
(578 Greenhouse forms, 413 Ashby forms) and the mechanism, so a later change
that re-opens a cluster fails here with the measurement attached.
"""
from datetime import date

from app.autofill.binder import DeterministicBinder, _class_standing_answer, resolve_form
from app.autofill.classify import classify, consent_key_for, is_follow_up, memory_key_for
from app.autofill.format import _as_bool, match_option
from app.autofill.memory import Memory
from app.autofill.schema import FieldClass, FieldKind, FieldOption, FillSource, FormField, FormSchema


def _field(key, label, kind=FieldKind.TEXT, options=(), required=False, klass=None):
    opts = [FieldOption(label=o, value=o) for o in options]
    return FormField(key=key, label=label, kind=kind, required=required, options=opts,
                     field_class=klass or classify(key, label, kind, option_count=len(opts)))


def _form(*fields):
    return FormSchema(ats="greenhouse", source="https://boards.greenhouse.io/acme/jobs/1", posting_id="1", company="Acme", title="SWE Intern", fields=list(fields))


# --- Ashby custom fields that restate a core fact (x37 Current Location, x22 Current Company ...)

def test_ashby_custom_core_fields_resolve_from_the_profile_without_a_model():
    memory = Memory(facts={"current_location": "Ithaca, NY", "current_employer": "Analytical Engines",
                           "linkedin": "https://linkedin.com/in/ada", "phone": "3015550100",
                           "first_name": "Ada", "last_name": "Lovelace"},
                    education={"university": "Cornell University", "degree": "BS Computer Science"})
    form = _form(
        _field("a1", "Current Location"), _field("a2", "Current Company"),
        _field("a3", "Please provide your LinkedIn profile"), _field("a4", "Please provide your phone number"),
        _field("a5", "Last Name"), _field("a6", "School"), _field("a7", "Degree"), _field("a8", "Location"),
    )
    plan = resolve_form(form, memory, binder=None)
    values = {e.field_key: e.value for e in plan.entries}
    assert values == {"a1": "Ithaca, NY", "a2": "Analytical Engines", "a3": "https://linkedin.com/in/ada",
                      "a4": "301-555-0100", "a5": "Lovelace", "a6": "Cornell University",
                      "a7": "BS Computer Science", "a8": "Ithaca, NY"}
    assert all(e.source == FillSource.MEMORY and not e.needs_review for e in plan.entries)


def test_a_checkbox_labelled_like_a_core_fact_is_not_given_the_fact():
    # "Location" as a checkbox means something else; the alias table is text-only.
    assert memory_key_for("q", "Location", FieldKind.BOOLEAN) is None


# --- optional secondary facts nobody has (x28 Address 2 / Address Line 2, x9 Twitter)

def test_an_unrecorded_second_address_line_is_left_blank_on_an_optional_field():
    plan = resolve_form(_form(_field("a", "Address Line 2"), _field("t", "Twitter")), Memory(), binder=None)
    assert all(e.skipped and not e.needs_review for e in plan.entries), [e.reason for e in plan.entries]


def test_the_same_fact_on_a_required_field_is_asked_for():
    plan = resolve_form(_form(_field("a", "Address Line 2", required=True)), Memory(), binder=None)
    assert plan.entries[0].needs_review and "not set" in plan.entries[0].reason


# --- untriggered follow-ups (x24 "If other, please specify", x20 "Please specify", x18 additional detail)

def test_an_optional_specify_field_is_blank_when_the_answer_above_was_not_other():
    heard = _field("h", "How did you hear about us?", FieldKind.SINGLE_SELECT, ["LinkedIn", "Other"])
    plan = resolve_form(_form(heard, _field("s", "If other, please specify")),
                        Memory(preferences={"heard_about": "LinkedIn"}), binder=None)
    specify = plan.entries[1]
    assert specify.skipped and not specify.needs_review and "LinkedIn" in specify.reason


def test_the_follow_up_stays_for_review_when_other_was_chosen_or_the_parent_is_unsettled():
    heard = _field("h", "How did you hear about us?", FieldKind.SINGLE_SELECT, ["LinkedIn", "Other"])
    chosen = resolve_form(_form(heard, _field("s", "If other, please specify")),
                          Memory(preferences={"heard_about": "Other"}), binder=None)
    assert chosen.entries[1].needs_review and not chosen.entries[1].skipped

    unsettled = resolve_form(_form(heard, _field("s", "Please specify")), Memory(), binder=None)
    assert unsettled.entries[1].needs_review and not unsettled.entries[1].skipped


def test_a_required_follow_up_is_never_skipped_by_this_rule():
    heard = _field("h", "How did you hear about us?", FieldKind.SINGLE_SELECT, ["LinkedIn", "Other"])
    plan = resolve_form(_form(heard, _field("s", "Please specify", required=True)),
                        Memory(preferences={"heard_about": "LinkedIn"}), binder=None)
    assert plan.entries[1].needs_review and not plan.entries[1].skipped


def test_follow_up_detection_is_by_opening_words():
    assert is_follow_up("If other, please specify")
    assert is_follow_up("Please provide additional detail if appropriate.")
    assert is_follow_up("Other")
    assert not is_follow_up("Other Website")
    assert not is_follow_up("Please provide your LinkedIn profile")


# --- protected answers against custom wording (x38 veteran sentence options, x12 Veteran Status Yes/No)

def test_a_stored_not_a_veteran_selects_the_option_that_opens_with_no():
    field = _field("v", "Are you a veteran or active member of the United States Armed Forces?",
                   FieldKind.SINGLE_SELECT,
                   ["Yes, I am a veteran or active member", "No, I am not a veteran or active member",
                    "I prefer to self-describe", "I don't wish to answer"])
    assert match_option("I am not a protected veteran", field).label.startswith("No,")
    yes_no = _field("v2", "Veteran Status", FieldKind.SINGLE_SELECT, ["Yes", "No", "I don't wish to answer"])
    assert match_option("I am not a protected veteran", yes_no).label == "No"


def test_polarity_reads_opening_words_but_never_a_decline_or_a_none():
    assert _as_bool("No, I am not a veteran or active member") is False
    assert _as_bool("Yes, I am a veteran or active member") is True
    assert _as_bool("I am authorized to work for any employer") is True
    assert _as_bool("I don't wish to answer") is None
    assert _as_bool("Not applicable") is None
    assert _as_bool("None") is None
    assert _as_bool("Yesterday") is None


# --- standing consents (x284 consent fields to review; 21 SMS opt-ins, 16 one-option privacy statements)

def test_consent_buckets_name_the_decision_and_leave_the_rest_human():
    assert consent_key_for("By selecting YES, I consent to receive recruiting SMS messages from Astranis") == "sms_messages"
    assert consent_key_for("Privacy Statement") == "privacy_notice"
    assert consent_key_for("I certify that all information I have provided in order to apply is true") == "truthful_certification"
    assert consent_key_for("Terms & Conditions") == "terms_and_conditions"
    assert consent_key_for("AI Policy for Application") is None
    assert consent_key_for("Agreement to Arbitrate") is None


def test_an_unset_consent_stays_human_and_a_set_one_picks_the_option_that_says_it():
    sms = _field("c1", "By selecting YES, I consent to receive recruiting SMS messages from Astranis",
                 FieldKind.SINGLE_SELECT, ["Yes", "No"], required=True)
    privacy = _field("c2", "Privacy Statement", FieldKind.SINGLE_SELECT, ["I Agree"], required=True)
    ai = _field("c3", "AI Policy for Application", FieldKind.SINGLE_SELECT, ["I acknowledge"], required=True)
    assert all(f.field_class == FieldClass.CONSENT for f in (sms, privacy, ai))

    blank = resolve_form(_form(sms, privacy, ai), Memory(), binder=None)
    assert all(e.needs_review and e.source == FillSource.HUMAN for e in blank.entries)
    assert "consents.sms_messages" in blank.entries[0].reason

    decided = resolve_form(_form(sms, privacy, ai),
                           Memory(consents={"sms_messages": "No", "privacy_notice": "Agree"}), binder=None)
    assert decided.entries[0].value == "No" and not decided.entries[0].needs_review
    assert decided.entries[1].value == "I Agree" and not decided.entries[1].needs_review
    assert decided.entries[2].needs_review  # the AI policy is never a standing consent
    assert all(e.source != FillSource.MODEL_DECISION for e in decided.entries)


def test_declining_a_single_option_agreement_leaves_it_to_the_applicant():
    privacy = _field("c2", "Privacy Statement", FieldKind.SINGLE_SELECT, ["I Agree"], required=True)
    plan = resolve_form(_form(privacy), Memory(consents={"privacy_notice": "No"}), binder=None)
    assert plan.entries[0].needs_review and plan.entries[0].value is None


# --- heard-about fallback (x46 required menus without the stored source)

def test_heard_about_falls_back_to_the_applicants_second_choices_in_order():
    from app.autofill.binder import _resolve_stored
    from app.autofill.themes import QuestionTheme
    field = _field("h", "How did you hear about this job?", FieldKind.SINGLE_SELECT,
                   ["Fairygodboss", "College Recruiting - Careers Services", "Job board", "Other"], required=True)
    memory = Memory(preferences={"heard_about": "Careers Website", "heard_about_fallback": "Other | Job board"})
    assert _resolve_stored(QuestionTheme.HEARD_ABOUT, field, memory).value == "Other"
    memory.preferences["heard_about_fallback"] = "Job board | Other"
    assert _resolve_stored(QuestionTheme.HEARD_ABOUT, field, memory).value == "Job board"
    memory.preferences["heard_about_fallback"] = ""
    assert _resolve_stored(QuestionTheme.HEARD_ABOUT, field, memory).needs_review


# --- class standing as a yes/no (Base Power x5: "Are you currently a Freshman or Sophomore ...")

def test_a_freshman_or_sophomore_checkbox_is_answered_from_the_stored_dates():
    this_year = date.today().year
    # Graduating in ~4 academic years from now: a freshman this year.
    freshman = Memory(education={"graduation_date": "May {}".format(this_year + (4 if date.today().month >= 8 else 3))})
    senior = Memory(education={"graduation_date": "May {}".format(this_year + (1 if date.today().month >= 8 else 0))})
    field = _field("b", "Are you currently a Freshman or Sophomore in an undergraduate program?", FieldKind.BOOLEAN)
    assert _class_standing_answer(field, freshman).value == "Yes"
    assert _class_standing_answer(field, senior).value == "No"
    assert _class_standing_answer(field, Memory()) is None
    assert _class_standing_answer(_field("x", "Senior Software Engineer referral?", FieldKind.BOOLEAN), senior) is None


# --- essays hiding in single-line controls (Ashby String: "What excites you about ... Talos?")

def test_a_single_line_question_that_asks_for_an_essay_is_narrative():
    assert classify("q", "What excites you about the opportunity to join Talos?", FieldKind.TEXT) == FieldClass.NARRATIVE
    assert classify("q", "What is one project you're really proud of?", FieldKind.TEXT) == FieldClass.NARRATIVE
    assert classify("q", "Describe your work authorization status", FieldKind.SINGLE_SELECT, option_count=3) != FieldClass.NARRATIVE
    assert classify("q", "Current Company", FieldKind.TEXT) == FieldClass.CORE


def test_the_internship_end_theme_is_stored_and_separate_from_graduation():
    from app.autofill.binder import _resolve_stored
    from app.autofill.themes import QuestionTheme, THEME_MEMORY_KEYS
    assert THEME_MEMORY_KEYS[QuestionTheme.INTERNSHIP_END] == "internship_end"
    field = _field("e", "Ideal end date")
    assert _resolve_stored(QuestionTheme.INTERNSHIP_END, field, Memory(preferences={"internship_end": "August 2027"})).value == "August 2027"


# --- sentence-shaped core labels (R48-1 Ashby held-out: Antares, Exegy, Composio, Etched)

def test_sentence_shaped_core_labels_resolve_from_the_profile():
    memory = Memory(facts={"linkedin": "https://linkedin.com/in/ada", "phone": "3015550100",
                           "github": "https://github.com/ada", "links_combined": "github.com/ada | linkedin.com/in/ada"})
    form = _form(_field("a", "Please include your LinkedIn profile"), _field("b", "Your Phone Number"),
                 _field("c", "Your LinkedIn Profile"), _field("d", "Your GitHub"),
                 _field("e", "GitHub or Portfolio URL"), _field("f", "Github/Portfolio/Twitter/Etc"))
    plan = resolve_form(form, memory, binder=None)
    values = {e.field_key: e.value for e in plan.entries}
    assert values["a"] == values["c"] == "https://linkedin.com/in/ada"
    assert values["b"] == "301-555-0100"
    assert values["d"] == "https://github.com/ada"
    assert values["e"] == values["f"] == "github.com/ada | linkedin.com/in/ada"
    assert all(e.source == FillSource.MEMORY and not e.needs_review for e in plan.entries)


def test_sentence_patterns_do_not_swallow_questions_about_those_things():
    assert memory_key_for("q", "How did you hear about us? (LinkedIn, GitHub, ...)", FieldKind.TEXT) is None
    assert memory_key_for("q", "Do you have a GitHub profile with public work?", FieldKind.BOOLEAN) is None
    assert memory_key_for("q", "Your LinkedIn", FieldKind.SINGLE_SELECT) is None


def test_a_note_field_after_a_links_field_is_an_essay_not_a_follow_up():
    # Crusoe (R48-1): "Additional information or a note you want to share" came
    # right after "Github/Portfolio/Twitter/Etc" and was skipped as its follow-up.
    assert classify("q", "Additional information or a note you want to share", FieldKind.TEXT) == FieldClass.NARRATIVE
    links = _field("l", "Github/Portfolio/Twitter/Etc")
    note = _field("n", "Additional detail", FieldKind.TEXT)
    plan = resolve_form(_form(links, note), Memory(facts={"links_combined": "github.com/ada"}), binder=None)
    assert plan.entries[1].needs_review and not plan.entries[1].skipped
