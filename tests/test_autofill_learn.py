"""
The learning loop (DECISIONS §50): the applicant's edits become episodes that
replay, and — when a model judges them stable — proposals the applicant may
promote. Offline; the Jev boundary is faked.
"""
from fastapi.testclient import TestClient

from app.autofill.binder import DeterministicBinder, resolve_form
from app.autofill.jev_binder import JevBinder
from app.autofill.learn import Observation, accept, learn, slug_for
from app.autofill.memory import Memory
from app.autofill.schema import FieldClass, FieldKind, FieldOption, FormField, FormSchema
from app.jev_client import Answer, Decision
from app.main import app


class JudgingClient:
    """Answers the stable Noul and the where-Choice from a table keyed on the answer text."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def decide(self, state, questions, description=""):
        self.calls.append({"state": state, "questions": questions})
        out = {}
        for qid, q in questions.items():
            text = q["instructions"]
            stable, key, conf = next((v for needle, v in self.table.items() if needle in text), (0.1, "new", 0.3))
            if q["type"] == "noul":
                out[qid] = Answer(type="noul", noul=stable)
            else:
                out[qid] = Answer(type="choice", choice=key, confidence=conf)
        return Decision(answers=out, cost_usd=0.00003, input_tokens=100)


def _binder(table):
    return JevBinder(client=JudgingClient(table))


def _obs(label, value, **kw):
    base = dict(ats="greenhouse", company="Acme", posting_id="1", field_key="question_1", kind="text",
                options=[], required=False, plan_value=None, plan_source=None, user_value=value)
    base.update(kw)
    return Observation(label=label, **base)


def _form(*fields):
    return FormSchema(ats="greenhouse", source="https://boards.greenhouse.io/acme/jobs/1", posting_id="1",
                      company="Acme", title="SWE Intern", fields=list(fields))


def _field(key, label, kind=FieldKind.TEXT, options=(), klass=FieldClass.SCREENING):
    return FormField(key=key, label=label, kind=kind, field_class=klass, required=False,
                     options=[FieldOption(label=o, value=o) for o in options])


def test_an_episode_replays_on_the_next_form_with_the_same_question():
    memory = Memory()
    result = learn(memory, [_obs("What is your T-shirt size?", "M")])
    assert result.learned == 1 and memory.recall_answer("What is your T-shirt size?") == "M"
    plan = resolve_form(_form(_field("question_9", "What is your T-shirt size?")), memory, binder=DeterministicBinder())
    assert plan.entries[0].value == "M" and not plan.entries[0].needs_review
    assert plan.entries[0].reason == "you answered this before"


def test_learning_the_same_observation_twice_changes_nothing():
    memory = Memory()
    learn(memory, [_obs("What is your T-shirt size?", "M")])
    second = learn(memory, [_obs("What is your T-shirt size?", "M")])
    assert second.learned == 0 and len(memory.answers) == 1


def test_an_answer_equal_to_the_plan_is_not_an_episode():
    memory = Memory()
    result = learn(memory, [_obs("Phone", "301-555-0100", plan_value="301-555-0100")])
    assert result.learned == 0 and result.ignored[0]["why"].startswith("same as the plan")


def test_consents_and_essays_are_never_learned():
    memory = Memory()
    result = learn(memory, [
        _obs("I certify that the information provided is true", "Yes"),
        _obs("Why do you want to join Acme?", "Because of the mission", kind="long_text"),
    ])
    assert result.learned == 0 and not memory.answers
    assert "consent" in result.ignored[0]["why"] and "essay" in result.ignored[1]["why"]


def test_a_protected_answer_replays_but_is_never_proposed_as_a_fact():
    memory = Memory()
    binder = _binder({"Veteran": (0.99, "new", 0.99)})
    result = learn(memory, [_obs("Veteran Status", "I am not a protected veteran",
                                 field_key="demographic_answers.veteran_status", kind="single_select",
                                 options=["I am a protected veteran", "I am not a protected veteran"])],
                   binder=binder)
    assert memory.recall_answer("Veteran Status") == "I am not a protected veteran"
    assert result.proposals == [] and result.jev_calls == 0   # never even asked


def test_a_company_specific_answer_is_kept_for_that_question_only():
    memory = Memory(facts={"phone": "1"})
    binder = _binder({"Who referred you": (0.15, "new", 0.9)})
    result = learn(memory, [_obs("Who referred you to Acme?", "Jane Doe")], binder=binder)
    assert memory.recall_answer("Who referred you to Acme?", "Acme") == "Jane Doe"
    assert memory.recall_answer("Who referred you to Acme?", "Beta") is None   # H2: scoped to Acme
    assert result.proposals == [] and "this question only" in result.ignored[0]["why"]


def test_a_stable_answer_for_an_empty_existing_key_is_proposed_not_applied():
    memory = Memory(facts={"phone": "1", "twitter": ""})
    binder = _binder({"Twitter handle": (0.97, "twitter", 0.95)})
    first = learn(memory, [_obs("Twitter handle", "@ada", company="Acme")], binder=binder)
    # One company is remembered (exact replay) but not yet proposed: the
    # two-company vote rule (docs/research/learning-agents.md §2).
    assert first.proposals == [] and memory.learned_pending["twitter"]["support"] == 1
    assert any("second company" in i["why"] for i in first.ignored)
    result = learn(memory, [_obs("Twitter handle", "@ada", company="Beta")], binder=binder)
    assert [(p.key, p.value, p.section, p.support) for p in result.proposals] == [("twitter", "@ada", "facts", 2)]
    assert memory.facts["twitter"] == ""                       # not applied
    assert memory.learned_pending["twitter"]["value"] == "@ada"
    assert accept(memory, "twitter") and memory.facts["twitter"] == "@ada" and "twitter" not in memory.learned_pending


def test_a_new_kind_of_fact_gets_a_deterministic_key_and_support_across_companies():
    memory = Memory(facts={"phone": "1"})
    binder = _binder({"university email": (0.96, "new", 0.93)})
    label = "Please provide your university email address."
    learn(memory, [_obs(label, "vz@cornell.edu", company="Acme")], binder=binder)
    result = learn(memory, [_obs(label, "vz@cornell.edu", company="Beta")], binder=binder)
    assert slug_for(label) == "university_email_address"
    assert result.proposals[0].key == "university_email_address" and result.proposals[0].support == 2
    assert accept(memory, "university_email_address") and memory.learned["university_email_address"] == "vz@cornell.edu"
    # procedural: the learned fact is now part of what the second pass may use
    from app.autofill.binder import _profile_catalog
    assert _profile_catalog(memory)["university_email_address"] == "vz@cornell.edu"


def test_an_already_stored_value_is_not_proposed_again():
    memory = Memory(facts={"twitter": "@ada"})
    binder = _binder({"Twitter": (0.97, "twitter", 0.95)})
    result = learn(memory, [_obs("Twitter", "@ada", plan_value=None)], binder=binder)
    assert result.proposals == [] and any("already stored" in i["why"] for i in result.ignored)


def test_the_learn_endpoint_round_trips_the_profile():
    client = TestClient(app)
    profile = {"facts": {"first_name": "Ada", "phone": "3015550100"}, "answers": {}}
    response = client.post("/api/autofill/learn", json={
        "profile": profile, "use_model": False,
        "observations": [{"ats": "ashby", "company": "Acme", "posting_id": "x", "field_key": "abc",
                          "label": "What is your T-shirt size?", "kind": "text", "options": [],
                          "required": False, "plan_value": None, "plan_source": None, "user_value": "M",
                          "at": "2026-10-01T00:00:00"}],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["learned"] == 1 and body["proposals"] == [] and body["engine"]
    assert Memory(**body["profile"]).recall_answer("What is your T-shirt size?") == "M"
    assert "learned" in body["profile"] and "learned_pending" in body["profile"]

    # accepting a pending proposal through the endpoint
    body["profile"]["learned_pending"] = {"t_shirt_size": {"value": "M", "section": "learned", "support": 2, "labels": [], "companies": []}}
    response = client.post("/api/autofill/learn", json={"profile": body["profile"], "use_model": False,
                                                       "observations": [], "accept": ["t_shirt_size"]})
    assert response.status_code == 200 and response.json()["accepted"] == ["t_shirt_size"]
    assert response.json()["profile"]["learned"]["t_shirt_size"] == "M"


# §54 — a choice on a core field is answered from the profile, never from an episode.
# IMC, 2026-10-12: the phone-country select displays "+1" for "United States
# +1"; the capture learned "Country" -> "+1" and every later Greenhouse form
# failed on Country.

def test_a_core_choice_is_never_an_episode():
    memory = Memory(facts={"country": "United States"})
    result = learn(memory, [_obs("Country", "+1", field_key="country", kind="single_select",
                                 plan_value="United States")])
    assert result.learned == 0 and memory.answers == {}
    assert result.ignored[0]["why"].startswith("answered from your profile")


def test_a_stale_core_episode_is_dropped_and_not_replayed():
    memory = Memory(facts={"country": "United States"})
    memory.remember_answer("Country", "+1")
    form = _form(_field("country", "Country", FieldKind.SINGLE_SELECT, klass=FieldClass.CORE))
    plan = resolve_form(form, memory, binder=DeterministicBinder())
    assert plan.entries[0].value == "United States"
    learn(memory, [_obs("Country", "+1", field_key="country", kind="single_select", plan_value="United States")])
    assert memory.recall_answer("Country") is None


def test_a_typed_core_answer_still_replays():
    memory = Memory(facts={"preferred_first_name": "Victor"})
    result = learn(memory, [_obs("Preferred name", "Vic", field_key="preferred_name", kind="text", plan_value="Victor")])
    assert result.learned == 1 and memory.recall_answer("Preferred name") == "Vic"


def test_an_option_label_that_extends_the_plan_value_is_the_same_answer():
    memory = Memory()
    result = learn(memory, [_obs("Which office?", "New York (HQ)", kind="single_select", plan_value="New York")])
    assert result.learned == 0 and result.ignored[0]["why"].startswith("same as the plan")
    changed = learn(memory, [_obs("Which office?", "London", kind="single_select", plan_value="New York")])
    assert changed.learned == 1


# H2 (audit 2026-10-12) — episodes were keyed on the label alone, so "Please
# specify" and a referral name replayed into unrelated questions and other
# companies as a confident fill.

def _plan_for(memory, company, *fields):
    form = FormSchema(ats="greenhouse", source="t", posting_id="1", company=company, title="SWE Intern",
                      fields=list(fields))
    return resolve_form(form, memory.for_company(company), binder=DeterministicBinder())


def test_a_follow_up_answer_is_never_remembered_or_replayed():
    memory = Memory()
    result = learn(memory, [_obs("Please specify", "Cornell career fair"),
                            _obs("If yes, please explain", "see above"),
                            _obs("Other", "Mastodon")])
    assert result.learned == 0 and memory.answers == {}
    assert all("follow-up" in i["why"] for i in result.ignored)

    # One stored before this rule is inert and dropped when next seen.
    memory.answers[Memory.fingerprint("Please specify")] = "Cornell career fair"
    assert memory.recall_answer("Please specify") is None
    heard = _field("heard", "How did you hear about us?", FieldKind.SINGLE_SELECT, ("LinkedIn", "Other"))
    plan = _plan_for(memory, "Beta", heard, _field("spec", "Please specify"))
    assert plan.entries[1].value != "Cornell career fair"
    learn(memory, [_obs("Please specify", "x", company="Beta")])
    assert memory.answers == {}


def test_a_label_that_merely_opens_like_a_follow_up_is_still_remembered():
    """Review of 2026-10-12, finding 5: the memory gate reused the wide
    conditional-skip regex and refused (and deleted) 144 distinct corpus
    labels, 477 instances, that are stable answers. A follow-up is refused
    only when it names its referent or is a bare "Please specify" / "Other"."""
    memory = Memory()
    kept = {"If applicable, which US state do you reside in?": "New York",
            "If applicable, what is your middle initial?": "Q",
            "Please provide additional context on your availability": "Free from June",
            "Please provide additional detail if appropriate.": "N/A"}
    result = learn(memory, [_obs(label, value) for label, value in kept.items()])
    assert result.learned == len(kept), result.ignored
    for label, value in kept.items():
        assert memory.recall_answer(label) == value, label
        plan = _plan_for(memory, "Beta", _field("q", label))
        assert plan.entries[0].value == value and plan.entries[0].reason == "you answered this before", label
    # A narrative box is not learned, but a remembered one still replays.
    assert memory.remember_answer("Additional Information", "N/A") is True
    narrative = _field("q", "Additional Information", FieldKind.LONG_TEXT, klass=FieldClass.NARRATIVE)
    assert _plan_for(memory, "Beta", narrative).entries[0].value == "N/A"

    # The real follow-ups still never are.
    for label in ("Please specify", "Please specify:", "If yes, please explain", "If other, please specify",
                  "If you selected 'Other', please list your University:", "Other", "If no, why not?"):
        assert memory.remember_answer(label, "x") is False, label
        assert memory.recall_answer(label) is None, label
    refused = learn(memory, [_obs("If yes, please explain", "see above")])
    assert refused.learned == 0 and "follow-up" in refused.ignored[0]["why"]


def test_a_referral_at_one_company_does_not_replay_at_another():
    memory = Memory()
    learn(memory, [_obs("Who referred you?", "Jane Doe", company="Acme")])
    assert len(memory.answers) == 1

    at_beta = _plan_for(memory, "Beta", _field("q", "Who referred you?"))
    assert at_beta.entries[0].value is None and at_beta.entries[0].needs_review
    at_acme = _plan_for(memory, "Acme", _field("q", "Who referred you?"))
    assert at_acme.entries[0].value == "Jane Doe" and at_acme.entries[0].reason == "you answered this before"
    # The company name is normalised the same way as for "worked here before".
    assert memory.recall_answer("Who referred you?", "Acme Labs, Inc.") == "Jane Doe"


def test_office_team_and_company_named_questions_are_company_scoped():
    from app.autofill.memory import is_company_shaped
    for label in ("Which office are you applying to?", "Which team are you most interested in?",
                  "What role are you applying for?", "Please indicate your preferred office location",
                  "Did anyone refer you to this position? If so, who?"):
        assert is_company_shaped(label), label
    assert is_company_shaped("Have you applied to Acme before?", "Acme Labs, Inc.")
    assert not is_company_shaped("Have you applied to Acme before?", "Beta")
    for label in ("What is your T-shirt size?", "What is your current role?", "Expected graduation year",
                  "What pronouns would you like our team to use?", "Are you authorized to work in the US?"):
        assert not is_company_shaped(label, "Acme"), label
    # A two-letter company name scopes nothing: it would match inside words.
    assert not is_company_shaped("Do you have an offer?", "Do")


def test_a_plain_screening_answer_still_replays_across_companies():
    memory = Memory()
    learn(memory, [_obs("What is your T-shirt size?", "M", company="Acme")])
    plan = _plan_for(memory, "Beta", _field("q", "What is your T-shirt size?"))
    assert plan.entries[0].value == "M" and plan.entries[0].reason == "you answered this before"


def test_label_only_fingerprints_stored_before_this_rule_still_replay():
    import hashlib
    from app.autofill.memory import normalize_label
    legacy = hashlib.sha256(normalize_label("What is your T-shirt size?").encode()).hexdigest()[:16]
    memory = Memory(answers={legacy: "M"})
    assert Memory.fingerprint("What is your T-shirt size?", "Acme") == legacy
    assert memory.recall_answer("What is your T-shirt size?", "Acme") == "M"
    assert Memory.fingerprint("Who referred you?", "Acme") != Memory.fingerprint("Who referred you?", "Beta")


def test_the_plan_endpoint_scopes_a_referral_to_the_request_company():
    client = TestClient(app)
    form = {"id": 1, "company_name": "Beta", "title": "SWE Intern",
            "questions": [{"label": "Who referred you?", "required": False,
                           "fields": [{"name": "question_1", "type": "input_text"}]}]}
    profile = {"answers": {Memory.fingerprint("Who referred you?", "Acme"): "Jane Doe"}}

    def entry(**kw):
        body = {"ats": "greenhouse", "form": form, "profile": profile, "use_model": False}
        body.update(kw)
        response = client.post("/api/autofill/plan", json=body)
        assert response.status_code == 200, response.text
        return next(e for e in response.json()["plan"]["entries"] if e["field_key"] == "question_1")

    assert entry()["value"] is None                      # the form says Beta
    assert entry(company="Acme")["value"] == "Jane Doe"  # the caller says Acme


# --- F8: the judge's catalog carries contact shapes, never contact values ---------

def test_the_catalog_describes_contact_details_by_shape_and_keeps_other_values():
    """Probe p12(b): email, phone, street address, LinkedIn and names reached
    the provider in every learn call. The judge needs to know the key and
    its kind, not the value; other facts keep theirs."""
    from app.autofill.learn import _catalog, contact_shape

    memory = Memory(
        facts={"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com",
               "phone": "3015550100", "street_address": "12 Analytical Way",
               "linkedin": "https://linkedin.com/in/ada", "github": "https://github.com/ada",
               "website": "https://ada.example", "pronouns": "she/her", "preferred_first_name": ""},
        education={"university": "Cornell", "gpa": "3.8"},
        legal_status={"work_authorization": "Yes"},
        learned={"university_email_address": "al123@cornell.edu", "t_shirt_size": "M"},
    )

    catalog = _catalog(memory)

    assert catalog["email"] == "an email address"
    assert catalog["phone"] == "a phone number"
    assert catalog["street_address"] == "a street address"
    assert catalog["linkedin"] == "a LinkedIn profile URL"
    assert catalog["github"] == "a GitHub profile URL"
    assert catalog["website"] == "a website URL"
    assert catalog["first_name"] == "a name" and catalog["last_name"] == "a name"
    assert catalog["university_email_address"] == "an email address"   # a learned slug
    assert catalog["preferred_first_name"] == ""                        # empty stays empty
    # Other facts keep their values: that is what the key choice runs on.
    assert catalog["university"] == "Cornell" and catalog["gpa"] == "3.8"
    assert catalog["pronouns"] == "she/her" and catalog["t_shirt_size"] == "M"
    assert catalog["work_authorization"] == "Yes"
    blob = " ".join(catalog.values())
    for leaked in ("Ada", "Lovelace", "ada@example.com", "3015550100", "Analytical", "linkedin.com", "github.com", "cornell.edu"):
        assert leaked not in blob, leaked
    assert contact_shape("university") is None and contact_shape("school_name") is None
