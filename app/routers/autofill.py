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
from app.autofill.ashby_api import looks_like_posting, parse_ashby_posting
from app.autofill import ENGINE
from app.autofill.binder import DeterministicBinder, FillPlan, resolve_form
from app.autofill.greenhouse import parse_greenhouse_job
from app.autofill.jev_binder import JevBinder, OptionCache, ThemeCache
from app.autofill.learn import LearnResult, Observation, accept, learn
from app.autofill.memory import Memory
from app.autofill.schema import FormSchema
from app.exceptions import JevConfigurationError, JevError
from app.rate_limit import autofill_limit

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


class AutofillResponse(BaseModel):
    plan: FillPlan
    #: Wall-clock time the backend spent, so a slow fill is blamed correctly.
    elapsed_ms: int = 0
    #: True when a model call timed out and the rest of the plan was finished
    #: deterministically — the fill is still complete, just with more review.
    model_skipped: bool = False
    summary: Dict[str, int]
    jev_calls: int = 0
    cost_usd: float = 0.0
    #: Fingerprint of the engine that built this plan (see app.autofill.ENGINE).
    engine: str = ""
    #: True when the model path was configured but failed on this plan or on
    #: the last one with no success since (an expired key). A caller that
    #: caches plans must not cache this one: it is the deterministic fallback,
    #: not the plan the engine would make. 2026-10-12: the extension kept
    #: serving plans made during the outage for a day after the key was fixed.
    model_degraded: bool = False


def _parse_form(ats: str, payload: Dict[str, Any]) -> FormSchema:
    if ats == "greenhouse":
        return parse_greenhouse_job(payload)

    if ats == "ashby":
        # The GraphQL `jobPosting` (with its applicationForm) when the
        # extension fetched it; the DOM extract otherwise (older clients,
        # or a page whose operation failed).
        if looks_like_posting(payload):
            return parse_ashby_posting(payload, org=payload.get("_org"))
        return parse_ashby_dom(payload)

    raise HTTPException(
        status_code=400,
        detail="Unsupported ats {!r}; expected 'greenhouse' or 'ashby'".format(ats),
    )


# Sync by design, like the other handlers here: this makes a blocking HTTP call
# to the decisions API, and FastAPI runs sync handlers in a threadpool. Declared
# async, one slow call would stall the event loop for every other request.
@router.post("/plan", response_model=AutofillResponse,
             dependencies=[Depends(autofill_limit)])
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
    # Company-shaped episodes (a referral, an office choice) replay only on
    # this company's forms; `resolve_form` has no way to hand it to Memory.
    memory.for_company(request.company or form.company)

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
            _note_model_error(e)

    try:
        plan = resolve_form(form, memory, company=request.company, binder=binder)
        if binder is not None and getattr(binder, "calls", 0):
            _note_model_ok()
    except JevError as e:
        # The decision service was reachable but failed. Rather than returning
        # nothing, retry without it: a deterministic plan is worth far more than
        # an error page.
        logger.warning("Jev failed mid-plan, retrying deterministically: %s", e)
        _note_model_error(e)
        plan = resolve_form(form, memory, company=request.company,
                            binder=DeterministicBinder())
        jev = None

    return AutofillResponse(
        model_degraded=bool(_MODEL_STATUS["last_error_at"] and (not _MODEL_STATUS["last_ok_at"] or _MODEL_STATUS["last_ok_at"] < _MODEL_STATUS["last_error_at"])),
        plan=plan,
        summary=plan.summary(),
        jev_calls=jev.calls if jev else 0,
        cost_usd=jev.cost_usd if jev else 0.0,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        model_skipped=bool(jev is not None and getattr(jev, "tripped", False)),
        engine=ENGINE,
    )


class LearnRequest(BaseModel):
    """What the applicant did on a form, and the profile to fold it into."""

    profile: Dict[str, Any] = Field(default_factory=dict)
    observations: List[Observation] = Field(default_factory=list)
    #: Keys of pending proposals the applicant accepted in the popup.
    accept: List[str] = Field(default_factory=list)
    use_model: bool = True


class LearnResponse(BaseModel):
    profile: Dict[str, Any]
    learned: int = 0
    accepted: List[str] = Field(default_factory=list)
    proposals: List[Dict[str, Any]] = Field(default_factory=list)
    ignored: List[Dict[str, str]] = Field(default_factory=list)
    jev_calls: int = 0
    cost_usd: float = 0.0
    engine: str = ""


@router.post("/learn", response_model=LearnResponse,
             dependencies=[Depends(autofill_limit)])
def learn_from_form(request: LearnRequest):
    """Fold the applicant's edits on a form into the profile (DECISIONS §50).

    Stateless like /plan: the updated profile comes back for the extension
    to store. Exact answers are saved for replay; generalisations are only
    proposed, and applied here only for keys listed in `accept`.
    """
    try:
        memory = Memory(**request.profile)
    except Exception as e:
        raise HTTPException(status_code=422, detail="Profile could not be read: {}".format(e))

    accepted = [key for key in request.accept if accept(memory, key)]

    binder: Optional[JevBinder] = None
    if request.use_model and request.observations:
        try:
            binder = JevBinder(cache=_THEME_CACHE, option_cache=_OPTION_CACHE)
        except JevConfigurationError as e:
            logger.warning("Jev unavailable; learning episodes only: %s", e)

    try:
        result: LearnResult = learn(memory, request.observations, binder=binder)
    except JevError as e:
        logger.warning("Jev failed while judging corrections; episodes only: %s", e)
        result = learn(memory, request.observations, binder=None)

    return LearnResponse(
        profile=result.profile, learned=result.learned, accepted=accepted,
        proposals=[p.model_dump() for p in result.proposals], ignored=result.ignored,
        jev_calls=result.jev_calls, cost_usd=result.cost_usd, engine=ENGINE,
    )


#: What the model path last did, so /health tells the truth about it.
#: `model_available` only says a key is configured; on 2026-10-11 the key had
#: expired, every plan fell back to deterministic, and nothing said so until
#: an audit run came back with two model decisions across five hundred forms.
_MODEL_STATUS: Dict[str, Any] = {"last_error": None, "last_error_at": None, "last_ok_at": None}


def _note_model_error(error: Exception) -> None:
    import datetime
    _MODEL_STATUS["last_error"] = "{}: {}".format(type(error).__name__, str(error)[:200])
    _MODEL_STATUS["last_error_at"] = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _note_model_ok() -> None:
    import datetime
    _MODEL_STATUS["last_ok_at"] = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


@router.get("/profile-schema")
def autofill_profile_schema():
    """The onboarding registry: what the profile can hold, with examples and
    notes, ordered by how many real application fields each key answers."""
    from app.autofill.memory import profile_schema
    rows = profile_schema()
    return {"sections": sorted({r["section"] for r in rows}, key=["facts", "education", "legal_status", "preferences", "consents", "protected"].index),
            "fields": sorted(rows, key=lambda r: -int(r["answers"] or 0))}


@router.get("/health")
def autofill_health():
    """Whether the model path is available, and what it last did."""
    from app.autofill.themes import QuestionTheme

    configured = True
    try:
        JevBinder()._ensure_client()
    except JevConfigurationError:
        configured = False

    # Degraded = configured but the last thing it did was fail, with no
    # success since: the state an expired key leaves you in.
    degraded = bool(configured and _MODEL_STATUS["last_error_at"] and
                    (not _MODEL_STATUS["last_ok_at"] or _MODEL_STATUS["last_ok_at"] < _MODEL_STATUS["last_error_at"]))

    return {
        "status": "ok",
        "engine": ENGINE,
        "model_available": configured,
        "model_degraded": degraded,
        "model_last_error": _MODEL_STATUS["last_error"],
        "model_last_error_at": _MODEL_STATUS["last_error_at"],
        "model_last_ok_at": _MODEL_STATUS["last_ok_at"],
        "themes": len(list(QuestionTheme)),
        "theme_cache": _THEME_CACHE.size,
        "option_cache": _OPTION_CACHE.size,
    }
