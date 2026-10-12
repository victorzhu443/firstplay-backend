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
    assert memory.recall_answer("Who referred you to Acme?") == "Jane Doe"
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
