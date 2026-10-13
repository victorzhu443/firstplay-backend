"""
A scripted stand-in for `JevClient`, shared by the model-boundary tests.

The binder has exactly one seam to the network, `JevClient.decide`, and names
every call it makes ("classify …", "answer …", "match …", "second pass …",
"verify …", "judge …"). Dispatching on that name lets one object sit behind a
whole `resolve_form` and answer each gate differently, and record every call
so a test can assert what the model was shown.
"""
import json
import re

from app.jev_client import Answer, Decision


def letters(text):
    """Lower-case alphanumerics only, so "Decline to self-identify" and the
    form's "Decline To Self Identify" compare equal."""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def call_blob(call):
    """One recorded call, state and questions, serialised."""
    return json.dumps(call["state"], default=str) + json.dumps(call["questions"], default=str)


def earlier_answers_in(call):
    return list((call["state"].get("form") or {}).get("earlier_answers", [])) if isinstance(call["state"], dict) else []


def _constant(value):
    return value if callable(value) else (lambda qid, question: value)


class ScriptedClient:
    """A fake decisions client that answers by the kind of call.

    The binder names every call it makes ("classify …", "answer …", "match …",
    "second pass …", "verify …", "judge …"), so one object can sit behind a
    whole `resolve_form` and answer each gate differently. Each script is a
    constant or a callable `(question_id, question) -> value`; a Choice script
    yields `(choice, confidence)`, a Noul script a probability. `failure` is
    an exception raised on every call, or a callable of the description that
    returns one (or None to answer normally). Every call is recorded.
    """

    def __init__(self, *, theme=("unknown", 0.2), answer=("unsure", 0.3), applies=0.02,
                 match=("none", 0.3), covered=0.05, key=("none", 0.3), verify=0.05,
                 stable=0.1, where=("new", 0.3), cost=0.00002, failure=None):
        self.theme, self.answer, self.applies = _constant(theme), _constant(answer), _constant(applies)
        self.match, self.covered, self.key = _constant(match), _constant(covered), _constant(key)
        self.verify, self.stable, self.where = _constant(verify), _constant(stable), _constant(where)
        self.cost, self.failure = cost, failure
        self.calls = []

    def decide(self, state, questions, description=""):
        self.calls.append({"state": state, "questions": questions, "description": description})
        if self.failure is not None:
            error = self.failure(description) if callable(self.failure) else self.failure
            if error is not None:
                raise error
        kind = description.split(" ", 1)[0]
        answers = {qid: self._answer(kind, qid, q) for qid, q in questions.items()}
        return Decision(answers=answers, cost_usd=self.cost, input_tokens=100)

    def _answer(self, kind, qid, question):
        if kind == "classify":
            return self._choice(self.theme, qid, question)
        if kind == "answer":
            if question["type"] == "noul":
                return self._noul(self.applies, qid, question)
            return self._choice(self.answer, qid, question)
        if kind == "match":
            return self._choice(self.match, qid, question)
        if kind == "second":
            if qid.endswith("__cov"):
                return self._noul(self.covered, qid, question)
            return self._choice(self.key, qid, question)
        if kind == "verify":
            return self._noul(self.verify, qid, question)
        if kind == "judge":
            if qid.endswith("__stable"):
                return self._noul(self.stable, qid, question)
            return self._choice(self.where, qid, question)
        raise AssertionError("the binder made a call this fake does not know: {!r}".format(kind))

    @staticmethod
    def _choice(script, qid, question):
        picked, confidence = script(qid, question)
        return Answer(type="choice", choice=picked, confidence=confidence)

    @staticmethod
    def _noul(script, qid, question):
        return Answer(type="noul", noul=float(script(qid, question)))

    def calls_of(self, kind):
        return [c for c in self.calls if c["description"].startswith(kind)]


def answer_everything(qid, question):
    """"Answer everything": the first option of every Choice, with certainty."""
    return next(iter(question["criteria"])), 1.0
