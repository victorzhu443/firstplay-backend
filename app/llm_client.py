"""
LangChain model wrapper for LLM interactions.
Uses LangChain ChatOpenAI instead of direct OpenAI SDK calls.
"""
import logging
import os
from typing import Any, Callable, Dict

import openai
from dotenv import load_dotenv
from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI

from app.exceptions import (
    LLMConfigurationError,
    LLMError,
    LLMOutputError,
    LLMServiceError,
)

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)

# No single LLM call may hang a worker indefinitely. A pipeline run is five
# sequential nodes, so bounding each call also bounds the whole run.
DEFAULT_TIMEOUT_SECONDS = 30

# Transport-level retries, performed by the OpenAI SDK itself, for connection
# errors, 429s and upstream 5xx. This is a different concern from retrying a
# call that *succeeded* but returned unusable content — see LLMOutputError.
DEFAULT_MAX_RETRIES = 2

# Attempts for a call whose output fails validation, including the first.
DEFAULT_MAX_OUTPUT_ATTEMPTS = 3

# How much to raise temperature per retry. Re-sending an identical prompt at
# temperature 0.0 is near-greedy decoding: the model tends to reproduce the
# same unusable completion, so a naive retry buys three identical failures and
# the backoff between them. Escalating forces the model off that trajectory.
TEMPERATURE_ESCALATION_STEP = 0.2

# Temperature is only meaningful up to 1.0 for chat completions.
MAX_TEMPERATURE = 1.0


def get_llm(
    model: str = "gpt-4o-mini",
    temperature: float = 0.0,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
):
    """
    Returns a LangChain ChatModel instance.

    Args:
        model: The OpenAI model to use (default: gpt-4o-mini)
        temperature: Creativity level 0.0-1.0 (default: 0.0 for consistency)
        timeout: Per-request timeout in seconds
        max_retries: Transport-level retries performed by the OpenAI SDK

    Returns:
        ChatOpenAI: A LangChain chat model instance

    Raises:
        ValueError: If OPENAI_API_KEY is not set
    """
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY environment variable is not set. "
            "Please add it to your .env file."
        )

    # `timeout` is an alias for ChatOpenAI's `request_timeout` field; it is
    # forwarded to the underlying httpx client.
    return ChatOpenAI(
        model=model,
        temperature=temperature,
        api_key=api_key,
        timeout=timeout,
        max_retries=max_retries,
    )


def invoke_chain(chain: Runnable, payload: Dict[str, Any], *, description: str) -> Any:
    """
    Invoke a LangChain runnable, translating failures into typed exceptions.

    Every chain routes its call through here so that error classification
    lives in one place rather than being re-implemented per chain.

    Args:
        chain: The runnable to invoke
        payload: Input variables for the chain
        description: Human-readable prefix for the error message,
            e.g. "Failed to parse resume"

    Returns:
        Whatever the chain's output parser produced

    Raises:
        LLMOutputError: Model replied, but the output failed parsing/validation
        LLMServiceError: Model was unreachable or failed transiently
        LLMConfigurationError: Request was rejected in a way retrying won't fix
        LLMError: The failure could not be classified
    """
    try:
        return chain.invoke(payload)

    except OutputParserException as e:
        logger.warning("%s: model output failed validation: %s", description, e)
        raise LLMOutputError(f"{description}: {e}") from e

    except (
        openai.APITimeoutError,
        openai.APIConnectionError,
        openai.RateLimitError,
        openai.InternalServerError,
    ) as e:
        logger.warning("%s: transient upstream failure: %s", description, e)
        raise LLMServiceError(f"{description}: {e}") from e

    except (
        openai.AuthenticationError,
        openai.PermissionDeniedError,
        openai.BadRequestError,
    ) as e:
        logger.error("%s: request rejected, not retryable: %s", description, e)
        raise LLMConfigurationError(f"{description}: {e}") from e

    except Exception as e:
        # Deliberately last, and deliberately still typed: an unrecognised
        # failure is preserved with its cause rather than flattened into a
        # bare Exception the way it used to be.
        logger.exception("%s: unclassified failure", description)
        raise LLMError(f"{description}: {e}") from e


def invoke_with_retry(
    chain_factory: Callable[..., Runnable],
    payload: Dict[str, Any],
    *,
    description: str,
    base_temperature: float = 0.0,
    max_attempts: int = DEFAULT_MAX_OUTPUT_ATTEMPTS,
) -> Any:
    """
    Invoke a chain, retrying only when the model's *output* was unusable.

    Takes a factory rather than a built chain because each retry raises the
    temperature, which means rebuilding the model. Retrying the identical
    prompt at temperature 0.0 mostly reproduces the identical bad output, so
    an escalating retry is the difference between a fix and wasted latency.

    Only LLMOutputError is retried:
      - LLMServiceError is already retried by the OpenAI SDK at the transport
        layer; retrying again here would compound the two.
      - LLMConfigurationError is fatal by definition. Retrying a bad API key
        just burns the backoff before failing anyway.

    Args:
        chain_factory: Callable taking a temperature and returning a runnable
        payload: Input variables for the chain
        description: Human-readable prefix for the error message
        base_temperature: The chain's normal temperature; retries escalate
            from here, so a deliberately creative chain stays creative
        max_attempts: Total attempts, including the first

    Returns:
        Whatever the chain's output parser produced

    Raises:
        LLMOutputError: Output failed validation on every attempt
        LLMError: Any non-retryable failure, raised on the first attempt
    """
    last_error = None

    for attempt in range(1, max_attempts + 1):
        temperature = min(
            base_temperature + (attempt - 1) * TEMPERATURE_ESCALATION_STEP,
            MAX_TEMPERATURE,
        )

        try:
            result = invoke_chain(
                chain_factory(temperature), payload, description=description
            )
        except LLMOutputError as e:
            last_error = e
            logger.warning(
                "%s: attempt %d/%d produced invalid output at temperature %.2f",
                description,
                attempt,
                max_attempts,
                temperature,
            )
            continue

        if attempt > 1:
            logger.info(
                "%s: attempt %d succeeded at temperature %.2f",
                description,
                attempt,
                temperature,
            )
        return result

    logger.error(
        "%s: all %d attempts produced invalid output", description, max_attempts
    )
    raise last_error
