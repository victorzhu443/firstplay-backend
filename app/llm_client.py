"""
LangChain model wrapper for LLM interactions.
Uses LangChain ChatOpenAI instead of direct OpenAI SDK calls.
"""
import logging
import os
from typing import Any, Dict

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
