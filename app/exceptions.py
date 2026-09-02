"""
Application exception hierarchy.

The chains previously raised a bare `Exception`, which destroyed both the
traceback and the exception type: an invalid API key, a rate limit, and
malformed model output all reached the router as an indistinguishable 500.
Callers cannot decide what to do about a failure they cannot identify, and a
retry layer in particular needs to tell *retryable* failures apart from
*fatal* ones. Every failure crossing the LLM boundary is therefore translated
into one of the types below, with the original exception preserved as
`__cause__`.
"""


class FirstPlayError(Exception):
    """Base class for all application errors."""


class PipelineNodeError(FirstPlayError):
    """A pipeline node could not complete for a non-LLM reason.

    Missing rows, or a prerequisite that was not satisfied. Raised inside a
    node body and recorded by the node wrapper, which halts the graph rather
    than letting downstream nodes run on absent data.
    """


class LLMError(FirstPlayError):
    """Base class for any failure originating from an LLM call.

    Raised directly only when a failure cannot be classified more precisely.
    """


class LLMOutputError(LLMError):
    """The model responded, but its output could not be parsed or validated.

    Retryable — but only if the retry changes the input. Re-sending an
    identical prompt at temperature 0.0 tends to reproduce the same unusable
    output, so a retry layer must feed the validation error back into the
    prompt (or resample at a higher temperature) rather than repeat the call.
    """


class LLMServiceError(LLMError):
    """The model could not be reached, or failed transiently.

    Covers timeouts, connection errors, rate limits and upstream 5xx
    responses. Retryable as-is; the OpenAI SDK already retries these a couple
    of times before the error surfaces here.
    """


class LLMConfigurationError(LLMError):
    """The request was rejected for a reason that retrying cannot fix.

    Missing or invalid credentials, insufficient permissions, or a malformed
    request such as a context-length overflow. Not retryable — surfacing this
    distinctly is what stops the pipeline from burning three attempts and
    twenty seconds of backoff on a typo in an environment variable.
    """
