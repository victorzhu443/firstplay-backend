"""
The learning loop: what the applicant does on a form becomes what the engine
knows (DECISIONS §50).

Victor, 2026-10-01: "build an agent that as we use it improves and understands
what I do to answer questions and starts being able to answer my own
questions." The extension watches what the applicant types or picks after a
fill and sends those observations here with the profile; this module returns
the profile to store. Nothing is kept on the server.

Three memories, after the cognitive-architecture framing of Sumers et al.
(CoALA, 2023):

* **Episodic** — the exact question and the applicant's exact answer, saved
  verbatim in `profile.answers` under the label's fingerprint. Automatic.
  The next form that asks the same question is answered from it with no
  model call (`Memory.recall_answer`). This is also the labelled example set
  every later step learns from.
* **Semantic** — a fact about the applicant, independent of any one employer.
  Each new episode is judged (Jev, two bounded questions: is it stable? which
  profile key is it the value of, or is it a new kind of fact?) and becomes a
  *proposal*: set an empty key, or add a new key under `profile.learned`.
  Proposals are never applied here; the applicant accepts them in the popup.
  Support — how many distinct companies' forms produced the same answer — is
  tracked in `profile.learned_pending` so recurring answers rise to the top.
  This is the verbal-feedback turn of Reflexion (Shinn et al., 2023) made
  durable: the correction is the feedback; memory is where it lands.
* **Procedural** — nothing new to store: the plan pipeline already reads
  `answers`, and the gate and the second pass read `learned` as they read
  any stored fact. A fact promoted once is used everywhere after, the way
  Voyager (Wang et al., 2023) reuses a skill once it is in the library.

What is never learned as a fact: protected characteristics (their answers may
be replayed, never generalised), consents and attestations (the applicant's
act each time), essays (prose for one employer), and documents.
"""
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from app.autofill.classify import classify
from app.autofill.memory import Memory
from app.autofill.schema import FieldClass, FieldKind, FillSource, FormField

#: A correction becomes a proposal only when the model is this sure it is a
#: stable fact and this sure which key it belongs to. Below, it stays an
#: episode: replayed for the same question, never generalised.
STABLE_FACT = 0.90
KEY_CONFIDENCE = 0.85

#: Sections whose keys may receive a learned value (never protected, consents).
_PROPOSABLE_SECTIONS = ("facts", "education", "preferences", "legal_status", "learned")
_NEVER_PROPOSE = {"resume_file", "cover_letter", "transcript_file"}


class Observation(BaseModel):
    """One field as the applicant left it, against what the plan had."""
    ats: str = ""
    company: Optional[str] = None
    posting_id: Optional[str] = None
    field_key: str = ""
    label: str
    kind: str = "text"
    options: List[str] = Field(default_factory=list)
    required: bool = False
    plan_value: Optional[object] = None
    plan_source: Optional[str] = None
    user_value: object
    at: Optional[str] = None


class Proposal(BaseModel):
    key: str
    value: str
    label: str
    support: int = 1
    why: str = ""
    section: str = "learned"


class LearnResult(BaseModel):
    profile: Dict[str, object]
    learned: int = 0
    proposals: List[Proposal] = Field(default_factory=list)
    ignored: List[Dict[str, str]] = Field(default_factory=list)
    jev_calls: int = 0
    cost_usd: float = 0.0


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " | ".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def _kind(name: str) -> FieldKind:
    try:
        return FieldKind(name)
    except ValueError:
        return FieldKind.TEXT


def slug_for(label: str) -> str:
    """A deterministic snake_case key for a new kind of fact, from its label.

    "Please provide your university email address." -> "university_email_address";
    "What is your T-shirt size?" -> "t_shirt_size". Same label, same key, so
    support accumulates across forms.
    """
    text = (label or "").lower()
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    text = re.sub(r"^(please |kindly )?(provide|enter|share|list|tell us|give us|add|include|specify)( us)?( your| the| a| an)? ", "", text)
    text = re.sub(r"^(what|which|who|where|when|how)( is| are| was| were)?( your| the)? ", "", text)
    text = re.sub(r"^(your|my|the|a|an) ", "", text)
    text = re.sub(r" (if (any|applicable|you have one)|optional).*$", "", text)
    words = text.split()[:5]
    return "_".join(words)[:40] or "learned_fact"


def _catalog(memory: Memory) -> Dict[str, str]:
    """Every non-protected profile key, empty ones included, for the key choice."""
    out: Dict[str, str] = {}
    for section in _PROPOSABLE_SECTIONS:
        for key, value in (getattr(memory, section, None) or {}).items():
            if key in _NEVER_PROPOSE:
                continue
            out[key] = str(value or "")
    return out


def _section_of(memory: Memory, key: str) -> str:
    for section in _PROPOSABLE_SECTIONS:
        if key in (getattr(memory, section, None) or {}):
            return section
    return "learned"


def learn(memory: Memory, observations: List[Observation], binder=None) -> LearnResult:
    """Fold the applicant's edits into the profile; propose what generalises.

    Args:
        memory: The profile as the extension holds it
        observations: Fields as the applicant left them
        binder: A JevBinder (with `judge_observations`) for the semantic step,
            or None for the episodic step alone

    Returns:
        The profile to store, what was saved, what is proposed, what was
        ignored and why
    """
    saved = 0
    ignored: List[Dict[str, str]] = []
    candidates: List[Tuple[str, Observation, str, FieldClass]] = []

    for index, obs in enumerate(observations):
        value = _as_text(obs.user_value)
        if not value:
            ignored.append({"label": obs.label, "why": "left empty"})
            continue
        if _as_text(obs.plan_value) == value:
            ignored.append({"label": obs.label, "why": "same as the plan; nothing new"})
            continue
        kind = _kind(obs.kind)
        field_class = classify(obs.field_key, obs.label, kind, option_count=len(obs.options))
        field = FormField(key=obs.field_key or "q", label=obs.label, kind=kind,
                          field_class=field_class, required=obs.required, options=[])

        if field_class == FieldClass.CONSENT:
            ignored.append({"label": obs.label, "why": "a consent or attestation is yours to give each time"})
            continue
        if field_class in (FieldClass.NARRATIVE, FieldClass.DOCUMENT) or kind in (FieldKind.LONG_TEXT, FieldKind.FILE):
            ignored.append({"label": obs.label, "why": "an essay or a document is not replayed"})
            continue
        if not field.may_fill_from(FillSource.MEMORY):
            ignored.append({"label": obs.label, "why": "this field is never filled from memory"})
            continue

        # Episodic: the exact question gets the exact answer next time.
        if memory.recall_answer(obs.label) != value:
            memory.remember_answer(obs.label, value)
            saved += 1

        if field_class == FieldClass.LEGAL:
            # Replayed for this question, never generalised into a fact.
            continue
        candidates.append(("obs_{}".format(index), obs, value, field_class))

    proposals: List[Proposal] = []
    jev_calls = 0
    cost = 0.0
    if candidates and binder is not None and hasattr(binder, "judge_observations"):
        catalog = _catalog(memory)
        state = {"profile": catalog}
        before_calls, before_cost = getattr(binder, "calls", 0), getattr(binder, "cost_usd", 0.0)
        judged = binder.judge_observations(
            state, [(item_id, obs.label, value, catalog) for item_id, obs, value, _c in candidates]
        )
        jev_calls = getattr(binder, "calls", 0) - before_calls
        cost = getattr(binder, "cost_usd", 0.0) - before_cost

        for item_id, obs, value, _c in candidates:
            p_stable, key, p_key = judged.get(item_id, (0.0, None, 0.0))
            if p_stable < STABLE_FACT:
                ignored.append({"label": obs.label, "why": "answer kept for this question only; {:.0%} that it is a general fact".format(p_stable)})
                continue
            if not key or p_key < KEY_CONFIDENCE:
                ignored.append({"label": obs.label, "why": "a general fact, but unsure where it belongs ({:.0%})".format(p_key)})
                continue
            if key == "new":
                key = slug_for(obs.label)
                section = "learned"
            else:
                section = _section_of(memory, key)
            current = (getattr(memory, section, None) or {}).get(key) or ""
            if current and current.strip().lower() == value.strip().lower():
                ignored.append({"label": obs.label, "why": "already stored as {}".format(key)})
                continue
            pending = dict(memory.learned_pending.get(key) or {})
            companies = list(pending.get("companies") or [])
            if obs.company and obs.company not in companies:
                companies.append(obs.company)
            labels = list(pending.get("labels") or [])
            if obs.label not in labels:
                labels.append(obs.label)
            memory.learned_pending[key] = {
                "value": value, "section": section, "support": max(1, len(companies)),
                "labels": labels[:10], "companies": companies[:20],
                "updated": obs.at or datetime.utcnow().isoformat(timespec="seconds"),
            }
            why = ("would replace the stored {!r}".format(current) if current
                   else ("a new kind of fact ({:.0%} stable)".format(p_stable) if section == "learned"
                         else "fills the empty {} ({:.0%} stable)".format(key, p_stable)))
            proposals.append(Proposal(key=key, value=value, label=obs.label,
                                      support=max(1, len(companies)), why=why, section=section))

    # One proposal per key, the most-supported first.
    seen: Dict[str, Proposal] = {}
    for proposal in proposals:
        if proposal.key not in seen or proposal.support > seen[proposal.key].support:
            seen[proposal.key] = proposal
    ordered = sorted(seen.values(), key=lambda p: (-p.support, p.key))

    return LearnResult(profile=memory.model_dump(), learned=saved, proposals=ordered,
                       ignored=ignored, jev_calls=jev_calls, cost_usd=cost)


def accept(memory: Memory, key: str) -> bool:
    """Promote a pending proposal into the profile (the popup's Accept)."""
    pending = memory.learned_pending.get(key)
    if not pending:
        return False
    section = str(pending.get("section") or "learned")
    target = getattr(memory, section, None)
    if target is None or section not in _PROPOSABLE_SECTIONS:
        target, section = memory.learned, "learned"
    target[key] = str(pending.get("value") or "")
    memory.learned_pending.pop(key, None)
    return True
