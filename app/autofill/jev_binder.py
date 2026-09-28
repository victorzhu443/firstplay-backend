"""
Theme classification with Jev, plus the cache that makes it nearly free.

The binder answers one question per unresolved field — "which of these ~14
themes does this label ask about?" — and answers all of them in a **single**
call, because Jev sends state once and a tenth question costs tokens but almost
no time.

**The cache is the point, not an optimisation.** Measured over 150 live
Greenhouse postings, the 1,010 screening fields used only 117 distinct labels:
8.5 reuses each, and 147 for the most common. Keyed on the normalised label, a
label classified once never needs classifying again — so the first application
costs one call and later ones trend toward zero. Two consequences worth stating:

  - Cost and latency are a function of *distinct labels seen*, not of
    applications submitted. A job search gets cheaper as it goes.
  - The cache is a labelled dataset. Every entry is (label -> theme) as judged
    by a calibrated model, and every correction the applicant makes in review
    overrides one. That is exactly the training data a local classifier would
    need, which is what keeps the `LocalBinder` option open rather than
    theoretical.

Confidence is *not* cached as a certainty. It is stored alongside the theme and
re-applied to the threshold each time, so raising `AUTOFILL_CONFIDENCE` later
tightens historical entries too rather than grandfathering them in.
"""
import hashlib
import json
import logging
import os
from typing import Dict, List, Optional, Tuple

from app.autofill.classify import normalize_label
from app.autofill.themes import THEME_CRITERIA, QuestionTheme
from app.autofill.schema import FormField
from app.jev_client import JevClient, choice, get_jev_client

logger = logging.getLogger(__name__)

#: One Jev call carries at most this many questions. Not an API limit — a guard
#: so that a pathological form cannot build a single enormous request whose
#: state also blows the 32k context window.
MAX_QUESTIONS_PER_CALL = 40

_INSTRUCTIONS = (
    "A job application form asks the candidate this question:\n\n"
    "  {label}\n\n"
    "Which of the following best describes what it is asking for?"
)


def taxonomy_fingerprint() -> str:
    """Identity of the current theme set.

    A cached classification is only valid against the taxonomy it was made
    with. Adding GRADUATE_PROGRAM did nothing for any label already cached under
    the older set, so a confident wrong "Yes" to "are you in a Masters or PhD
    program?" survived a fix that should have removed it — invisibly, because
    the cache reported a hit. Entries from a different taxonomy are now dropped.
    """
    return hashlib.sha256(
        "|".join(sorted(t.value for t in QuestionTheme)).encode()
    ).hexdigest()[:12]


class ThemeCache:
    """Normalised label -> (theme, confidence), optionally persisted as JSON."""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._entries: Dict[str, Tuple[str, float]] = {}
        self.hits = 0
        self.misses = 0
        self.invalidated = 0

        if path and os.path.exists(path):
            try:
                with open(path) as handle:
                    raw = json.load(handle)
                stored = raw.pop("__taxonomy__", None) if isinstance(raw, dict) else None
                if stored == taxonomy_fingerprint():
                    self._entries = {k: (v[0], float(v[1])) for k, v in raw.items()}
                else:
                    # Dropped rather than migrated: re-classifying is cheap
                    # ($0.0000354 a label) and a stale theme is a wrong answer.
                    self.invalidated = len(raw)
                    logger.info(
                        "Theme taxonomy changed; discarding %d cached classifications",
                        self.invalidated,
                    )
            except Exception:
                # A corrupt cache must not stop a run: it is derived data and
                # rebuilds itself on the next call.
                logger.warning("Could not read theme cache at %s; starting empty", path)

    def get(self, label: str) -> Optional[Tuple[QuestionTheme, float]]:
        entry = self._entries.get(normalize_label(label))

        if entry is None:
            self.misses += 1
            return None

        self.hits += 1

        try:
            return QuestionTheme(entry[0]), entry[1]
        except ValueError:
            # A theme name that no longer exists in the enum. Treated as a miss
            # so a renamed theme does not resurrect stale classifications.
            return None

    def put(self, label: str, theme: QuestionTheme, confidence: float) -> None:
        self._entries[normalize_label(label)] = (theme.value, confidence)

    def save(self) -> None:
        if not self.path:
            return

        payload = {k: [v[0], v[1]] for k, v in self._entries.items()}
        payload["__taxonomy__"] = taxonomy_fingerprint()

        with open(self.path, "w") as handle:
            json.dump(payload, handle, indent=1)

    @property
    def size(self) -> int:
        return len(self._entries)

    def hit_rate(self) -> Optional[float]:
        total = self.hits + self.misses

        return (self.hits / total) if total else None


#: Asking which of a form's own options expresses a stored answer. The subject
#: being judged — the answer and the question — goes in **state**, not in the
#: instructions: a smoke test that put the subject in the instructions produced
#: a confident Choice but mushy Noul and Score results, which is what the
#: guidance means by state being "everything you would place in front of an
#: expert before asking them to judge".
_OPTION_INSTRUCTIONS = (
    "The candidate has already answered this question for themselves. Which of "
    "the listed options expresses that same answer? Choose `none` if no option "
    "expresses it."
)


_ANSWER_INSTRUCTIONS = (
    "You are filling a job application for the applicant described in state.applicant. "
    "state.form.earlier_answers lists what has already been answered on this same form. "
    "Choose the option that TRUTHFULLY answers the question for this applicant, using only "
    "the profile and those earlier answers. A question that presupposes something untrue of "
    "the applicant (e.g. it asks about a visa programme when the applicant is a citizen) is "
    "answered with the option that says it does not apply, when one is offered. "
    "Choose 'unsure' whenever the profile does not determine the answer — never guess."
)


class OptionCache:
    """signature -> (chosen option label, confidence).

    Separate from ThemeCache rather than reusing it: that one stores a
    QuestionTheme, and an option label is not one. Forcing it through lost the
    label on every cache hit, so matches cached in one run came back as "no
    match" in the next — visible only as coverage quietly dropping 4 points.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._entries: Dict[str, Tuple[Optional[str], float]] = {}
        self.hits = 0
        self.misses = 0

        if path and os.path.exists(path):
            try:
                with open(path) as handle:
                    raw = json.load(handle)
                self._entries = {k: (v[0], float(v[1])) for k, v in raw.items()}
            except Exception:
                logger.warning("Could not read option cache at %s; starting empty", path)

    def get(self, signature: str) -> Optional[Tuple[Optional[str], float]]:
        entry = self._entries.get(signature)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def put(self, signature: str, label: Optional[str], confidence: float) -> None:
        self._entries[signature] = (label, confidence)

    def save(self) -> None:
        if not self.path:
            return
        with open(self.path, "w") as handle:
            json.dump({k: [v[0], v[1]] for k, v in self._entries.items()}, handle, indent=1)

    @property
    def size(self) -> int:
        return len(self._entries)


class JevBinder:
    """Classifies labels into themes with one batched Jev call per form."""

    name = "jev"

    def __init__(
        self,
        client: Optional[JevClient] = None,
        *,
        cache: Optional[ThemeCache] = None,
        option_cache: Optional["OptionCache"] = None,
        form_context: Optional[Dict[str, str]] = None,
    ):
        #: Deferred so a missing key raises only when a call is actually needed.
        #: A fully cached form should not require credentials at all.
        self._client = client
        self._cache = cache if cache is not None else ThemeCache()
        self._form_context = form_context or {}

        self.calls = 0
        self.cost_usd = 0.0
        self.questions_asked = 0

        self._options = option_cache if option_cache is not None else OptionCache(
            (cache.path.replace("themes.json", "options.json")
             if cache is not None and cache.path else None)
        )

    @property
    def cache(self) -> ThemeCache:
        return self._cache

    @property
    def option_cache(self) -> "OptionCache":
        return self._options

    def save_caches(self) -> None:
        self._cache.save()
        self._options.save()

    def _ensure_client(self) -> JevClient:
        if self._client is None:
            self._client = get_jev_client()

        return self._client

    def classify_themes(
        self, labels: List[str]
    ) -> Dict[str, Tuple[QuestionTheme, float]]:
        """Map each label to a theme, using the cache and at most a few calls.

        Args:
            labels: Field labels needing classification. Duplicates and blanks
                are handled here rather than by the caller.

        Returns:
            label -> (theme, confidence) for every label given
        """
        out: Dict[str, Tuple[QuestionTheme, float]] = {}
        pending: List[str] = []

        for label in labels:
            if not (label or "").strip():
                out[label] = (QuestionTheme.UNKNOWN, 0.0)
                continue

            cached = self._cache.get(label)
            if cached is not None:
                out[label] = cached
            elif label not in pending:
                pending.append(label)

        for batch_start in range(0, len(pending), MAX_QUESTIONS_PER_CALL):
            batch = pending[batch_start:batch_start + MAX_QUESTIONS_PER_CALL]
            out.update(self._classify_batch(batch))

        # Labels that appeared more than once share one classification.
        for label in labels:
            if label not in out:
                out[label] = (QuestionTheme.UNKNOWN, 0.0)

        return out

    def match_options(
        self, requests: List[Tuple[str, str, FormField]]
    ) -> Dict[str, Tuple[Optional[str], float]]:
        """Map stored answers onto each form's own option wording.

        Deterministic matching handles exact text, yes/no, declining, and
        numeric and date buckets. What is left is genuinely semantic: a stored
        "Yes" against Lyft's "I am willing to relocate before starting
        employment.", or a country name against a 31-entry list. That is a
        bounded choice over options the form itself supplies, which is the
        shape Jev exists for.

        Args:
            requests: (request_id, stored_value, field) tuples

        Returns:
            request_id -> (chosen option label or None, confidence)
        """
        if not requests:
            return {}

        out: Dict[str, Tuple[Optional[str], float]] = {}
        pending = []

        # Cached on the same terms as theme classification, and for the same
        # reason: the corpus reuses question wordings heavily, so re-paying for
        # an identical match on every run is pure waste. Keyed on the stored
        # answer *and* the option set, since either changing changes the answer.
        for request_id, value, field in requests:
            if len(field.options) > 254:
                out[request_id] = (None, 0.0)
                continue
            signature = self._option_signature(value, field)
            cached = self._options.get(signature)
            if cached is not None:
                out[request_id] = cached
                continue
            pending.append((request_id, value, field, signature))

        for start in range(0, len(pending), MAX_QUESTIONS_PER_CALL):
            batch = pending[start:start + MAX_QUESTIONS_PER_CALL]
            out.update(self._match_batch([(r, v, f) for r, v, f, _sig in batch]))
            for request_id, _v, _f, signature in batch:
                label, confidence = out.get(request_id, (None, 0.0))
                self._options.put(signature, label, confidence)

        return out

    def answer_from_profile(
        self, state: Dict[str, object], requests: List[Tuple[str, FormField]]
    ) -> Dict[str, Tuple[Optional[str], float]]:
        """Decide, from the applicant's profile, which option answers a question.

        The gate Victor asked for by example: "If so, are you eligible for
        OPT?" and its 24-month follow-up, for a US citizen who has just
        answered "No" to sponsorship, are both "NA" — obvious to anyone who
        reads the profile, and reachable by no lookup because the question is
        about a programme the applicant is not in. This is a bounded choice
        over options the form supplies, with the profile as state: Jev's
        shape. Barred from LEGAL and CONSENT fields by the caller.

        Args:
            state: `{"applicant": {...}, "form": {"earlier_answers": [...]}}`
            requests: (request_id, field) tuples

        Returns:
            request_id -> (chosen option label or None, confidence)
        """
        if not requests:
            return {}

        state_hash = hashlib.sha1(
            json.dumps(state.get("applicant", {}), sort_keys=True, default=str).encode()
        ).hexdigest()[:12]

        out: Dict[str, Tuple[Optional[str], float]] = {}
        pending = []

        for request_id, field in requests:
            # A Choice takes at most 255 options; a 552-university list
            # (Rothesay, corpus-large) must not fail the whole form.
            if len(field.options) > 254:
                out[request_id] = (None, 0.0)
                continue
            signature = "answer::{}::{}::{}".format(
                field.label[:80], "|".join(o.label for o in field.options)[:200], state_hash
            )
            cached = self._options.get(signature)
            if cached is not None:
                out[request_id] = cached
                continue
            pending.append((request_id, field, signature))

        for start in range(0, len(pending), MAX_QUESTIONS_PER_CALL):
            batch = pending[start:start + MAX_QUESTIONS_PER_CALL]
            out.update(self._answer_batch(state, [(r, f) for r, f, _sig in batch]))
            for request_id, _field, signature in batch:
                label, confidence = out.get(request_id, (None, 0.0))
                self._options.put(signature, label, confidence)

        return out

    def _answer_batch(
        self, state: Dict[str, object], batch: List[Tuple[str, FormField]]
    ) -> Dict[str, Tuple[Optional[str], float]]:
        questions = {}
        option_labels: Dict[str, Dict[str, str]] = {}

        for request_id, field in batch:
            criteria = {}
            labels = {}
            for index, option in enumerate(field.options):
                key = "opt_{}".format(index)
                criteria[key] = option.label
                labels[key] = option.label
            criteria["unsure"] = "The profile and earlier answers do not determine the answer."
            option_labels[request_id] = labels
            questions[request_id] = choice(
                "{}\n\nQuestion on the form: {}".format(_ANSWER_INSTRUCTIONS, field.label),
                criteria,
            )

        decision = self._ensure_client().decide(
            state, questions, description="answer {} question(s) from the profile".format(len(batch))
        )

        self.calls += 1
        self.cost_usd += decision.cost_usd
        self.questions_asked += len(batch)

        out: Dict[str, Tuple[Optional[str], float]] = {}

        for request_id, _field in batch:
            answer = decision.get(request_id)
            if answer is None or not answer.choice or answer.choice == "unsure":
                out[request_id] = (None, answer.belief() if answer else 0.0)
                continue
            out[request_id] = (option_labels[request_id].get(answer.choice), answer.belief())

        return out

    @staticmethod
    def _option_signature(value: str, field) -> str:
        """Identity of one option-matching question."""
        options = "|".join(o.label for o in field.options)

        return "optmatch::{}::{}::{}".format(field.label[:60], value[:40], options[:200])

    def _match_batch(
        self, batch: List[Tuple[str, str, FormField]]
    ) -> Dict[str, Tuple[Optional[str], float]]:
        state = {"questions": [
            {"id": request_id, "question": field.label, "candidate_answer": value}
            for request_id, value, field in batch
        ]}

        questions = {}
        option_labels: Dict[str, Dict[str, str]] = {}

        for request_id, _value, field in batch:
            # Keyed by index rather than by label: option text repeats across
            # forms and is not guaranteed unique within one.
            criteria = {}
            labels = {}
            for index, option in enumerate(field.options):
                key = "opt_{}".format(index)
                criteria[key] = option.label
                labels[key] = option.label
            criteria["none"] = "No listed option expresses the candidate's answer."

            option_labels[request_id] = labels
            questions[request_id] = choice(
                "{}\n\nQuestion id: {}".format(_OPTION_INSTRUCTIONS, request_id), criteria
            )

        decision = self._ensure_client().decide(
            state, questions, description="match {} option set(s)".format(len(batch))
        )

        self.calls += 1
        self.cost_usd += decision.cost_usd
        self.questions_asked += len(batch)

        out: Dict[str, Tuple[Optional[str], float]] = {}

        for request_id, _value, _field in batch:
            answer = decision.get(request_id)
            if answer is None or not answer.choice or answer.choice == "none":
                out[request_id] = (None, answer.belief() if answer else 0.0)
                continue
            out[request_id] = (
                option_labels[request_id].get(answer.choice), answer.belief()
            )

        return out

    def _classify_batch(self, batch: List[str]) -> Dict[str, Tuple[QuestionTheme, float]]:
        # Only the form's identity goes into state, not the whole form. Accuracy
        # falls as state fills with material the question does not need, and the
        # question already carries the label it is about.
        state = {"form": self._form_context} if self._form_context else {"form": {}}

        questions = {}
        ids: Dict[str, str] = {}

        for index, label in enumerate(batch):
            question_id = "theme_{}".format(index)
            ids[question_id] = label
            questions[question_id] = choice(
                _INSTRUCTIONS.format(label=label), THEME_CRITERIA
            )

        decision = self._ensure_client().decide(
            state, questions, description="classify {} form question(s)".format(len(batch))
        )

        self.calls += 1
        self.cost_usd += decision.cost_usd
        self.questions_asked += len(batch)

        out: Dict[str, Tuple[QuestionTheme, float]] = {}

        for question_id, label in ids.items():
            answer = decision.get(question_id)

            if answer is None or not answer.choice:
                out[label] = (QuestionTheme.UNKNOWN, 0.0)
                continue

            try:
                theme = QuestionTheme(answer.choice)
            except ValueError:
                # The model returned an option name outside the enum. Structurally
                # this should be impossible — answers are constrained to the
                # criteria supplied — so it is logged rather than swallowed.
                logger.warning("Jev returned unknown theme %r for %r", answer.choice, label)
                out[label] = (QuestionTheme.UNKNOWN, 0.0)
                continue

            confidence = answer.belief()
            out[label] = (theme, confidence)
            self._cache.put(label, theme, confidence)

        return out
