"""
Endpoint tests.

The behaviours worth guarding are the ones that make the stateless design safe:
nothing is stored, nothing is submitted, the model path degrades rather than
fails, and no policy-barred field ever comes back with a value.
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from app.autofill.schema import FieldClass
from app.main import app

client = TestClient(app)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "forms")


def _fixture(name):
    with open(os.path.join(FIXTURE_DIR, name)) as handle:
        return json.load(handle)


PROFILE = {
    "facts": {"first_name": "Ada", "last_name": "Lovelace",
              "email": "ada@example.com", "phone": "3019063249",
              "linkedin": "https://linkedin.com/in/ada"},
    "education": {"university": "Cornell", "graduation_date": "May 2028",
                  "gpa": "3.8", "degree": "BS Computer Science"},
    "legal_status": {"work_authorization": "Yes", "needs_sponsorship": "No"},
    "protected": {"veteran_status": "Prefer not to say",
                  "race_ethnicity": "Prefer not to say",
                  "gender": "Prefer not to say"},
    "skip": ["cover_letter"],
}


def _plan(ats="greenhouse", fixture="greenhouse_anthropic_ae.json", **kw):
    body = {"ats": ats, "form": _fixture(fixture), "profile": PROFILE,
            "use_model": False}
    body.update(kw)
    return client.post("/api/autofill/plan", json=body)


# --- the contract ------------------------------------------------------------

def test_a_greenhouse_form_resolves(): 
    response = _plan()

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["total"] == len(body["plan"]["entries"])
    assert body["summary"]["filled"] > 0


def test_a_real_intern_posting_resolves():
    """The corpus this is actually built for. Interns are gated harder than the
    senior roles in the other fixtures — 39.9% screening versus 26.6%."""
    response = _plan(fixture="greenhouse_intern_swe.json")

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["filled"] >= 5
    assert body["summary"]["total"] >= 14


def test_an_ashby_dom_extract_resolves():
    response = _plan(ats="ashby", fixture="ashby_notion_swe.json")

    assert response.status_code == 200
    assert response.json()["summary"]["filled"] > 0


def test_every_field_is_in_exactly_one_state():
    body = _plan().json()
    summary = body["summary"]

    assert (summary["filled"] + summary["satisfied"] + summary["skipped"]
            + summary["attach"] + summary["review"] == summary["total"])


def test_phone_is_normalised_through_the_endpoint():
    """The profile stores bare digits; the form gets the canonical form."""
    entries = _plan().json()["plan"]["entries"]
    phone = [e for e in entries if e["field_key"] == "phone"][0]

    assert phone["value"] == "301-906-3249"


# --- what must never come back -----------------------------------------------

def test_no_model_source_reaches_a_protected_field():
    """The invariant, asserted at the boundary a client actually calls."""
    body = _plan().json()
    entries = {e["field_key"]: e for e in body["plan"]["entries"]}

    veteran = entries["veteran_status"]
    assert veteran["source"] == "memory"
    assert veteran["value"]


def test_consent_fields_never_come_back_with_a_value():
    """An arbitration agreement is signed by the applicant, never by the tool."""
    body = _plan().json()

    for entry in body["plan"]["entries"]:
        if "arbitrat" in entry["label"].lower():
            assert entry["value"] is None
            assert entry["needs_review"]


def test_a_standing_skip_is_reported_as_handled_not_missing():
    """Uses the intern fixture: neither Account Executive posting has a cover
    letter field, which is the kind of gap that comes from testing a pivot
    against the corpus it pivoted away from."""
    body = _plan(fixture="greenhouse_intern_swe.json").json()
    cover = [e for e in body["plan"]["entries"] if e["field_key"] == "cover_letter"]

    assert cover and cover[0]["skipped"]
    assert not cover[0]["needs_review"]


def test_the_plan_never_contains_a_submit_instruction():
    """There is no code path that submits, and no field in the response that
    could be read as one."""
    body = _plan().json()

    assert "submit" not in json.dumps(body).lower().replace("submitted", "")


# --- statelessness -----------------------------------------------------------

def test_two_profiles_do_not_leak_into_each_other():
    """Nothing is stored, so a second caller sees only their own data."""
    other = {"facts": {"first_name": "Grace", "email": "grace@example.com"}}

    first = _plan().json()
    second = client.post("/api/autofill/plan", json={
        "ats": "greenhouse", "form": _fixture("greenhouse_anthropic_ae.json"),
        "profile": other, "use_model": False}).json()

    by_key = lambda body, key: [e for e in body["plan"]["entries"]
                                if e["field_key"] == key][0]["value"]

    assert by_key(first, "first_name") == "Ada"
    assert by_key(second, "first_name") == "Grace"


def test_an_empty_profile_fills_nothing_and_does_not_crash():
    """First run, before onboarding."""
    response = client.post("/api/autofill/plan", json={
        "ats": "greenhouse", "form": _fixture("greenhouse_anthropic_ae.json"),
        "profile": {}, "use_model": False})

    assert response.status_code == 200
    assert response.json()["summary"]["review"] > 0


# --- failure modes -----------------------------------------------------------

def test_an_unknown_ats_is_rejected_clearly():
    response = client.post("/api/autofill/plan", json={
        "ats": "workday", "form": {}, "profile": {}})

    assert response.status_code == 400
    assert "workday" in response.json()["detail"]


def test_a_malformed_profile_is_a_422_not_a_500():
    response = client.post("/api/autofill/plan", json={
        "ats": "greenhouse", "form": _fixture("greenhouse_anthropic_ae.json"),
        "profile": {"facts": "not a mapping"}})

    assert response.status_code == 422


def test_use_model_false_makes_no_network_call(monkeypatch):
    """A caller who wants nothing to leave the machine gets a deterministic
    plan rather than an error."""
    import httpx

    def explode(*args, **kwargs):
        raise AssertionError("no network call should have been made")

    monkeypatch.setattr(httpx, "post", explode)
    assert _plan().status_code == 200


def test_health_reports_without_exercising_the_model():
    response = client.get("/api/autofill/health")

    assert response.status_code == 200
    assert "model_available" in response.json()


def test_an_install_token_is_the_rate_limit_key_and_extension_origins_pass_cors():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.rate_limit import client_key
    from starlette.requests import Request
    client = TestClient(app)
    scope = {"type": "http", "headers": [(b"x-firstplay-install", b"4b9a7d1e-2c3f-4e5a-9b8c-0d1e2f3a4b5c")],
             "client": ("10.0.0.1", 1234), "method": "POST", "path": "/api/autofill/plan", "query_string": b"", "scheme": "http", "server": ("x", 80)}
    assert client_key(Request(scope)) == "install:4b9a7d1e-2c3f-4e5a-9b8c-0d1e2f3a4b5c"
    scope["headers"] = [(b"x-firstplay-install", b"<script>")]
    assert client_key(Request(scope)) == "10.0.0.1"
    response = client.options("/api/autofill/health", headers={
        "Origin": "chrome-extension://fpigopojdgjjodoaiefaacgpbpgoccfi",
        "Access-Control-Request-Method": "POST"})
    assert response.headers.get("access-control-allow-origin") == "chrome-extension://fpigopojdgjjodoaiefaacgpbpgoccfi"


def test_health_reports_a_model_that_failed_last(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.routers import autofill as router
    from app.exceptions import JevServiceError
    client = TestClient(app)
    router._MODEL_STATUS.update({"last_error": None, "last_error_at": None, "last_ok_at": None})
    router._note_model_error(JevServiceError("API key expired"))
    health = client.get("/api/autofill/health").json()
    # Sanitised: the type of failure, never the upstream text (audit M5).
    assert health["model_last_error"] == "JevServiceError" and "expired" not in health["model_last_error"]
    assert health["model_degraded"] is (health["model_available"] is True)
    router._note_model_ok()
    assert client.get("/api/autofill/health").json()["model_degraded"] is False


# --- M6 / L2: the response says whether a model was available at all --------

def _reset_model_status():
    from app.routers import autofill as router
    router._MODEL_STATUS.clear()
    router._MODEL_STATUS.update({"last_error": None, "last_error_at": None, "last_ok_at": None})
    router._CALLER_STATUS.clear()


def test_a_plan_built_without_a_model_key_says_so(monkeypatch):
    """No key is not "degraded" and not "skipped": it is a different state,
    and a caller that caches plans must not cache this one."""
    from app.routers import autofill as router
    _reset_model_status()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    body = _plan(use_model=True).json()
    assert body["model_available"] is False
    assert body["model_degraded"] is False and body["model_skipped"] is False
    assert body["jev_calls"] == 0 and body["summary"]["filled"] > 0
    # A missing key is not recorded as a model failure.
    assert router._MODEL_STATUS["last_error_at"] is None
    assert client.get("/api/autofill/health").json()["model_available"] is False


def test_model_available_reports_the_key_not_the_request(monkeypatch):
    _reset_model_status()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-a-real-key")
    body = _plan(use_model=False).json()
    assert body["model_available"] is True and body["model_degraded"] is False


@pytest.mark.parametrize("error,summary", [
    ("JevServiceError", "HTTP 401 Unauthorized: {\"error\": {\"message\": \"User not found; key sk-or-v1-abcdef expired\"}}", ),
    ("JevServiceError", "decisions call timed out after 8.0s"),
    ("JevServiceError", "transport error: connection reset by peer"),
    ("JevConfigurationError", "OPENROUTER_API_KEY is not set. Add it to your .env file."),
    ("JevServiceError", "something else entirely " + "x" * 300),
])
def test_the_last_model_error_is_a_short_sanitised_summary(error, summary):
    from app.routers import autofill as router
    from app import exceptions
    text = router._summarise_error(getattr(exceptions, error)(summary))
    assert len(text) <= 80
    assert "sk-or" not in text and "expired" not in text and ".env" not in text and "xxx" not in text
    expected = {"HTTP 401": "401 from upstream", "timed out": "upstream timed out",
                "transport": "upstream unreachable", "not set": "model not configured"}
    for needle, value in expected.items():
        if needle in summary:
            assert text == value


def test_model_degradation_is_per_caller_with_a_global_fallback(monkeypatch):
    """One caller's failure no longer marks a caller who has succeeded since
    as degraded; a caller with no record inherits the global signal, which
    is what an expired key looks like to everyone."""
    from app.routers import autofill as router
    from app.exceptions import JevServiceError
    _reset_model_status()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-a-real-key")

    router._note_model_ok("install:bbb")
    router._note_model_error(JevServiceError("HTTP 500"), "install:aaa")
    assert router._model_degraded("install:aaa") is True
    assert router._model_degraded("install:bbb") is False
    assert router._model_degraded("install:new") is True      # global fallback
    assert router._model_degraded() is True                   # /health, process-wide

    def plan_for(token):
        return client.post("/api/autofill/plan", headers={"X-FirstPlay-Install": token},
                           json={"ats": "greenhouse", "form": _fixture("greenhouse_anthropic_ae.json"),
                                 "profile": PROFILE, "use_model": False}).json()
    assert plan_for("aaa")["model_degraded"] is True
    assert plan_for("bbb")["model_degraded"] is False

    router._note_model_ok("install:aaa")
    assert router._model_degraded("install:aaa") is False and router._model_degraded() is False

    # Bounded: the oldest caller record is dropped, never the process.
    for i in range(router._CALLER_STATUS_MAX + 5):
        router._note_model_ok("install:{}".format(i))
    assert len(router._CALLER_STATUS) == router._CALLER_STATUS_MAX
    _reset_model_status()
