"""
Tests for output-validation retry.

The design point these pin down: a retry is only worth making if it changes
the input. Re-sending an identical prompt at temperature 0.0 is near-greedy
decoding and tends to reproduce the identical unusable completion, so the
retry escalates temperature each attempt. And only LLMOutputError is retried
— retrying a rejected API key just burns latency before failing anyway.
"""
from unittest.mock import Mock

import httpx
import openai
import pytest
from langchain_core.exceptions import OutputParserException

from app.exceptions import LLMConfigurationError, LLMError, LLMOutputError, LLMServiceError
from app.llm_client import (
    DEFAULT_MAX_OUTPUT_ATTEMPTS,
    TEMPERATURE_ESCALATION_STEP,
    invoke_with_retry,
)

REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


class RecordingFactory:
    """Chain factory that records the temperature of each attempt."""

    def __init__(self, side_effects):
        self.side_effects = list(side_effects)
        self.temperatures = []

    def __call__(self, temperature):
        self.temperatures.append(temperature)
        outcome = self.side_effects.pop(0)
        chain = Mock()
        if isinstance(outcome, Exception):
            chain.invoke.side_effect = outcome
        else:
            chain.invoke.return_value = outcome
        return chain


def _bad_output():
    return OutputParserException("could not parse")


# --- retry succeeds ---------------------------------------------------------

def test_retries_until_output_validates():
    factory = RecordingFactory([_bad_output(), _bad_output(), {"ok": True}])

    result = invoke_with_retry(
        factory, {}, description="Failed to parse resume", base_temperature=0.0
    )

    assert result == {"ok": True}
    assert len(factory.temperatures) == 3


def test_temperature_escalates_on_each_retry():
    """Identical prompt at temperature 0.0 reproduces the identical failure."""
    factory = RecordingFactory([_bad_output(), _bad_output(), {"ok": True}])

    invoke_with_retry(
        factory, {}, description="Failed to parse resume", base_temperature=0.0
    )

    assert factory.temperatures == [
        0.0,
        TEMPERATURE_ESCALATION_STEP,
        2 * TEMPERATURE_ESCALATION_STEP,
    ]
    # Strictly increasing, so no attempt repeats the previous sampling.
    assert factory.temperatures == sorted(set(factory.temperatures))


def test_escalation_starts_from_the_chain_s_own_temperature():
    """A deliberately creative chain must not be reset to 0.0 on retry."""
    factory = RecordingFactory([_bad_output(), {"ok": True}])

    invoke_with_retry(
        factory, {}, description="Failed to generate projects", base_temperature=0.7
    )

    assert factory.temperatures[0] == 0.7
    assert factory.temperatures[1] == pytest.approx(0.7 + TEMPERATURE_ESCALATION_STEP)


def test_temperature_is_capped_at_one():
    factory = RecordingFactory([_bad_output()] * 4 + [{"ok": True}])

    invoke_with_retry(
        factory,
        {},
        description="Failed to generate projects",
        base_temperature=0.9,
        max_attempts=5,
    )

    assert max(factory.temperatures) <= 1.0


def test_no_retry_when_the_first_attempt_succeeds():
    factory = RecordingFactory([{"ok": True}])

    result = invoke_with_retry(
        factory, {}, description="Failed to parse resume", base_temperature=0.0
    )

    assert result == {"ok": True}
    assert factory.temperatures == [0.0]


# --- retry gives up ---------------------------------------------------------

def test_raises_after_exhausting_attempts():
    factory = RecordingFactory([_bad_output()] * DEFAULT_MAX_OUTPUT_ATTEMPTS)

    with pytest.raises(LLMOutputError):
        invoke_with_retry(
            factory, {}, description="Failed to parse resume", base_temperature=0.0
        )

    assert len(factory.temperatures) == DEFAULT_MAX_OUTPUT_ATTEMPTS


# --- what must NOT be retried ----------------------------------------------

def test_configuration_error_is_not_retried():
    """Retrying a rejected API key burns backoff and fails anyway."""
    factory = RecordingFactory(
        [openai.AuthenticationError("bad key", response=httpx.Response(401, request=REQUEST), body=None)]
    )

    with pytest.raises(LLMConfigurationError):
        invoke_with_retry(
            factory, {}, description="Failed to parse resume", base_temperature=0.0
        )

    assert len(factory.temperatures) == 1, "a fatal error must fail on attempt 1"


def test_service_error_is_not_retried_here():
    """The OpenAI SDK already retries transport failures; don't compound it."""
    factory = RecordingFactory(
        [openai.RateLimitError("rate limited", response=httpx.Response(429, request=REQUEST), body=None)]
    )

    with pytest.raises(LLMServiceError):
        invoke_with_retry(
            factory, {}, description="Failed to parse resume", base_temperature=0.0
        )

    assert len(factory.temperatures) == 1


def test_unclassified_error_is_not_retried():
    factory = RecordingFactory([ValueError("something else")])

    with pytest.raises(LLMError):
        invoke_with_retry(
            factory, {}, description="Failed to parse resume", base_temperature=0.0
        )

    assert len(factory.temperatures) == 1
