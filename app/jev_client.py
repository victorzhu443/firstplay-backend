"""
The Jev boundary: typed decisions from TypeSafe's System One model.

One module, so that provider choice and error classification live in one place
— the same reason `app/llm_client.py` exists for the generative models.

**Why raw HTTP rather than the official SDK.** `typesafe-sdk` requires Python
3.10. `pyproject.toml` pins `target-version = "py39"` deliberately, so that
ruff does not rewrite `Optional[X]` into `X | None`, which Pydantic evaluates
at runtime and which raises a TypeError on 3.9. `httpx` is already a
dependency, and going over it keeps the floor intact and the boundary trivially
mockable.

**Request and response shape**, verified against the published reference rather
than assumed:

    POST https://openrouter.ai/api/alpha/decisions
    {"model": "typesafe/jev-1.13",
     "state": <str | dict | list>,
     "questions": {"<id>": {"type": "noul|choice|score",
                            "instructions": "...", "criteria": ...}}}

    -> {"answers": {"<id>": {"type": ..., "choice": "...",
                             "probabilities": {...}, "confidence": 0.67}},
        "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 1.9e-05}}

Three details that are easy to get wrong and are enforced below:

  - **A Noul carries no `confidence` field.** Its probability *is* the belief.
    Code that reads `.confidence` uniformly across the three primitives breaks
    on every Noul.
  - **A Score returns a probability-weighted float**, not a level index — 1.99
    rather than 2 — plus a `legend` mapping indices to the level descriptions.
  - **Criteria are required** for Choice (a map, max 255 entries) and Score (an
    array, 2-10 levels), and optional for Noul, where they take the form
    `{"true": ..., "false": ...}`.

**Batching is the whole cost argument.** State is sent once per call, so a
tenth question costs tokens but almost no time. Callers should ask everything
about one form in a single call, never one call per field.
"""
import logging
import os
from typing import Any, Dict, List, Optional, Union

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from app.exceptions import JevConfigurationError, JevError, JevServiceError

load_dotenv()

logger = logging.getLogger(__name__)

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"

#: A single decision call is on the critical path of a page interaction, and
#: published latency is 70-500ms. Ten seconds is generous and still bounded.
#: Per-call budget. A fill that waits on a slow model is slower than typing
#: by hand, which defeats the tool; measured 2026-09-28, every call was
#: hitting the old 10 s limit and each plan paid it up to three times. Four
#: seconds covers a healthy call (~1-3 s) and bounds the damage of a sick one.
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("JEV_TIMEOUT_SECONDS", "4.0"))

#: API limits, enforced before sending so a malformed question set fails with a
#: message naming the question rather than as an opaque 422.
MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10

#: Transient upstream statuses. 529 is OpenRouter's "overloaded" and is not a
#: standard code, so it has to be named explicitly.
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504, 529})


# --- question builders -------------------------------------------------------

def noul(instructions: str, *, true_means: Optional[str] = None,
         false_means: Optional[str] = None) -> Dict[str, Any]:
    """A yes/no question. Returns a probability, not a confidence.

    `true_means` / `false_means` are optional but worth supplying: the guidance
    is to describe an observable situation rather than a degree, and an
    undefined proposition comes back near 0.5.
    """
    question: Dict[str, Any] = {"type": "noul", "instructions": instructions}

    if true_means or false_means:
        question["criteria"] = {"true": true_means or "", "false": false_means or ""}

    return question


def choice(instructions: str, criteria: Dict[str, str]) -> Dict[str, Any]:
    """Pick one of a named set. `criteria` maps option name -> situation."""
    if not criteria:
        raise JevConfigurationError("A choice question needs at least one option")

    if len(criteria) > MAX_CHOICE_OPTIONS:
        raise JevConfigurationError(
            "A choice question accepts at most {} options, got {}".format(
                MAX_CHOICE_OPTIONS, len(criteria)
            )
        )

    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def score(instructions: str, levels: List[str]) -> Dict[str, Any]:
    """Position on an ordered scale. `levels` describe situations, in order."""
    if not MIN_SCORE_LEVELS <= len(levels) <= MAX_SCORE_LEVELS:
        raise JevConfigurationError(
            "A score question needs {}-{} levels, got {}".format(
                MIN_SCORE_LEVELS, MAX_SCORE_LEVELS, len(levels)
            )
        )

    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


# --- answers -----------------------------------------------------------------

class Answer(BaseModel):
    """One typed answer.

    `confidence` is Optional rather than defaulted, because a Noul genuinely has
    none — defaulting it to 1.0 or 0.0 would silently feed a made-up number into
    a threshold comparison.
    """

    type: str
    noul: Optional[float] = None
    choice: Optional[str] = None
    score: Optional[float] = None
    probabilities: Dict[str, float] = Field(default_factory=dict)
    confidence: Optional[float] = None
    legend: Dict[str, str] = Field(default_factory=dict)

    def belief(self) -> float:
        """A single number to compare against a threshold, per primitive.

        For a Noul that is the probability itself; for a Choice or Score it is
        the calibrated confidence. Having one accessor keeps callers from
        reaching for `.confidence` on a Noul and getting None.
        """
        if self.type == "noul":
            return self.noul if self.noul is not None else 0.0

        return self.confidence if self.confidence is not None else 0.0


class Decision(BaseModel):
    """One call's worth of answers, plus what it cost."""

    model: Optional[str] = None
    answers: Dict[str, Answer] = Field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def __getitem__(self, key: str) -> Answer:
        return self.answers[key]

    def get(self, key: str) -> Optional[Answer]:
        return self.answers.get(key)


# --- the client --------------------------------------------------------------

class JevClient:
    """Calls the decisions endpoint and translates failures into typed errors."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        url: str = DECISIONS_URL,
    ):
        key = api_key or os.getenv("OPENROUTER_API_KEY") or ""

        # Validated here rather than on first request. A malformed key that
        # reaches the wire comes back as a bare 401 halfway through a run; this
        # names the actual problem before anything is sent. `llm_client._require_env`
        # takes the same approach for OPENAI_API_KEY.
        if not key:
            raise JevConfigurationError(
                "OPENROUTER_API_KEY is not set. Add it to your .env file."
            )

        if not key.startswith("sk-or-"):
            raise JevConfigurationError(
                "OPENROUTER_API_KEY does not look like an OpenRouter key "
                "(expected it to start with 'sk-or-'). Got {} characters "
                "starting {!r}.".format(len(key), key[:8])
            )

        self._key = key
        self._model = model
        self._timeout = timeout
        self._url = url

    def decide(
        self,
        state: Union[str, Dict[str, Any], List[Any]],
        questions: Dict[str, Dict[str, Any]],
        *,
        description: str = "decision",
    ) -> Decision:
        """Ask one or more typed questions about one state.

        Args:
            state: What the questions are about. A dict keeps relationships
                explicit and is preferred over a flattened string.
            questions: Question id -> a builder's output. Ask everything about
                one form here: state is sent once, so extra questions are close
                to free.
            description: Prefix for log lines and error messages.

        Returns:
            A Decision.

        Raises:
            JevConfigurationError: Rejected in a way retrying cannot fix
            JevServiceError: Unreachable or transiently failing
            JevError: Unclassifiable failure
        """
        if not questions:
            raise JevConfigurationError("No questions supplied")

        payload = {"model": self._model, "state": state, "questions": questions}

        try:
            response = httpx.post(
                self._url,
                headers={
                    "Authorization": "Bearer " + self._key,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout,
            )
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            logger.warning("%s: transport failure: %s", description, e)
            raise JevServiceError("{}: {}".format(description, e)) from e
        except Exception as e:
            logger.exception("%s: unclassified transport failure", description)
            raise JevError("{}: {}".format(description, e)) from e

        return self._parse(response, description=description)

    def _parse(self, response: httpx.Response, *, description: str) -> Decision:
        status = response.status_code

        if status in (401, 403):
            raise JevConfigurationError(
                "{}: API key rejected (HTTP {})".format(description, status)
            )

        if status == 422:
            raise JevConfigurationError(
                "{}: the question set was rejected (HTTP 422): {}".format(
                    description, response.text[:300]
                )
            )

        if status in _TRANSIENT_STATUSES:
            raise JevServiceError(
                "{}: upstream unavailable (HTTP {})".format(description, status)
            )

        if status >= 400:
            raise JevError(
                "{}: unexpected HTTP {}: {}".format(description, status, response.text[:200])
            )

        try:
            body = response.json()
        except ValueError as e:
            raise JevError("{}: response was not JSON: {}".format(description, e)) from e

        answers = {}
        for key, raw in (body.get("answers") or {}).items():
            answers[key] = Answer(**raw)

        usage = body.get("usage") or {}

        return Decision(
            model=body.get("model"),
            answers=answers,
            input_tokens=usage.get("input_tokens") or 0,
            output_tokens=usage.get("output_tokens") or 0,
            cost_usd=usage.get("cost") or 0.0,
        )


def get_jev_client(**kwargs) -> JevClient:
    """Build a client from the environment. Mirrors `llm_client.get_llm`."""
    return JevClient(**kwargs)
