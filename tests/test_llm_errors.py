"""
Tests for LLM client hardening: request timeouts and error classification.

The point of `invoke_chain` is that a caller can tell *why* an LLM call failed.
These tests pin that contract: each upstream failure mode must arrive as a
distinct type, so a retry layer can decide whether retrying is even sensible.
"""
import httpx
import openai
import pytest
from langchain_core.exceptions import OutputParserException
from unittest.mock import Mock

from app.exceptions import (
    FirstPlayError,
    LLMConfigurationError,
    LLMError,
    LLMOutputError,
    LLMServiceError,
)
from app.llm_client import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    get_llm,
    invoke_chain,
)

REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=REQUEST)


def _chain_raising(exc: Exception) -> Mock:
    """A stand-in runnable whose invoke() fails with `exc`."""
    chain = Mock()
    chain.invoke.side_effect = exc
    return chain


# --- get_llm: timeouts must be configured, not left unbounded ---------------

def test_get_llm_sets_request_timeout():
    """A hung request must not pin a worker indefinitely."""
    llm = get_llm()
    assert llm.request_timeout == DEFAULT_TIMEOUT_SECONDS


def test_get_llm_sets_max_retries():
    llm = get_llm()
    assert llm.max_retries == DEFAULT_MAX_RETRIES


def test_get_llm_timeout_reaches_underlying_client():
    """`timeout` is an alias for request_timeout; confirm it is not swallowed."""
    llm = get_llm(timeout=5, max_retries=0)
    assert llm.client._client.timeout == 5
    assert llm.client._client.max_retries == 0


# --- invoke_chain: successful passthrough -----------------------------------

def test_invoke_chain_returns_parser_output():
    chain = Mock()
    chain.invoke.return_value = {"parsed": True}

    result = invoke_chain(chain, {"x": 1}, description="Failed to do thing")

    assert result == {"parsed": True}
    chain.invoke.assert_called_once_with({"x": 1})


# --- invoke_chain: classification -------------------------------------------

def test_output_parser_failure_maps_to_output_error():
    """Malformed/unvalidatable model output is the retryable-with-changes case."""
    chain = _chain_raising(OutputParserException("could not parse"))

    with pytest.raises(LLMOutputError):
        invoke_chain(chain, {}, description="Failed to parse resume")


@pytest.mark.parametrize(
    "exc",
    [
        openai.APITimeoutError(request=REQUEST),
        openai.APIConnectionError(request=REQUEST),
        openai.RateLimitError("rate limited", response=_response(429), body=None),
        openai.InternalServerError("upstream broke", response=_response(500), body=None),
    ],
    ids=["timeout", "connection", "rate_limit", "server_error"],
)
def test_transient_upstream_failures_map_to_service_error(exc):
    chain = _chain_raising(exc)

    with pytest.raises(LLMServiceError):
        invoke_chain(chain, {}, description="Failed to parse resume")


@pytest.mark.parametrize(
    "exc",
    [
        openai.AuthenticationError("bad key", response=_response(401), body=None),
        openai.PermissionDeniedError("denied", response=_response(403), body=None),
        openai.BadRequestError("context too long", response=_response(400), body=None),
    ],
    ids=["auth", "permission", "bad_request"],
)
def test_fatal_failures_map_to_configuration_error(exc):
    """These must NOT be retried — retrying a bad API key just burns backoff."""
    chain = _chain_raising(exc)

    with pytest.raises(LLMConfigurationError):
        invoke_chain(chain, {}, description="Failed to parse resume")


def test_configuration_error_is_not_a_service_error():
    """The retry layer branches on these types; they must not overlap."""
    assert not issubclass(LLMConfigurationError, LLMServiceError)
    assert not issubclass(LLMOutputError, LLMServiceError)
    assert issubclass(LLMConfigurationError, LLMError)
    assert issubclass(LLMError, FirstPlayError)


def test_unclassified_failure_still_typed():
    """An unrecognised error is wrapped, not flattened into a bare Exception."""
    chain = _chain_raising(ValueError("Invalid JSON format"))

    with pytest.raises(LLMError) as exc_info:
        invoke_chain(chain, {}, description="Failed to parse resume")

    assert type(exc_info.value) is LLMError


# --- invoke_chain: diagnostics ----------------------------------------------

def test_original_exception_preserved_as_cause():
    """Losing the cause was the reason failures were undebuggable before."""
    original = OutputParserException("could not parse")
    chain = _chain_raising(original)

    with pytest.raises(LLMOutputError) as exc_info:
        invoke_chain(chain, {}, description="Failed to parse resume")

    assert exc_info.value.__cause__ is original


def test_description_prefixes_the_message():
    chain = _chain_raising(OutputParserException("could not parse"))

    with pytest.raises(LLMOutputError) as exc_info:
        invoke_chain(chain, {}, description="Failed to improve resume")

    assert "Failed to improve resume" in str(exc_info.value)
    assert "could not parse" in str(exc_info.value)
