"""
Autofill endpoint: a form plus a profile becomes a FillPlan.

**Stateless, not blind.** What actually crosses the wire, stated plainly
(audit 2026-10-12, M5):

  - **To this backend, on every `/plan` and `/learn` request:** the whole
    profile as the extension holds it — facts, education, legal status,
    preferences, `protected`, `consents`, `answers`, `learned`, employers —
    together with the form payload. It is parsed into a `Memory` for that one
    request, used to build the plan, and discarded when the response is
    written. Nothing is stored: no database, no session, no file. Nothing is
    logged from the body: the app logs exception text and a short description
    of each model call, never profile contents. Two process-wide caches
    outlive the request and hold no profile values: label -> theme, and
    hashed question signatures -> chosen option label.
  - **From this backend to OpenRouter, when a model call is made:** field
    labels and option texts, the posting's company and title, earlier answers
    on the same form, and the *non-protected* applicant state (legal status,
    education, preferences, background facts, learned facts, current
    location, employers). `protected` and `consents` are never sent;
    `ALLOWED_FILL_SOURCES` bars model judgement from those fields entirely.
    `use_model: false` makes no model call at all.
  - **To Greenhouse's board API and Ashby's GraphQL:** posting identifiers.

Statelessness is a design decision rather than an omission: there is no
server-side record of a user to breach or to authenticate, every extension
holds its own profile, and adding sync later is additive where un-storing
data is not. But the backend sees the profile in transit; it does not keep it.
"""
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
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
from app.jev_client import get_jev_client, summarise_error
from app.rate_limit import autofill_limit, client_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/autofill", tags=["autofill"])

#: Shared across requests on purpose. The theme cache holds label -> theme
#: mappings, which are facts about *forms* rather than about any applicant, so
#: there is nothing user-specific in it and every user's first application
#: benefits from every previous one. Measured: 123 distinct phrasings across
#: 42 postings, so the cache saturates quickly and marginal cost trends to
#: zero. The option cache is keyed on hashes (label, option set and stored
#: answer go through SHA-256) and bounded at 5,000 entries, so a hosted
#: process holds no applicant's stored answer in a key and cannot grow
#: without limit.
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

    #: The applicant's whole profile, protected answers included. Held in
    #: memory for this request only; stored nowhere, logged nowhere.
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
    #: False when no OpenRouter key is configured, so the plan was built by
    #: the deterministic binder for lack of a model rather than by choice.
    #: Distinct from `model_skipped` (a call tripped mid-plan) and from
    #: `model_degraded` (a key is present but failing): the hosted backend
    #: ran keyless for a day with both of those false and the extension
    #: cached its plans for six hours (audit 2026-10-12, M6). A caller that
    #: caches plans must not cache one with this false.
    model_available: bool = True
    #: True when a model call was attempted on this plan and raised — in a
    #: sequential gate (the plan was then finished deterministically) or in
    #: the speculative pool, which swallows what it gets back. Before this,
    #: a plan after a failed model path reported `jev_calls=0, cost_usd=0,
    #: model_skipped=False`, indistinguishable from one that never tried
    #: (audit 2026-10-12, F21).
    model_failed: bool = False
    #: The sanitised category of that failure ("422 from upstream",
    #: "upstream timed out"); never the upstream text.
    model_error: Optional[str] = None


def _profile_error_detail(error: Exception) -> str:
    """A 422 detail that names the field paths, never the values.

    pydantic's own message carries `input_value=<the profile value>`, which
    the extension prints in the page console (audit 2026-10-12, F22).
    """
    errors = getattr(error, "errors", None)
    try:
        rows = errors() if callable(errors) else []
    except Exception:  # noqa: BLE001
        rows = []
    if not rows:
        return "Profile could not be read: {}".format(type(error).__name__)
    paths = sorted({".".join(str(part) for part in (row.get("loc") or ())) or "(root)" for row in rows})
    shown = ", ".join(paths[:10]) + (" and {} more".format(len(paths) - 10) if len(paths) > 10 else "")
    return "Profile could not be read: {} validation error(s) at {}".format(len(rows), shown)


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
def build_plan(request: AutofillRequest, http_request: Request):
    """Resolve a form against a profile.

    Never submits anything, and never returns a value for a field the policy
    forbids filling. Every field comes back in exactly one state: filled,
    satisfied by a sibling control, skipped by a standing decision, or flagged
    for the applicant.

    Args:
        request: The form, the profile, and whether model calls are permitted
        http_request: The HTTP request, for the caller's install token

    Returns:
        The plan, a summary, and what the model calls cost
    """
    started = time.perf_counter()
    caller = client_key(http_request)
    form = _parse_form(request.ats, request.form)

    try:
        memory = Memory(**request.profile)
    except Exception as e:
        raise HTTPException(status_code=422, detail=_profile_error_detail(e))
    # Company-shaped episodes (a referral, an office choice) replay only on
    # this company's forms; `resolve_form` has no way to hand it to Memory.
    memory.for_company(request.company or form.company)

    binder = DeterministicBinder()
    jev: Optional[JevBinder] = None

    # Checked up front rather than discovered mid-plan: the binder builds its
    # client lazily, so a missing key used to surface as a JevError halfway
    # through resolution and be recorded as degradation. No key is a
    # different state from a failing key, and the response says which.
    available = _model_configured()

    if request.use_model and available:
        jev = JevBinder(cache=_THEME_CACHE, option_cache=_OPTION_CACHE,
                        form_context={"company": form.company or "",
                                      "title": form.title or ""})
        binder = jev
    elif request.use_model:
        logger.warning("No model key configured; building a deterministic plan")

    model_error: Optional[str] = None
    try:
        plan = resolve_form(form, memory, company=request.company, binder=binder)
        if jev is not None and getattr(jev, "calls", 0):
            _note_model_ok(caller)
    except JevError as e:
        # The decision service was reachable but failed. Rather than returning
        # nothing, retry without it: a deterministic plan is worth far more than
        # an error page.
        # The summary, not the upstream body: /health was sanitised (M5) and
        # the log line carried the same text (review of 2026-10-12).
        model_error = _summarise_error(e)
        logger.warning("Jev failed mid-plan, retrying deterministically: %s", model_error)
        _note_model_error(e, caller)
        plan = resolve_form(form, memory, company=request.company,
                            binder=DeterministicBinder())
        jev = None

    if model_error is None and jev is not None and getattr(jev, "failed", False):
        # A call raised and was swallowed (the speculative gate) or tripped
        # the breaker; the plan is complete but not the one the model would
        # have made.
        model_error = getattr(jev, "last_error", None) or "model call failed"

    return AutofillResponse(
        model_available=available,
        model_degraded=bool(available and _model_degraded(caller)),
        plan=plan,
        summary=plan.summary(),
        jev_calls=jev.calls if jev else 0,
        cost_usd=jev.cost_usd if jev else 0.0,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        model_skipped=bool(jev is not None and getattr(jev, "tripped", False)),
        model_failed=model_error is not None,
        model_error=model_error,
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
    #: True when the judge was called and raised; the episodes were still
    #: saved, the proposals are the ones no model saw (audit 2026-10-12, F21).
    model_failed: bool = False
    model_error: Optional[str] = None


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
        raise HTTPException(status_code=422, detail=_profile_error_detail(e))

    accepted = [key for key in request.accept if accept(memory, key)]

    binder: Optional[JevBinder] = None
    if request.use_model and request.observations:
        if _model_configured():
            binder = JevBinder(cache=_THEME_CACHE, option_cache=_OPTION_CACHE)
        else:
            logger.warning("No model key configured; learning episodes only")

    model_error: Optional[str] = None
    try:
        result: LearnResult = learn(memory, request.observations, binder=binder)
    except JevError as e:
        # The summary, never `e` itself: a 4xx that echoed the judge's
        # questions put the applicant's answers in the log (audit
        # 2026-10-12, F9; probe p08).
        model_error = _summarise_error(e)
        logger.warning("Jev failed while judging corrections; episodes only: %s", model_error)
        result = learn(memory, request.observations, binder=None)
    if model_error is None and binder is not None and getattr(binder, "failed", False):
        model_error = getattr(binder, "last_error", None) or "model call failed"

    return LearnResponse(
        profile=result.profile, learned=result.learned, accepted=accepted,
        proposals=[p.model_dump() for p in result.proposals], ignored=result.ignored,
        jev_calls=result.jev_calls, cost_usd=result.cost_usd, engine=ENGINE,
        model_failed=model_error is not None, model_error=model_error,
    )


#: What the model path last did, so /health tells the truth about it.
#: `model_available` only says a key is configured; on 2026-10-11 the key had
#: expired, every plan fell back to deterministic, and nothing said so until
#: an audit run came back with two model decisions across five hundred forms.
#:
#: Process-wide, plus one record per caller (the install token that is also
#: the rate-limit key). A plan reports the caller's own record when there is
#: one, else the global one: one caller's transient failure no longer marks
#: every other caller degraded (audit 2026-10-12, L2), while a new caller
#: still inherits the signal an expired key leaves behind. Bounded.
_MODEL_STATUS: Dict[str, Any] = {"last_error": None, "last_error_at": None, "last_ok_at": None}
_CALLER_STATUS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_CALLER_STATUS_MAX = 1000
#: Monotonic sequence for ordering events: two within one second share an
#: ISO timestamp and could not be ordered.
_MODEL_SEQ = {"n": 0}


#: The sanitised category of a model failure, for /health, the response and
#: every log line. Defined in `app.jev_client` beside the error classes so the
#: binder can use it too; kept under this name here for its callers.
_summarise_error = summarise_error


def _stamp() -> str:
    import datetime
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


#: Handlers run in FastAPI's threadpool. Without this, one thread's
#: `get` + `move_to_end` on a caller record could straddle another thread's
#: eviction of that same (oldest) record and raise KeyError — a 500 for the
#: applicant (review of 2026-10-12, finding 6). Reads in `_model_degraded`
#: are single dict lookups and stay unlocked.
_STATUS_LOCK = threading.Lock()


def _caller_record(caller: Optional[str]) -> Optional[Dict[str, Any]]:
    """The caller's record, created and made most recent. Call under `_STATUS_LOCK`."""
    if not caller:
        return None
    record = _CALLER_STATUS.get(caller)
    if record is None:
        record = _CALLER_STATUS[caller] = {"error_seq": 0, "ok_seq": 0}
        while len(_CALLER_STATUS) > _CALLER_STATUS_MAX:
            _CALLER_STATUS.popitem(last=False)
    _CALLER_STATUS.move_to_end(caller)
    return record


def _note_model_error(error: Exception, caller: Optional[str] = None) -> None:
    with _STATUS_LOCK:
        _MODEL_SEQ["n"] += 1
        _MODEL_STATUS["last_error"] = _summarise_error(error)
        _MODEL_STATUS["last_error_at"] = _stamp()
        _MODEL_STATUS["error_seq"] = _MODEL_SEQ["n"]
        record = _caller_record(caller)
        if record is not None:
            record["error_seq"] = _MODEL_SEQ["n"]


def _note_model_ok(caller: Optional[str] = None) -> None:
    with _STATUS_LOCK:
        _MODEL_SEQ["n"] += 1
        _MODEL_STATUS["last_ok_at"] = _stamp()
        _MODEL_STATUS["ok_seq"] = _MODEL_SEQ["n"]
        record = _caller_record(caller)
        if record is not None:
            record["ok_seq"] = _MODEL_SEQ["n"]


def _degraded_in(record: Dict[str, Any]) -> bool:
    """The last thing the model did was fail, with no success since."""
    error_seq, ok_seq = record.get("error_seq") or 0, record.get("ok_seq") or 0
    if not error_seq:
        # A status reset from outside (tests) clears the timestamps only.
        return bool(record.get("last_error_at") and not record.get("last_ok_at"))
    return error_seq > ok_seq


def _model_degraded(caller: Optional[str] = None) -> bool:
    record = _CALLER_STATUS.get(caller) if caller else None
    if record is not None and (record.get("error_seq") or record.get("ok_seq")):
        return _degraded_in(record)
    if not _MODEL_STATUS.get("last_error_at"):
        return False
    return _degraded_in(_MODEL_STATUS)


def _model_configured() -> bool:
    """Whether an OpenRouter key is set and well-formed. Reads the environment
    only; no request is made."""
    try:
        get_jev_client()
    except JevConfigurationError:
        return False
    return True


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

    configured = _model_configured()

    # Degraded = configured but the last thing it did was fail, with no
    # success since: the state an expired key leaves you in.
    degraded = bool(configured and _model_degraded())

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
