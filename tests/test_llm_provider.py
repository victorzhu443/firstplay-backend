"""
Tests for the LLM provider switch.

`get_llm()` is the one place a chain gets its model, so LLM_PROVIDER must be
honoured there and nowhere else. These tests pin that each provider builds
the right client from the right key, and that Cohere's failure modes arrive
as the same typed exceptions the retry layer already branches on.
"""
import httpx
import pytest
from cohere import errors as cohere_errors
from cohere.core.api_error import ApiError as CohereApiError
from langchain_cohere import ChatCohere
from langchain_openai import ChatOpenAI
from unittest.mock import Mock

from app.chains.job_parser import create_job_parsing_chain
from app.chains.project_generator import create_project_generation_chain
from app.chains.resume_improver import create_resume_improvement_chain
from app.chains.resume_parser import create_resume_parsing_chain
from app.exceptions import LLMConfigurationError, LLMServiceError
from app.llm_client import (
    COHERE_DEFAULT_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    OPENAI_DEFAULT_MODEL,
    get_llm,
    get_provider,
    invoke_chain,
)

COHERE_TEST_KEY = "cohere-test-dummy-key-not-a-real-credential"


@pytest.fixture
def cohere_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "cohere")
    monkeypatch.setenv("COHERE_API_KEY", COHERE_TEST_KEY)


def _chain_raising(exc: Exception) -> Mock:
    chain = Mock()
    chain.invoke.side_effect = exc
    return chain


# --- provider selection -----------------------------------------------------

def test_default_provider_is_openai(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert get_provider() == "openai"
    assert isinstance(get_llm(), ChatOpenAI)


def test_provider_value_is_normalised(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "  Cohere ")
    assert get_provider() == "cohere"


def test_unsupported_provider_fails_at_construction(monkeypatch):
    """A typo must fail here, not as an attribute error inside a chain."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")

    with pytest.raises(ValueError) as exc_info:
        get_llm()

    assert "anthropic" in str(exc_info.value)
    assert "openai" in str(exc_info.value)
    assert "cohere" in str(exc_info.value)


# --- cohere client ----------------------------------------------------------

def test_cohere_provider_builds_chat_cohere(cohere_env):
    llm = get_llm()

    assert isinstance(llm, ChatCohere)
    assert llm.model == COHERE_DEFAULT_MODEL
    assert llm.temperature == 0.0
    assert llm.timeout_seconds == DEFAULT_TIMEOUT_SECONDS


def test_cohere_provider_uses_cohere_key_not_openai_key(cohere_env, monkeypatch):
    """The OpenAI key is seeded for the whole suite; Cohere must not need it."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    llm = get_llm()

    assert llm.cohere_api_key.get_secret_value() == COHERE_TEST_KEY


def test_cohere_provider_without_key_names_the_missing_variable(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "cohere")
    monkeypatch.delenv("COHERE_API_KEY", raising=False)

    with pytest.raises(ValueError) as exc_info:
        get_llm()

    assert "COHERE_API_KEY" in str(exc_info.value)
    assert "OPENAI_API_KEY" not in str(exc_info.value)


def test_cohere_temperature_and_timeout_are_forwarded(cohere_env):
    llm = get_llm(temperature=0.6, timeout=7)

    assert llm.temperature == 0.6
    assert llm.timeout_seconds == 7


def test_cohere_model_env_override(cohere_env, monkeypatch):
    monkeypatch.setenv("COHERE_MODEL", "command-r-plus-08-2024")
    assert get_llm().model == "command-r-plus-08-2024"


def test_explicit_model_beats_env_override(cohere_env, monkeypatch):
    monkeypatch.setenv("COHERE_MODEL", "command-r-plus-08-2024")
    assert get_llm(model="command-r7b-12-2024").model == "command-r7b-12-2024"


def test_openai_model_env_override(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    assert get_llm().model_name == "gpt-4o"


def test_openai_default_model_unchanged(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert get_llm().model_name == OPENAI_DEFAULT_MODEL == "gpt-4o-mini"


# --- every chain follows the switch -----------------------------------------

@pytest.mark.parametrize(
    "factory",
    [
        create_resume_parsing_chain,
        create_job_parsing_chain,
        create_project_generation_chain,
        create_resume_improvement_chain,
    ],
    ids=["parse_resume", "parse_job", "generate_projects", "improve_resume"],
)
def test_chains_build_on_cohere_when_selected(cohere_env, factory):
    """No chain may construct its own OpenAI client behind the switch."""
    chain = factory()

    models = [step for step in chain.steps if isinstance(step, (ChatCohere, ChatOpenAI))]

    assert len(models) == 1
    assert isinstance(models[0], ChatCohere)


# --- failure classification -------------------------------------------------

@pytest.mark.parametrize(
    "exc",
    [
        cohere_errors.TooManyRequestsError(body="rate limited"),
        cohere_errors.InternalServerError(body="upstream broke"),
        cohere_errors.ServiceUnavailableError(body="down"),
        cohere_errors.GatewayTimeoutError(body="slow"),
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectError("connection refused"),
    ],
    ids=["rate_limit", "server_error", "unavailable", "gateway_timeout", "timeout", "connection"],
)
def test_cohere_transient_failures_map_to_service_error(exc):
    with pytest.raises(LLMServiceError):
        invoke_chain(_chain_raising(exc), {}, description="Failed to parse resume")


@pytest.mark.parametrize(
    "exc",
    [
        cohere_errors.UnauthorizedError(body="bad key"),
        cohere_errors.ForbiddenError(body="denied"),
        cohere_errors.BadRequestError(body="context too long"),
        cohere_errors.InvalidTokenError(body="bad token"),
        cohere_errors.UnprocessableEntityError(body="bad params"),
        cohere_errors.NotFoundError(body="no such model"),
    ],
    ids=["auth", "forbidden", "bad_request", "invalid_token", "unprocessable", "not_found"],
)
def test_cohere_fatal_failures_map_to_configuration_error(exc):
    """A bad key or a misspelt model name must not be retried."""
    with pytest.raises(LLMConfigurationError):
        invoke_chain(_chain_raising(exc), {}, description="Failed to parse resume")


def test_unnamed_cohere_status_is_treated_as_transient():
    exc = CohereApiError(status_code=418, body="teapot")

    with pytest.raises(LLMServiceError) as exc_info:
        invoke_chain(_chain_raising(exc), {}, description="Failed to parse resume")

    assert exc_info.value.__cause__ is exc
