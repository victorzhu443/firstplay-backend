"""
Autofill endpoint: a form plus a profile becomes a FillPlan.

**Deliberately stateless.** The profile arrives in the request and is never
written anywhere — no database, no session, no log of its contents. That is a
design decision rather than an omission, and it buys three things:

  - **No breach surface.** This data includes protected characteristics, work
    authorisation and a home postcode. The safest place to keep it is the only
    machine that needs it.
  - **No authentication to build.** There is nothing server-side belonging to a
    user, so there is nothing to protect. Multi-user works by construction:
    every extension holds its own profile.
  - **Consistency.** The résumé-screening gate was rejected for sending work
    history to a third party in exchange for 0.07 fields per application. It
    would be odd to then store demographics on a server for convenience.

Adding sync later is additive. Un-storing data is not.

The one thing that does leave the machine is what Jev needs: field labels and
option text for theme classification, plus the stored answer for the ~4% of
fields needing semantic option matching. Protected fields never reach that path
— `ALLOWED_FILL_SOURCES` bars model judgement from them entirely.
"""
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.autofill.ashby import parse_ashby_dom
from app.autofill.binder import DeterministicBinder, FillPlan, resolve_form
from app.autofill.greenhouse import parse_greenhouse_job
from app.autofill.jev_binder import JevBinder, OptionCache, ThemeCache
from app.autofill.memory import Memory
from app.autofill.schema import FormSchema
from app.exceptions import JevConfigurationError, JevError
from app.rate_limit import llm_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/autofill", tags=["autofill"])

#: Shared across requests on purpose. The cache holds label -> theme mappings,
#: which are facts about *forms* rather than about any applicant, so there is
#: nothing user-specific in it and every user's first application benefits from
#: every previous one. Measured: 123 distinct phrasings across 42 postings, so
#: the cache saturates quickly and marginal cost trends to zero.
_THEME_CACHE = ThemeCache()
_OPTION_CACHE = OptionCache()


class AutofillRequest(BaseModel):
    """A form to fill and the profile to fill it from."""

    #: "greenhouse" with a raw board-API payload, or "ashby" with a DOM extract.
    ats: str

    #: The ATS payload. For Greenhouse this is the board API's job JSON; for
    #: Ashby it is `{posting_id, company, title, apply_url, controls: [...]}`
    #: as read from the page.
    form: Dict[str, Any]

    #: The applicant's profile. Sent per request, stored nowhere.
    profile: Dict[str, Any] = Field(default_factory=dict)

    #: Company being applied to, for the parameterised themes. Falls back to
    #: whatever the form itself names.
    company: Optional[str] = None

    #: False disables every model call, leaving only deterministic resolution.
    #: Useful for a caller who wants nothing to leave the machine at all.
    use_model: bool = True


class AutofillResponse(BaseModel,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        model_skipped=bool(jev is not None and getattr(jev, "tripped", False)),
    ):
    plan: FillPlan
    summary: Dict[str, int]
    jev_calls: int = 0
    cost_usd: float = 0.0


def _parse_form(ats: str, payload: Dict[str, Any]) -> FormSchema:
    if ats == "greenhouse":
        return parse_greenhouse_job(payload)

    if ats == "ashby":
        return parse_ashby_dom(payload)

    raise HTTPException(
        status_code=400,
        detail="Unsupported ats {!r}; expected 'greenhouse' or 'ashby'".format(ats),
    )


# Sync by design, like the other handlers here: this makes a blocking HTTP call
# to the decisions API, and FastAPI runs sync handlers in a threadpool. Declared
# async, one slow call would stall the event loop for every other request.
@router.post("/plan", response_model=AutofillResponse,
             dependencies=[Depends(llm_limit)])
def build_plan(request: AutofillRequest):
    """Resolve a form against a profile.

    Never submits anything, and never returns a value for a field the policy
    forbids filling. Every field comes back in exactly one state: filled,
    satisfied by a sibling control, skipped by a standing decision, or flagged
    for the applicant.

    Args:
        request: The form, the profile, and whether model calls are permitted

    Returns:
        The plan, a summary, and what the model calls cost
    """
    started = time.perf_counter()
    form = _parse_form(request.ats, request.form)

    try:
        memory = Memory(**request.profile)
    except Exception as e:
        raise HTTPException(
            status_code=422, detail="Profile could not be read: {}".format(e)
        )

    binder = DeterministicBinder()
    jev: Optional[JevBinder] = None

    if request.use_model:
        try:
            jev = JevBinder(cache=_THEME_CACHE, option_cache=_OPTION_CACHE,
                            form_context={"company": form.company or "", 
                                          "title": form.title or ""})
            binder = jev
        except JevConfigurationError as e:
            # No key, or a malformed one. Deterministic resolution still answers
            # the large majority of fields, so this degrades rather than fails.
            logger.warning("Jev unavailable, falling back to deterministic: %s", e)

    try:
        plan = resolve_form(form, memory, company=request.company, binder=binder)
    except JevError as e:
        # The decision service was reachable but failed. Rather than returning
        # nothing, retry without it: a deterministic plan is worth far more than
        # an error page.
        logger.warning("Jev failed mid-plan, retrying deterministically: %s", e)
        plan = resolve_form(form, memory, company=request.company,
                            binder=DeterministicBinder())
        jev = None

    return AutofillResponse(
        plan=plan,
        summary=plan.summary(),
        jev_calls=jev.calls if jev else 0,
        cost_usd=jev.cost_usd if jev else 0.0,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        model_skipped=bool(jev is not None and getattr(jev, "tripped", False)),
    )


@router.get("/health")
def autofill_health():
    """Whether the model path is available, without exercising it."""
    from app.autofill.themes import QuestionTheme

    configured = True
    try:
        JevBinder()._ensure_client()
    except JevConfigurationError:
        configured = False

    return {
        "status": "ok",
        "model_available": configured,
        "themes": len(list(QuestionTheme)),
        "theme_cache": _THEME_CACHE.size,
        "option_cache": _OPTION_CACHE.size,
    }
