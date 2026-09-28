"""
Jev boundary tests. Transport is mocked, so the suite stays credential-free.

The cases that matter are the three shape details that are easy to get wrong
and silently break a threshold comparison, plus the error classification, since
a caller cannot decide what to do about a failure it cannot identify.
"""
import httpx
import pytest

from app.exceptions import JevConfigurationError, JevError, JevServiceError
from app.jev_client import (
    Answer,
    JevClient,
    choice,
    noul,
    score,
)

KEY = "sk-or-v1-test-not-a-real-credential"


def _client(handler, **kw):
    """A client whose transport is a callable, so no network is touched."""
    client = JevClient(api_key=KEY, **kw)
    original = httpx.post

    def fake_post(url, **kwargs):
        return handler(url, kwargs)

    httpx.post = fake_post
    client._restore = lambda: setattr(httpx, "post", original)
    return client


@pytest.fixture
def restore_httpx():
    original = httpx.post
    yield
    httpx.post = original


# --- key validation ---------------------------------------------------------

def test_missing_key_fails_at_construction(monkeypatch):
    """Before a request is sent, not as a 401 halfway through a run."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(JevConfigurationError) as excinfo:
        JevClient()

    assert "OPENROUTER_API_KEY" in str(excinfo.value)


def test_a_key_of_the_wrong_shape_is_rejected_before_sending():
    """This is the check that would have caught a `.env` line containing the
    text of a shell command rather than a key."""
    with pytest.raises(JevConfigurationError) as excinfo:
        JevClient(api_key="grep -c OPENROUTER_API_KEY .env")

    assert "sk-or-" in str(excinfo.value)


def test_the_key_is_never_included_in_an_error_message():
    with pytest.raises(JevConfigurationError) as excinfo:
        JevClient(api_key="totally-wrong-but-secret-value")

    assert "secret-value" not in str(excinfo.value)


# --- question builders ------------------------------------------------------

def test_noul_criteria_are_a_true_false_map():
    """Not a list, and not absent by default — the shape the API documents."""
    question = noul("Is this urgent?", true_means="Asks for action today",
                    false_means="No deadline stated")

    assert question["type"] == "noul"
    assert question["criteria"] == {"true": "Asks for action today",
                                    "false": "No deadline stated"}


def test_noul_omits_criteria_when_undescribed():
    assert "criteria" not in noul("Is this urgent?")


def test_choice_rejects_more_than_the_option_limit():
    with pytest.raises(JevConfigurationError) as excinfo:
        choice("Pick one", {str(i): "option" for i in range(256)})

    assert "255" in str(excinfo.value)


def test_choice_rejects_an_empty_option_set():
    with pytest.raises(JevConfigurationError):
        choice("Pick one", {})


@pytest.mark.parametrize("levels", [1, 11])
def test_score_enforces_the_level_range(levels):
    with pytest.raises(JevConfigurationError):
        score("How severe?", ["level"] * levels)


def test_score_accepts_the_documented_range():
    assert score("How severe?", ["a", "b"])["criteria"] == ["a", "b"]


# --- the three shape details --------------------------------------------------

def test_a_noul_has_no_confidence_and_its_probability_is_the_belief():
    """Reading `.confidence` uniformly across primitives breaks on every Noul."""
    answer = Answer(type="noul", noul=0.96)

    assert answer.confidence is None
    assert answer.belief() == 0.96


def test_a_choice_belief_is_its_calibrated_confidence():
    answer = Answer(type="choice", choice="payments", confidence=0.67,
                    probabilities={"payments": 0.78, "frontend": 0.22})

    assert answer.belief() == 0.67


def test_a_score_is_a_weighted_float_with_a_legend():
    """1.99, not 2 — and the legend is what makes the float interpretable."""
    answer = Answer(type="score", score=1.99, confidence=0.99,
                    legend={"0": "can wait", "1": "this week", "2": "blocking"})

    assert answer.score == pytest.approx(1.99)
    assert answer.legend["2"] == "blocking"


def test_a_missing_belief_is_zero_not_one():
    """An absent confidence must fail a threshold, never pass it."""
    assert Answer(type="choice", choice="x").belief() == 0.0


# --- parsing a real response shape ------------------------------------------

def test_a_documented_response_parses(restore_httpx):
    body = {
        "id": "gen-dec-1", "model": "typesafe/jev-1.13-20260917", "provider": "TypeSafe",
        "answers": {
            "is_bug": {"type": "noul", "noul": 0.96},
            "team": {"type": "choice", "choice": "payments", "confidence": 0.67,
                     "probabilities": {"payments": 0.78, "frontend": 0.22, "account": 0}},
            "urgency": {"type": "score", "score": 1.99, "confidence": 0.99,
                        "probabilities": {"0": 0, "1": 0, "2": 1},
                        "legend": {"0": "can wait", "1": "this week", "2": "blocking"}},
        },
        "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.000019992},
    }
    httpx.post = lambda url, **kw: httpx.Response(200, json=body)

    decision = JevClient(api_key=KEY).decide({"ticket": "blank screen"},
                                             {"is_bug": noul("bug?")})

    assert decision["is_bug"].noul == 0.96
    assert decision["team"].choice == "payments"
    assert decision["urgency"].score == pytest.approx(1.99)
    assert decision.cost_usd == pytest.approx(0.000019992)
    assert decision.input_tokens == 476


def test_state_and_questions_are_both_sent(restore_httpx):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs.get("json") or {})
        captured["url"] = url
        return httpx.Response(200, json={"answers": {}, "usage": {}})

    httpx.post = fake_post
    JevClient(api_key=KEY).decide({"a": 1}, {"q": noul("yes?")})

    assert captured["url"].endswith("/api/alpha/decisions")
    assert captured["model"] == "typesafe/jev-1.13"
    assert captured["state"] == {"a": 1}
    assert captured["questions"]["q"]["type"] == "noul"


def test_no_questions_is_a_configuration_error():
    with pytest.raises(JevConfigurationError):
        JevClient(api_key=KEY).decide({"a": 1}, {})


# --- error classification ---------------------------------------------------

@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials_are_not_retryable(status, restore_httpx):
    httpx.post = lambda url, **kw: httpx.Response(status, json={})

    with pytest.raises(JevConfigurationError):
        JevClient(api_key=KEY).decide({}, {"q": noul("yes?")})


def test_a_422_names_the_question_set(restore_httpx):
    httpx.post = lambda url, **kw: httpx.Response(422, text="criteria too long")

    with pytest.raises(JevConfigurationError) as excinfo:
        JevClient(api_key=KEY).decide({}, {"q": noul("yes?")})

    assert "criteria too long" in str(excinfo.value)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
def test_transient_statuses_are_service_errors(status, restore_httpx):
    """529 is OpenRouter's "overloaded" and is not a standard code, so it has to
    be named explicitly or it falls through to unclassified."""
    httpx.post = lambda url, **kw: httpx.Response(status, json={})

    with pytest.raises(JevServiceError):
        JevClient(api_key=KEY).decide({}, {"q": noul("yes?")})


def test_a_timeout_is_a_service_error(restore_httpx):
    def boom(url, **kw):
        raise httpx.TimeoutException("too slow")

    httpx.post = boom

    with pytest.raises(JevServiceError):
        JevClient(api_key=KEY).decide({}, {"q": noul("yes?")})


def test_a_non_json_body_is_reported_as_such(restore_httpx):
    httpx.post = lambda url, **kw: httpx.Response(200, text="<html>nope</html>")

    with pytest.raises(JevError) as excinfo:
        JevClient(api_key=KEY).decide({}, {"q": noul("yes?")})

    assert "not JSON" in str(excinfo.value)
