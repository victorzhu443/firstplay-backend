"""
The model boundary, executed: the real `JevBinder` behind a scripted client,
through the real passes (`resolve_form`, `_resolve_option_mismatches`,
`_second_pass`, the answer gate, the circuit breaker, `/plan` and `/learn`).

Audit 2026-10-12, F19: these passes had no executing test — the invariant
test ran the deterministic binder, which cannot produce a model decision, and
the option-gate test grepped source text. Nothing here touches the network:
the client is faked at the one seam the binder has (`JevClient.decide`), and
the endpoint tests swap `get_jev_client` for it.

Tests marked xfail point at a finding the owning stream is fixing (F1: the
protected and consent answers travel in `earlier_answers`; F8: contact
values travel in the second pass; F19: `model_skipped` is never reported when
the trip happens in a sequential gate). They are strict: when the fix lands
they fail as "unexpectedly passing" and the marker comes off.
"""
import copy
import json
import os

import pytest
from fastapi.testclient import TestClient

from app.autofill import jev_binder as jev_binder_module
from app.autofill.binder import (
    AUTOFILL_CONFIDENCE,
    FillPlanEntry,
    SUGGEST_CONFIDENCE,
    _profile_catalog,
    _resolve_option_mismatches,
    _within_options,
    resolve_form,
)
from app.autofill.format import OPTION_MISMATCH, match_option
from app.autofill.greenhouse import parse_greenhouse_job
from app.autofill.jev_binder import JevBinder, OptionCache, ThemeCache
from app.autofill.memory import Memory, Resolution
from app.autofill.schema import FieldClass, FieldKind, FieldOption, FillSource, FormField, FormSchema
from app.exceptions import JevError, JevServiceError
from app.main import app
from tests.jev_fakes import ScriptedClient, answer_everything, call_blob, earlier_answers_in, letters

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "forms")
TEST_KEY = "sk-or-test-not-a-real-key"


def _binder(client, **kw):
    return JevBinder(client=client, cache=ThemeCache(), option_cache=OptionCache(), **kw)


def _fixture(name):
    with open(os.path.join(FIXTURE_DIR, name)) as handle:
        return json.load(handle)


def _form(*fields, **kw):
    base = dict(source="t", ats="greenhouse", posting_id="1", company="Acme", title="SWE Intern")
    base.update(kw)
    return FormSchema(fields=list(fields), **base)


def _field(key, label, kind=FieldKind.SINGLE_SELECT, options=(), klass=FieldClass.SCREENING, required=False):
    return FormField(key=key, label=label, kind=kind, field_class=klass, required=required,
                     options=[FieldOption(label=o, value=str(i)) for i, o in enumerate(options)])


def _entry(field, value, **kw):
    base = dict(field_key=field.key, label=field.label, value=value, source=FillSource.MEMORY,
                needs_review=True, reason=OPTION_MISMATCH, required=field.required)
    base.update(kw)
    return FillPlanEntry(**base)


_blob = call_blob


#: Options a stored "Yes" matches no pass of `match_option`: no exact text, no
#: polarity in the opening words, no synonym, no bucket.
RELOCATION_OPTIONS = ("Relocating works for me", "Only within my current state", "Depends on the offer")


# --- §5.8 item 2: option translation, end to end -----------------------------

def test_option_translation_writes_the_forms_label_by_index_or_leaves_the_entry_for_review():
    """The model answers `opt_1`, `none`, an off-menu `unsure`, and a weak
    `opt_2`: the entry receives the form's own option label for the index, or
    keeps its stored value and stays for review. The value never comes from
    the model's text."""
    fields = [_field("q{}".format(i), "Question {}".format(i), options=RELOCATION_OPTIONS) for i in range(4)]
    for field in fields:
        assert match_option("Yes", field) is None            # precondition: deterministically a mismatch
    entries = [_entry(f, "Yes") for f in fields]

    script = {"req_0": ("opt_1", 0.93), "req_1": ("none", 0.90), "req_2": ("unsure", 0.90), "req_3": ("opt_2", 0.60)}
    client = ScriptedClient(match=lambda qid, q: script[qid])
    _resolve_option_mismatches(entries, _form(*fields), _binder(client))

    assert len(client.calls) == 1 and set(client.calls[0]["questions"]) == set(script)
    question = client.calls[0]["questions"]["req_0"]
    assert question["type"] == "choice"
    assert question["criteria"] == {"opt_0": RELOCATION_OPTIONS[0], "opt_1": RELOCATION_OPTIONS[1],
                                    "opt_2": RELOCATION_OPTIONS[2],
                                    "none": "No listed option expresses the candidate's answer."}
    # The stored answer travels in state, not in the instructions.
    assert {q["candidate_answer"] for q in client.calls[0]["state"]["questions"]} == {"Yes"}

    translated = entries[0]
    assert translated.value == RELOCATION_OPTIONS[1] and translated.values == []
    assert translated.source == FillSource.MODEL_DECISION and not translated.needs_review
    assert translated.confidence == 0.93 and "matched to this form's wording" in translated.reason

    for entry in (entries[1], entries[2]):                   # none, and an answer outside the menu
        assert entry.value == "Yes" and entry.needs_review and entry.source == FillSource.MEMORY
        assert entry.reason == "no option matches your stored answer"

    weak = entries[3]                                         # below AUTOFILL_CONFIDENCE: a suggestion
    assert weak.value == RELOCATION_OPTIONS[2] and weak.needs_review and weak.source == FillSource.MEMORY
    assert weak.confidence == 0.60 and "60%" in weak.reason


def test_a_legal_or_consent_mismatch_is_never_sent_for_translation():
    """`allows_option_translation` is the gate: a protected answer that matches
    no option stays a review item, and its stored value appears in no call."""
    screening = _field("s", "Question S", options=RELOCATION_OPTIONS)
    veteran = _field("veteran_status", "Veteran Status", klass=FieldClass.LEGAL,
                     options=("I am a protected veteran", "I am not a protected veteran", "I don't wish to answer"))
    consent = _field("sms", "May we contact you by SMS about your application?", klass=FieldClass.CONSENT,
                     options=("Yes, text me", "No thanks"))
    unknown = _field("u", "Mystery", klass=FieldClass.UNKNOWN, options=RELOCATION_OPTIONS)
    entries = [_entry(screening, "Yes"), _entry(veteran, "Served, prefer not to detail"),
               _entry(consent, "Opt me in"), _entry(unknown, "Yes")]

    client = ScriptedClient(match=("opt_0", 0.99))
    _resolve_option_mismatches(entries, _form(screening, veteran, consent, unknown), _binder(client))

    assert len(client.calls) == 1 and list(client.calls[0]["questions"]) == ["req_0"]
    blob = _blob(client.calls[0])
    for leaked in ("Served", "Opt me in", "Veteran", "SMS", "Mystery"):
        assert leaked not in blob, leaked
    assert entries[0].value == RELOCATION_OPTIONS[0] and entries[0].source == FillSource.MODEL_DECISION
    for untouched in entries[1:]:
        assert untouched.needs_review and untouched.source == FillSource.MEMORY
        assert untouched.reason == OPTION_MISMATCH


def test_a_stored_theme_answer_is_translated_through_the_whole_plan():
    """Through `resolve_form`: the relocation theme reads the stored "Yes", no
    option matches it, the model is asked once, and the entry is filled with
    this form's wording as a MODEL_DECISION."""
    field = _field("q1", "Are you open to relocation for this role?", options=RELOCATION_OPTIONS)
    client = ScriptedClient(match=("opt_1", 0.91))
    plan = resolve_form(_form(field), Memory(preferences={"open_to_relocation": "Yes"}), binder=_binder(client))

    entry = plan.entries[0]
    assert entry.value == RELOCATION_OPTIONS[1] and entry.source == FillSource.MODEL_DECISION
    assert not entry.needs_review and entry.theme == "willing_to_relocate"
    matches = client.calls_of("match")
    assert len(matches) == 1 and len(matches[0]["questions"]) == 1
    assert client.calls_of("classify") == []                  # a local pattern named the theme


# --- §5.8 item 3: the circuit breaker ----------------------------------------

def _timeout(description):
    return JevServiceError("{}: decisions call timed out after 4.0s".format(description))


def test_the_breaker_trips_on_the_first_timeout_and_skips_every_later_call():
    client = ScriptedClient(failure=lambda d: _timeout(d) if d.startswith("classify") else None)
    binder = _binder(client)
    field = _field("q", "Are you willing to travel up to 25% of the time?", options=("Yes", "No"))

    with pytest.raises(JevServiceError, match="timed out"):
        binder.classify_themes(["Which mascot do you prefer?"])
    assert binder.tripped is True and binder.calls == 0

    with pytest.raises(JevServiceError, match="model skipped"):
        binder.answer_from_profile({"applicant": {}}, [("a", field)])
    with pytest.raises(JevServiceError, match="model skipped"):
        binder.match_options([("m", "Yes", field)])
    assert len(client.calls) == 1                              # nothing after the trip reached the client


def test_an_upstream_error_that_is_not_a_timeout_does_not_trip_the_breaker():
    client = ScriptedClient(failure=lambda d: JevServiceError("upstream unavailable (HTTP 500)")
                            if d.startswith("classify") else None)
    binder = _binder(client)
    with pytest.raises(JevServiceError, match="500"):
        binder.classify_themes(["Which mascot do you prefer?"])
    assert binder.tripped is False
    field = _field("q", "Are you willing to travel up to 25% of the time?", options=("Yes", "No"))
    binder.answer_from_profile({"applicant": {}}, [("a", field)])  # reaches the client again
    assert len(client.calls) == 2


def _greenhouse_payload(*questions):
    """A minimal board-API payload: the parser adds only the Country select."""
    return {"id": 1, "company_name": "Acme", "title": "SWE Intern", "questions": list(questions)}


def _gh_question(name, label, kind="input_text", options=(), required=False):
    field = {"name": name, "type": kind}
    if options:
        field["values"] = [{"label": o, "value": i} for i, o in enumerate(options)]
    return {"label": label, "required": required, "fields": [field]}


@pytest.fixture
def model_configured(monkeypatch):
    """A key in the environment and the binder's lazy client swapped for a fake."""
    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_KEY)

    def install(client):
        monkeypatch.setattr(jev_binder_module, "get_jev_client", lambda **kw: client)
        return client

    return install


def test_plan_reports_model_skipped_when_the_breaker_trips_and_the_plan_still_completes(model_configured):
    """The speculative answer gate times out; the sponsorship question is then
    answered by its theme from the profile with no further model call, so the
    plan completes with the breaker tripped and the response says so."""
    client = model_configured(ScriptedClient(failure=lambda d: _timeout(d) if d.startswith("answer") else None))
    payload = _greenhouse_payload(
        _gh_question("first_name", "First Name"),
        _gh_question("question_1", "Will you now or in the future require visa sponsorship?",
                     "multi_value_single_select", ("Yes", "No"), required=True),
    )
    profile = {"facts": {"first_name": "Ada", "country_of_residence": "United States"},
               "legal_status": {"needs_sponsorship": "No"}}

    body = TestClient(app).post("/api/autofill/plan", json={
        "ats": "greenhouse", "form": payload, "profile": profile, "use_model": True}).json()

    assert body["model_available"] is True
    assert body["model_skipped"] is True
    assert body["jev_calls"] == 0 and body["cost_usd"] == 0.0
    assert [c["description"] for c in client.calls] == ["answer 1 question(s) from the profile"]
    entries = {e["field_key"]: e for e in body["plan"]["entries"]}
    assert entries["first_name"]["value"] == "Ada"
    assert entries["question_1"]["value"] == "No" and entries["question_1"]["source"] == "memory"
    assert not entries["question_1"]["needs_review"]


def test_a_timeout_in_a_sequential_gate_still_yields_a_deterministic_plan_and_is_recorded(model_configured):
    from app.routers import autofill as router

    client = model_configured(ScriptedClient(failure=_timeout))
    payload = _greenhouse_payload(
        _gh_question("first_name", "First Name"),
        _gh_question("question_1", "What is your favourite programming language?",
                     "multi_value_single_select", ("Python", "Rust"), required=True),
    )
    body = TestClient(app).post("/api/autofill/plan", json={
        "ats": "greenhouse", "form": payload, "profile": {"facts": {"first_name": "Ada"}},
        "use_model": True}).json()

    assert body["summary"]["filled"] >= 1 and body["model_available"] is True
    assert body["model_degraded"] is True
    assert router._MODEL_STATUS["last_error"] == "upstream timed out"
    language = next(e for e in body["plan"]["entries"] if e["field_key"] == "question_1")
    assert language["needs_review"] and language["value"] is None
    # The breaker tripped on the first timeout: one call per gate attempted,
    # none after the trip.
    assert all(c["description"].startswith(("classify", "answer")) for c in client.calls)
    assert len(client.calls) <= 2


@pytest.mark.xfail(strict=True, reason="audit F19: model_skipped stays false when the trip happens in a "
                                       "sequential gate — the JevServiceError propagates out of resolve_form "
                                       "and the router rebuilds the plan with jev=None")
def test_plan_reports_model_skipped_after_a_timeout_in_a_sequential_gate(model_configured):
    """The documented contract (`AutofillResponse.model_skipped`, `_decide`):
    after the first transport failure the rest of the plan is finished
    deterministically and the response says so — whichever gate tripped."""
    model_configured(ScriptedClient(failure=_timeout))
    payload = _greenhouse_payload(
        _gh_question("question_1", "What is your favourite programming language?",
                     "multi_value_single_select", ("Python", "Rust"), required=True),
    )
    body = TestClient(app).post("/api/autofill/plan", json={
        "ats": "greenhouse", "form": payload, "profile": {"facts": {"first_name": "Ada"}},
        "use_model": True}).json()
    assert body["model_skipped"] is True


# --- §5.8 item 4: second pass and verify -------------------------------------

def _second_pass_memory():
    return Memory(
        facts={"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com",
               "phone": "3015550100", "street_address": "12 Analytical Way", "resume_file": "/tmp/r.pdf"},
        education={"university": "Cornell", "degree": "BS Computer Science"},
        legal_status={"work_authorization": "Yes"},
        learned={"t_shirt_size": "M"},
        protected={"gender": "Woman", "veteran_status": "I am not a protected veteran"},
        consents={"sms_messages": "Yes"},
    )


TSHIRT = "What is your T-shirt size?"


def test_the_second_pass_writes_the_stored_value_verbatim_after_verification():
    memory = _second_pass_memory()
    field = _field("q1", TSHIRT, kind=FieldKind.TEXT)
    assert memory.resolve(field) is None                     # precondition: no key or episode answers it
    client = ScriptedClient(covered=0.95, key=("t_shirt_size", 0.92), verify=0.97)

    plan = resolve_form(_form(field), memory, binder=_binder(client))

    entry = plan.entries[0]
    assert entry.value == "M" and entry.values == []
    assert entry.source == FillSource.MODEL_DECISION and not entry.needs_review
    assert entry.theme == "profile.t_shirt_size"
    assert entry.confidence == pytest.approx(0.92)             # min(covered, key, verified)
    assert entry.reason.startswith("second pass: your t_shirt_size answers this")

    second, verify = client.calls_of("second"), client.calls_of("verify")
    assert len(second) == 1 and len(verify) == 1
    assert set(second[0]["questions"]) == {"sp_0__cov", "sp_0__key"}
    assert second[0]["questions"]["sp_0__cov"]["type"] == "noul"
    # The verify question names the stored value, which is what will be written.
    assert "Proposed answer: M" in verify[0]["questions"]["sp_0"]["instructions"]


def test_the_key_choice_criteria_hold_only_catalog_keys():
    """The Choice is over the profile's own keys plus `none`: nothing protected,
    no consent, no document, nothing the model could name that is not stored."""
    memory = _second_pass_memory()
    client = ScriptedClient(covered=0.95, key=("t_shirt_size", 0.92), verify=0.97)
    resolve_form(_form(_field("q1", TSHIRT, kind=FieldKind.TEXT)), memory, binder=_binder(client))

    catalog = _profile_catalog(memory)
    criteria = client.calls_of("second")[0]["questions"]["sp_0__key"]["criteria"]
    assert set(criteria) == set(catalog) | {"none"}
    for barred in ("gender", "veteran_status", "race_ethnicity", "sms_messages", "resume_file",
                   "cover_letter", "transcript_file"):
        assert barred not in criteria, barred
    assert "t_shirt_size" in criteria and "university" in criteria
    assert set(client.calls_of("second")[0]["state"]["profile"]) == set(catalog)


def test_a_verify_below_threshold_leaves_the_entry_for_review():
    client = ScriptedClient(covered=0.95, key=("t_shirt_size", 0.92), verify=0.60)
    plan = resolve_form(_form(_field("q1", TSHIRT, kind=FieldKind.TEXT)), _second_pass_memory(),
                        binder=_binder(client))
    entry = plan.entries[0]
    assert entry.needs_review and entry.value is None and entry.source == FillSource.HUMAN
    assert entry.reason == "second pass: t_shirt_size looked like the answer but did not verify (60%)"
    assert len(client.calls_of("verify")) == 1


def test_a_key_the_model_names_outside_the_catalog_is_never_written():
    """The model's text is not a value: a confident key that is not in the
    catalog is dropped, and verify is not even asked."""
    client = ScriptedClient(covered=0.95, key=("Medium", 0.99), verify=0.99)
    plan = resolve_form(_form(_field("q1", TSHIRT, kind=FieldKind.TEXT)), _second_pass_memory(),
                        binder=_binder(client))
    entry = plan.entries[0]
    assert entry.needs_review and entry.value is None
    assert entry.reason == "your profile may cover this (95%), but no single stored fact answers it"
    assert client.calls_of("verify") == []


def test_a_question_the_profile_does_not_cover_says_so():
    client = ScriptedClient(covered=0.10, key=("t_shirt_size", 0.95), verify=0.99)
    plan = resolve_form(_form(_field("q1", TSHIRT, kind=FieldKind.TEXT)), _second_pass_memory(),
                        binder=_binder(client))
    entry = plan.entries[0]
    assert entry.needs_review and entry.value is None
    assert entry.reason == "not in your profile (10% that it is covered)"
    assert client.calls_of("verify") == []


# --- §5.8 item 5: a yes/no single-select through the answer batch -----------

TRAVEL = "Are you willing to travel up to 25% of the time?"


@pytest.mark.parametrize("choice,confidence,expect", [
    ("opt_0", 0.90, "fill"),
    ("opt_0", AUTOFILL_CONFIDENCE, "fill"),
    ("opt_1", 0.70, "suggest"),
    ("opt_1", SUGGEST_CONFIDENCE, "suggest"),
    ("opt_0", 0.40, "nothing"),
    ("unsure", 0.95, "nothing"),
])
def test_a_yes_no_single_select_lands_in_the_right_confidence_band(choice, confidence, expect):
    field = _field("q1", TRAVEL, options=("Yes", "No"), required=True)
    client = ScriptedClient(answer=(choice, confidence))
    plan = resolve_form(_form(field), Memory(preferences={"open_to_travel": "Yes"}), binder=_binder(client))

    asked = client.calls_of("answer")
    assert len(asked) == 1 and list(asked[0]["questions"]) == ["spec_q1"]
    question = asked[0]["questions"]["spec_q1"]
    assert question["type"] == "choice"
    assert list(question["criteria"]) == ["opt_0", "opt_1", "unsure"]
    assert question["criteria"]["opt_0"] == "Yes" and question["criteria"]["opt_1"] == "No"
    assert TRAVEL in question["instructions"]

    entry = plan.entries[0]
    label = {"opt_0": "Yes", "opt_1": "No"}.get(choice)
    if expect == "fill":
        assert entry.value == label and entry.source == FillSource.MODEL_DECISION
        assert not entry.needs_review and entry.confidence == confidence
        assert entry.reason == "decided from your profile and your earlier answers on this form"
    elif expect == "suggest":
        assert entry.value == label and entry.needs_review and entry.source == FillSource.HUMAN
        assert entry.confidence == confidence and "{:.0%}".format(confidence) in entry.reason
    else:
        assert entry.value is None and entry.needs_review and entry.source == FillSource.HUMAN
        assert entry.reason == "nothing stored answers this yet"
    assert entry in plan.filled() if expect == "fill" else entry in plan.for_review()


def test_the_answer_gate_is_never_asked_about_a_legal_or_consent_select():
    veteran = _field("veteran_status", "Veteran Status", klass=FieldClass.LEGAL,
                     options=("I am a protected veteran", "I am not a protected veteran"))
    consent = _field("sms", "May we contact you by SMS about your application?", klass=FieldClass.CONSENT,
                     options=("Yes", "No"))
    travel = _field("q1", TRAVEL, options=("Yes", "No"))
    client = ScriptedClient(answer=("opt_0", 0.99))
    plan = resolve_form(_form(veteran, consent, travel), Memory(), binder=_binder(client))

    asked = {qid: q for call in client.calls for qid, q in call["questions"].items()}
    assert set(asked) == {"spec_q1", "theme_0"}               # the gate and the theme question, both about q1
    assert TRAVEL in asked["theme_0"]["instructions"]
    for call in client.calls:
        for leaked in ("Veteran", "SMS", "protected veteran"):
            assert leaked not in _blob(call), (call["description"], leaked)
    by_key = {e.field_key: e for e in plan.entries}
    assert by_key["veteran_status"].needs_review and by_key["veteran_status"].value is None
    assert by_key["sms"].needs_review and by_key["sms"].value is None
    assert by_key["q1"].value == "Yes" and by_key["q1"].source == FillSource.MODEL_DECISION


# --- §5.8 item 6: multi-selects ----------------------------------------------

def test_a_multi_select_is_filled_from_one_stored_theme_value():
    """"Select all that apply" where exactly one applies: the stored value
    selects that option and nothing else, from memory, with no model call
    deciding anything."""
    field = _field("q1", "How did you hear about us? Select all that apply.", kind=FieldKind.MULTI_SELECT,
                   options=("LinkedIn", "Referral", "Career Fair"))
    memory = Memory(preferences={"heard_about": "LinkedIn"})
    client = ScriptedClient(applies=0.99)                     # the gate would tick everything, if asked
    plan = resolve_form(_form(field), memory, binder=_binder(client))

    entry = plan.entries[0]
    assert entry.values == ["LinkedIn"] and entry.value is None
    assert entry.source == FillSource.MEMORY and not entry.needs_review
    assert entry.reason == "stored as heard_about"
    assert entry in plan.filled()


def test_a_multi_value_resolution_outside_the_option_set_is_refused():
    field = _field("q1", "Which offices are you open to?", kind=FieldKind.MULTI_SELECT,
                   options=("Chicago", "New York"))
    outside = _within_options(Resolution(field_key="q1", values=["Chicago", "Mars"], source=FillSource.MEMORY), field)
    assert outside.needs_review and outside.reason == OPTION_MISMATCH
    assert not outside.values                                  # nothing is selected on the applicant's behalf

    inside = _within_options(Resolution(field_key="q1", values=["Chicago"], source=FillSource.MEMORY), field)
    assert inside.values == ["Chicago"] and not inside.needs_review


# --- §5.8 item 7: /learn with a key ------------------------------------------

def _observation(label, value, **kw):
    base = dict(ats="greenhouse", company="Acme", posting_id="1", field_key="question_1", kind="text",
                options=[], required=False, plan_value=None, plan_source=None, user_value=value,
                at="2026-10-01T00:00:00")
    base.update(kw)
    base["label"] = label
    return base


LEARN_PROFILE = {
    "facts": {"first_name": "Ada", "email": "ada@example.com", "phone": "3015550100",
              "resume_file": "/tmp/r.pdf", "cover_letter": "/tmp/c.pdf", "transcript_file": "/tmp/t.pdf"},
    "education": {"university": "Cornell"},
    "legal_status": {"work_authorization": "Yes"},
    "preferences": {"heard_about": "LinkedIn"},
    "protected": {"gender": "Woman", "race_ethnicity": "Decline to self-identify",
                  "veteran_status": "I am not a protected veteran", "disability_status": "No"},
    "consents": {"sms_messages": "Yes", "privacy_notice": "Agree"},
    "answers": {},
}


def _learn_with_a_failing_judge(model_configured):
    client = model_configured(ScriptedClient(failure=JevError("judge 1 correction(s) for memory: boom")))
    response = TestClient(app).post("/api/autofill/learn", json={
        "profile": LEARN_PROFILE, "use_model": True,
        "observations": [_observation(TSHIRT, "M")]})
    assert response.status_code == 200, response.text
    return client, response.json()


def test_learn_falls_back_to_episodes_when_the_judge_fails(model_configured):
    client, body = _learn_with_a_failing_judge(model_configured)
    assert body["proposals"] == [] and body["ignored"] == []
    assert body["jev_calls"] == 0 and body["cost_usd"] == 0.0
    assert Memory(**body["profile"]).recall_answer(TSHIRT) == "M"   # the episode was kept
    assert len(client.calls) == 1                              # the judge was asked once, then abandoned
    if "model_failed" in body:                                 # F21 proposes the field; absent today
        assert body["model_failed"] is True


@pytest.mark.xfail(strict=True, reason="audit F19 (found by this test): /learn's JevError fallback re-runs "
                                       "learn() on the Memory the first run already mutated, so the episode "
                                       "it saved is reported as learned: 0")
def test_learn_counts_the_episode_it_kept_when_the_judge_fails(model_configured):
    _client, body = _learn_with_a_failing_judge(model_configured)
    assert Memory(**body["profile"]).recall_answer(TSHIRT) == "M"
    assert body["learned"] == 1


def test_the_judges_catalog_holds_no_protected_consent_or_document_key(model_configured):
    client = model_configured(ScriptedClient(stable=0.1, where=("new", 0.3)))
    response = TestClient(app).post("/api/autofill/learn", json={
        "profile": LEARN_PROFILE, "use_model": True,
        "observations": [_observation(TSHIRT, "M")]})

    assert response.status_code == 200, response.text
    assert response.json()["jev_calls"] == 1
    judged = client.calls_of("judge")
    assert len(judged) == 1
    criteria = judged[0]["questions"]["obs_0__key"]["criteria"]
    catalog = judged[0]["state"]["profile"]
    for barred in ("gender", "race_ethnicity", "veteran_status", "disability_status", "hispanic_latino",
                   "sms_messages", "privacy_notice", "marketing_communications",
                   "resume_file", "cover_letter", "transcript_file"):
        assert barred not in criteria and barred not in catalog, barred
    assert "new" in criteria and {"university", "work_authorization", "heard_about"} <= set(criteria)
    blob = _blob(judged[0])
    for leaked in ("Woman", "Decline to self-identify", "protected veteran", "/tmp/"):
        assert leaked not in blob, leaked


# --- §5.8 item 8: required follow-ups and required skips surface -------------

def test_a_required_field_the_applicant_normally_skips_is_flagged_not_skipped():
    memory = Memory(skip=["cover_letter"])
    required = _field("cover_letter", "Cover Letter", kind=FieldKind.FILE, klass=FieldClass.DOCUMENT, required=True)
    optional = _field("cover_letter", "Cover Letter", kind=FieldKind.FILE, klass=FieldClass.DOCUMENT)

    flagged = memory.resolve(required)
    assert flagged.needs_review and not flagged.skipped
    assert flagged.reason == "you normally skip cover_letter, but this form requires it"
    assert memory.resolve(optional).skipped == "you always skip cover_letter"

    plan = resolve_form(_form(required), memory, binder=_binder(ScriptedClient()))
    entry = plan.entries[0]
    assert entry.needs_review and entry.skipped is None and entry.document == "cover_letter"
    assert entry in plan.for_review() and entry not in plan.skipped_entries()
    assert resolve_form(_form(optional), memory, binder=_binder(ScriptedClient())).entries[0].skipped


def test_a_required_graduate_transcript_asked_of_an_undergraduate_is_flagged():
    memory = Memory(education={"degree": "BS Computer Science"}, facts={"transcript_file": "/tmp/t.pdf"})
    required = _field("q1", "Graduate Transcript", kind=FieldKind.FILE, klass=FieldClass.DOCUMENT, required=True)
    optional = _field("q1", "Graduate Transcript", kind=FieldKind.FILE, klass=FieldClass.DOCUMENT)

    flagged = memory.resolve(required)
    assert flagged.needs_review and not flagged.skipped
    assert flagged.reason == "asks for a graduate transcript; you are pursuing a bachelor's, but the form requires it"
    assert memory.resolve(optional).skipped == "a graduate transcript does not apply to a bachelor's student"

    plan = resolve_form(_form(required), memory, binder=_binder(ScriptedClient()))
    assert plan.entries[0].needs_review and plan.entries[0].attach is None and plan.entries[0] in plan.for_review()
    # The undergraduate transcript is attached as usual.
    undergrad = _field("q2", "Undergraduate Transcript", kind=FieldKind.FILE, klass=FieldClass.DOCUMENT, required=True)
    assert resolve_form(_form(undergrad), memory, binder=_binder(ScriptedClient())).entries[0].attach == "/tmp/t.pdf"


AUTHORIZED = "Are you legally authorized to work in the United States?"
IF_NO = "If you answered 'No' to the question above, please explain your status."
IF_YES = "If you answered 'Yes' to the question above, please provide details."


@pytest.mark.parametrize("stored,follow_up,required,expect", [
    ("No", IF_NO, True, "detail"),         # triggers on No, answered No: the applicant writes the detail
    ("No", IF_NO, False, "detail"),
    ("Yes", IF_NO, True, "required"),      # inapplicable, but required with no way to say so
    ("Yes", IF_NO, False, "satisfied"),    # inapplicable and optional: answered by the question above
    ("Yes", IF_YES, True, "detail"),
    ("No", IF_YES, True, "required"),
    ("No", IF_YES, False, "satisfied"),
])
def test_a_conditional_follow_up_reads_its_polarity_and_a_required_one_always_surfaces(
        stored, follow_up, required, expect):
    parent = _field("q1", AUTHORIZED, options=("Yes", "No"), required=True)
    child = _field("q2", follow_up, kind=FieldKind.LONG_TEXT, required=required)
    memory = Memory(legal_status={"work_authorization": stored})
    plan = resolve_form(_form(parent, child), memory, binder=_binder(ScriptedClient()))

    first, second = plan.entries
    assert first.value == stored and not first.needs_review
    assert second.value is None
    if expect == "detail":
        assert second.needs_review and second.satisfied_by is None
        assert second.reason == "you answered '{}' above, so this needs your detail".format(stored)
        assert second in plan.for_review()
    elif expect == "required":
        assert second.needs_review and second.satisfied_by is None
        assert second.reason == "not applicable after your '{}' above, but this form requires an answer".format(stored)
        assert second in plan.for_review()
    else:
        assert not second.needs_review and second.satisfied_by == "q1"
        assert second in plan.satisfied() and second not in plan.for_review()


# --- §5.8 item 10 / F1 / F8: what the model is shown --------------------------

PROTECTED = {"veteran_status": "I identify as one or more of the classifications of a protected veteran",
             "race_ethnicity": "Decline to self-identify", "gender": "Decline to self-identify",
             "hispanic_latino": "Decline To Self Identify"}
CONSENTS = {"sms_messages": "Yes", "privacy_notice": "Yes"}
CONTACT = {"email": "ada.lovelace@example.com", "phone": "3015550100", "street_address": "12 Analytical Way"}


def eeoc_form():
    """The Anthropic fixture (an EEOC compliance block, an arbitration consent)
    plus two standing consents and one screening text question, so that every
    gate — speculative, option, answer, second pass — has work to do."""
    payload = copy.deepcopy(_fixture("greenhouse_anthropic_ae.json"))
    payload["questions"].extend([
        _gh_question("question_sms", "Do you consent to receive SMS text messages from us about your application?",
                     "multi_value_single_select", ("Yes", "No"), required=True),
        _gh_question("question_privacy", "I acknowledge the candidate privacy notice",
                     "multi_value_single_select", ("I acknowledge",), required=True),
        _gh_question("question_tshirt", TSHIRT, "input_text"),
    ])
    return parse_greenhouse_job(payload, board="anthropic")


def eeoc_memory():
    return Memory(
        facts=dict(first_name="Ada", last_name="Lovelace", linkedin="https://linkedin.com/in/ada",
                   current_location="New York, NY", country_of_residence="United States", **CONTACT),
        education={"university": "Cornell", "graduation_date": "May 2028", "degree": "BS Computer Science"},
        legal_status={"work_authorization": "Yes", "needs_sponsorship": "No", "citizenship": "US Citizen"},
        preferences={"open_to_relocation": "Yes", "in_office_tolerance": "Yes"},
        protected=dict(PROTECTED), consents=dict(CONSENTS),
    )


def _sensitive_fields(form):
    return [f for f in form.fields if f.field_class in (FieldClass.LEGAL, FieldClass.CONSENT)]


def _full_plan(client):
    form, memory = eeoc_form(), eeoc_memory()
    plan = resolve_form(form, memory, binder=_binder(client, form_context={"company": "Anthropic"}))
    return form, memory, plan


def test_the_fixture_exercises_every_gate():
    """Precondition for the two prompt-content tests: the protected and consent
    answers were filled from memory, every kind of model call happened, and no
    question was ever asked *about* a protected or consent field."""
    client = ScriptedClient()
    form, memory, plan = _full_plan(client)
    by_key = {e.field_key: e for e in plan.entries}
    assert by_key["veteran_status"].value == PROTECTED["veteran_status"]
    assert letters(by_key["race"].value) == letters(PROTECTED["race_ethnicity"])   # the form's own spelling
    assert by_key["question_sms"].value == "Yes" and by_key["question_privacy"].value == "I acknowledge"
    assert all(by_key[k].source == FillSource.MEMORY
               for k in ("veteran_status", "race", "question_sms", "question_privacy"))

    kinds = {c["description"].split(" ", 1)[0] for c in client.calls}
    assert {"classify", "answer", "second"} <= kinds, kinds
    sensitive_keys = {f.key for f in _sensitive_fields(form)}
    for call in client.calls:
        for qid in call["questions"]:
            assert not any(qid.endswith(k) for k in sensitive_keys), (call["description"], qid)


@pytest.mark.xfail(strict=True, reason="audit F1: earlier_answers is built from every resolved entry with no "
                                       "class filter, so EEOC and consent answers travel to the model")
def test_no_protected_or_consent_value_or_label_reaches_the_model():
    client = ScriptedClient()
    form, memory, plan = _full_plan(client)
    assert client.calls
    values = {letters(v) for v in PROTECTED.values()}
    labels = {f.label for f in _sensitive_fields(form) if f.label}
    for call in client.calls:
        blob = letters(_blob(call))
        for value in values:
            assert value not in blob, (call["description"], value)
        for earlier in earlier_answers_in(call):
            assert earlier["question"] not in labels, (call["description"], earlier)


def _contact_values():
    return set(CONTACT.values()) | {"301-555-0100"}            # as stored, and as the plan formats the phone


@pytest.mark.xfail(strict=True, reason="audit F8 (via the F1 mechanism): earlier_answers carries the CORE "
                                       "Email / Phone / address entries, so contact values reach the answer "
                                       "gate; a CORE+SCREENING class filter alone would not stop this")
def test_earlier_answers_carry_no_contact_value():
    client = ScriptedClient()
    _full_plan(client)
    assert client.calls_of("answer")
    for call in client.calls:
        for earlier in (call["state"].get("form") or {}).get("earlier_answers", []):
            blob = json.dumps(earlier, default=str)
            for text in _contact_values():
                assert text not in blob, (call["description"], earlier)


@pytest.mark.xfail(strict=True, reason="audit F8: _profile_catalog puts email, phone and street address "
                                       "into the second-pass state and the __key criteria text")
def test_the_second_pass_shows_the_model_no_contact_value():
    client = ScriptedClient()
    _full_plan(client)
    second = client.calls_of("second")
    assert second                                              # the pass that carries the catalog ran
    for call in second + client.calls_of("verify"):
        blob = _blob(call)
        for text in _contact_values():
            assert text not in blob, (call["description"], text)
