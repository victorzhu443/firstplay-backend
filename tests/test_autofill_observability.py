"""
Observability and log hygiene on the autofill endpoints (audit 2026-10-12,
F9, F21, F22).

  - no log line and no error message carries an upstream body or a profile
    value (the judge's questions contain the applicant's answers);
  - a plan or a learn after a failed model call says so (`model_failed`,
    `model_error`), including when the speculative pool swallowed it;
  - every response carries `X-Request-Id`, and the log lines written while
    serving it carry the same id;
  - 422 details name field paths, never values.
"""
import logging
import re

import pytest
from fastapi.testclient import TestClient

from app.exceptions import JevConfigurationError, JevError, JevServiceError
from app.logging_config import LOG_FORMAT, RequestIdFilter
from app.main import app
from app.routers import autofill as router

client = TestClient(app)

SECRET = "SECRET-ANSWER-TEXT"
PROFILE = {"facts": {"first_name": "Ada", "email": "ada@example.com"},
           "legal_status": {"work_authorization": "Yes"}}
FORM = {"id": 1, "company_name": "Acme", "title": "Intern", "questions": [
    {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text"}]},
    {"label": "Are you willing to relocate to Ithaca for this role?", "required": False,
     "fields": [{"name": "question_1", "type": "multi_value_single_select",
                 "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
]}


@pytest.fixture
def model_configured(monkeypatch):
    monkeypatch.setattr(router, "_model_configured", lambda: True)
    router._MODEL_STATUS.update({"last_error": None, "last_error_at": None, "last_ok_at": None})
    router._CALLER_STATUS.clear()


@pytest.fixture
def log_lines():
    """Formatted lines exactly as the stdout handler would print them,
    request id included."""
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = Capture(level=logging.WARNING)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


# --- F9: /learn logs the category, never the upstream text -----------------------

def test_learn_logs_a_sanitised_category_not_the_upstream_error(monkeypatch, model_configured, log_lines):
    """Probe p08's shape: the judge raises with the (hypothetical, now
    impossible) upstream body in its text; the handler's log line must not
    repeat it, and the response says the model failed."""
    from app.autofill import jev_binder

    def boom(self, state, items):
        raise JevConfigurationError("judge 1 correction(s) for memory: the question set was "
                                    "rejected (HTTP 422): {\"error\":\"near: " + SECRET + "\"}")
    monkeypatch.setattr(jev_binder.JevBinder, "judge_observations", boom)

    response = client.post("/api/autofill/learn", json={
        "profile": PROFILE,
        "observations": [{"label": "What is your T-shirt size?", "user_value": "M", "kind": "text"}]})

    assert response.status_code == 200
    body = response.json()
    assert body["model_failed"] is True
    assert body["model_error"] and SECRET not in body["model_error"] and len(body["model_error"]) <= 80
    joined = "\n".join(log_lines)
    assert "Jev failed while judging corrections" in joined
    assert SECRET not in joined


def test_learn_without_a_model_failure_reports_none(monkeypatch):
    monkeypatch.setattr(router, "_model_configured", lambda: False)
    response = client.post("/api/autofill/learn", json={
        "profile": PROFILE, "observations": [{"label": "x", "user_value": "y"}]})
    assert response.status_code == 200
    assert response.json()["model_failed"] is False and response.json()["model_error"] is None


# --- F21: a failed model path is reported as such ---------------------------------

def test_plan_after_a_sequential_model_failure_says_model_failed(monkeypatch, model_configured, log_lines):
    from app.autofill import jev_binder

    def boom(self, labels):
        raise JevServiceError("classify 2 label(s): upstream unavailable (HTTP 503)")
    monkeypatch.setattr(jev_binder.JevBinder, "classify_themes", boom)

    response = client.post("/api/autofill/plan", json={"ats": "greenhouse", "form": FORM, "profile": PROFILE})

    assert response.status_code == 200
    body = response.json()
    assert body["model_failed"] is True
    assert body["model_error"] == "503 from upstream"
    assert body["jev_calls"] == 0 and body["summary"]["filled"] >= 1
    assert any("503 from upstream" in line for line in log_lines)


def test_plan_without_a_model_call_reports_no_failure(monkeypatch):
    monkeypatch.setattr(router, "_model_configured", lambda: False)
    response = client.post("/api/autofill/plan", json={"ats": "greenhouse", "form": FORM, "profile": PROFILE})
    assert response.status_code == 200
    assert response.json()["model_failed"] is False and response.json()["model_error"] is None


def test_a_call_that_raises_is_recorded_on_the_binder_and_logged_once(log_lines):
    """The speculative pool swallows whatever `answer_from_profile` raises.
    The record and the warning therefore live at the boundary itself:
    `_decide` marks the binder failed with the sanitised category before
    re-raising, so a swallowed failure is still visible."""
    from app.autofill.jev_binder import JevBinder, OptionCache, ThemeCache

    class FailingClient:
        def decide(self, state, questions, description="decision"):
            raise JevError("{}: unexpected HTTP 418 teapot {}".format(description, SECRET))

    binder = JevBinder(FailingClient(), cache=ThemeCache(), option_cache=OptionCache())
    assert binder.failed is False and binder.last_error is None

    with pytest.raises(JevError):
        binder._decide({}, {"q": {"type": "noul", "instructions": "?"}}, "answer 1 question(s)")

    assert binder.failed is True
    assert binder.last_error == "418 from upstream"
    matching = [line for line in log_lines if "answer 1 question(s) failed" in line]
    assert len(matching) == 1 and "418 from upstream" in matching[0] and SECRET not in matching[0]


def test_plan_reports_a_failure_the_pool_swallowed(monkeypatch, model_configured):
    """`_decide` raises on every call; whichever gate reaches it first — the
    speculative pool, which swallows, or a sequential gate, which the
    router catches — the response must say the model failed."""
    from app.autofill import jev_binder

    def boom(self, state, questions, description):
        self._record_failure(description, JevError("{}: unexpected HTTP 418".format(description)))
        raise JevError("{}: unexpected HTTP 418".format(description))
    monkeypatch.setattr(jev_binder.JevBinder, "_decide", boom)

    response = client.post("/api/autofill/plan", json={"ats": "greenhouse", "form": FORM, "profile": PROFILE})

    assert response.status_code == 200
    body = response.json()
    assert body["model_failed"] is True and body["model_error"] == "418 from upstream"
    assert body["summary"]["filled"] >= 1


# --- F21: request ids ---------------------------------------------------------------

def test_every_response_carries_a_fresh_request_id():
    first = client.get("/health")
    second = client.get("/health")

    assert re.fullmatch(r"[0-9a-f]{32}", first.headers["x-request-id"])
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


def test_an_incoming_request_id_is_not_honoured():
    response = client.get("/health", headers={"X-Request-Id": "forged"})
    assert response.headers["x-request-id"] != "forged"


def test_log_lines_written_while_serving_carry_the_response_request_id(monkeypatch, model_configured, log_lines):
    """The handler runs in a threadpool; the id must survive the hop."""
    from app.autofill import jev_binder

    def boom(self, labels):
        raise JevServiceError("upstream unavailable (HTTP 502)")
    monkeypatch.setattr(jev_binder.JevBinder, "classify_themes", boom)

    response = client.post("/api/autofill/plan", json={"ats": "greenhouse", "form": FORM, "profile": PROFILE})

    request_id = response.headers["x-request-id"]
    lines = [line for line in log_lines if "502 from upstream" in line]
    assert lines and all("[{}]".format(request_id) in line for line in lines)


def test_log_lines_outside_a_request_carry_a_dash(log_lines):
    logging.getLogger("app.test").warning("outside")
    assert any("[-] app.test: outside" in line for line in log_lines)


# --- F22: 422 details carry paths, not values -------------------------------------

def test_a_profile_that_fails_validation_is_named_by_path_not_value():
    response = client.post("/api/autofill/plan", json={
        "ats": "greenhouse", "form": FORM, "profile": {"facts": {"phone": 5550100}}})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "facts.phone" in detail and "1 validation error" in detail
    assert "5550100" not in detail


def test_learn_profile_errors_are_named_by_path_not_value():
    response = client.post("/api/autofill/learn", json={
        "profile": {"facts": {"first_name": 1}}, "observations": [{"label": "x", "user_value": "y"}]})

    assert response.status_code == 422
    assert "facts.first_name" in response.json()["detail"]
    assert "input_value" not in response.json()["detail"]


def test_request_body_422s_do_not_echo_the_input():
    """FastAPI's default detail includes each error's `input`; for these
    endpoints that is a profile value or the applicant's answer."""
    response = client.post("/api/autofill/plan", json={
        "ats": ["not", "a", "string", "ada@example.com"], "form": FORM, "profile": {}})

    assert response.status_code == 422
    rows = response.json()["detail"]
    assert rows and all(set(row) == {"loc", "msg", "type"} for row in rows)
    assert "ada@example.com" not in response.text
    assert rows[0]["loc"] == ["body", "ats"]


# --- the three model flags have one meaning each (tests stream, F19) --------------

def test_a_breaker_trip_in_a_sequential_gate_reports_model_skipped(monkeypatch, model_configured):
    """`_decide` trips on a timeout and re-raises; when that happens in a
    sequential gate the error propagates out of `resolve_form` and the
    router rebuilds deterministically. The response must still say the
    breaker tripped (`model_skipped`), that a call failed (`model_failed`,
    `model_error`), and the caller's record is degraded — one meaning per
    flag, documented on AutofillResponse."""
    from app.autofill import jev_binder

    def boom(self, labels):
        self.tripped = True
        self._record_failure("classify 2 label(s)", JevServiceError("classify: decisions call timed out after 4.0s"))
        raise JevServiceError("classify 2 label(s): decisions call timed out after 4.0s")
    monkeypatch.setattr(jev_binder.JevBinder, "classify_themes", boom)

    body = client.post("/api/autofill/plan", json={"ats": "greenhouse", "form": FORM, "profile": PROFILE}).json()

    assert body["model_skipped"] is True
    assert body["model_failed"] is True and body["model_error"] == "upstream timed out"
    assert body["model_degraded"] is True
    assert body["jev_calls"] == 0 and body["cost_usd"] == 0.0
    assert body["summary"]["filled"] >= 1


def test_learn_counts_the_episode_it_kept_when_the_judge_fails(monkeypatch, model_configured):
    """`learn` saves the episode into the Memory before the judge runs; the
    fallback used to re-run over that same Memory, find the episode stored,
    and report `learned: 0` for an episode it had kept."""
    from app.autofill import jev_binder
    from app.autofill.memory import Memory

    def boom(self, state, items):
        raise JevError("judge 1 correction(s) for memory: boom")
    monkeypatch.setattr(jev_binder.JevBinder, "judge_observations", boom)

    body = client.post("/api/autofill/learn", json={
        "profile": PROFILE, "observations": [{"label": "What is your T-shirt size?", "user_value": "M", "kind": "text"}]}).json()

    assert body["learned"] == 1
    assert Memory(**body["profile"]).recall_answer("What is your T-shirt size?") == "M"
    assert body["model_failed"] is True and body["jev_calls"] == 0
