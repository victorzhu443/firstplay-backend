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
import re
import json
import logging
import os
from typing import Dict, List, Optional, Tuple

from app.autofill.classify import normalize_label
from app.autofill.themes import THEME_CRITERIA, QuestionTheme
from app.autofill.schema import FieldKind, FormField
from app.exceptions import JevServiceError
from app.jev_client import JevClient, choice, get_jev_client, noul

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


#: Per-option yes/no for a "check all that apply" question. A Noul wants an
#: observable situation on each side, and an undetermined proposition comes
#: back near 0.5 — which is exactly what the caller treats as "ask the
#: applicant".
_APPLIES_INSTRUCTIONS = (
    "You are filling a job application for the applicant described in state.applicant; "
    "state.form.earlier_answers lists what is already answered on this form. The form asks "
    "a select-all-that-apply question. Judge whether ONE option truthfully applies to this "
    "applicant, using only facts stated in the profile and earlier answers. If the profile "
    "says nothing that bears on the option, it does NOT apply here — the applicant decides. "
    "An option that means none / not applicable / never held / no is the one that applies "
    "when the profile states the applicant has none of the things the question lists."
)

#: Noul thresholds for one option of a multi-select. Between them the option
#: is undecided and the whole field stays with the applicant.
MULTI_YES = 0.90
MULTI_NO = 0.10

#: The opt-out entry of a "check all that apply" list. A Noul scores "Not
#: Applicable" low as a proposition even when the profile says the applicant
#: has none of the listed things (Rocket Lab's clearance list: every level
#: at p<=0.05, "Not Applicable" at 0.15). When every concrete option is
#: confidently ruled out and exactly one option means none, that one is the
#: answer by elimination — and its confidence is the weakest of those no's.
_NONE_OPTION = re.compile(
    r"\b(none|not applicable|n/?a|never held|no clearance|do not have|i do not have|neither)\b", re.I
)

_COVERED_INSTRUCTIONS = (
    "state.profile lists what the applicant has recorded about themselves, as key: value. "
    "Decide whether that profile contains the specific information this free-text question "
    "asks for. 'Contains' means a value that directly states it — not something from which it "
    "could be guessed. A question about the applicant's own words, opinions, motivations, "
    "stories, references, or anything not stated is NOT covered."
)
_KEY_INSTRUCTIONS = (
    "state.profile lists the applicant's recorded facts as key: value. Choose the ONE key whose "
    "stored value, written verbatim into this field, truthfully answers the question. Choose "
    "'none' when no single value does, when the field wants prose, or when the value would be "
    "only loosely related (a graduation date is not a start date; a current location is not an "
    "address; an email is not a university email unless it says so)."
)
_VERIFY_INSTRUCTIONS = (
    "state.profile is the applicant's recorded information. A proposed answer is about to be "
    "written into a free-text field on a job application under the applicant's name. Judge "
    "whether it is a truthful, directly responsive answer to the question as asked."
)

_STABLE_INSTRUCTIONS = (
    "The applicant corrected or supplied an answer on a job application form. state.profile "
    "lists what the applicant has recorded about themselves. Decide whether the answer is a "
    "stable fact or standing preference about the applicant — something that would be answered "
    "the same way on any employer's form asking the same question — as opposed to something "
    "specific to this company, this role, this posting, or this moment (a reason for interest, "
    "a referral name, an office choice for this employer, an essay, a date that depends on this "
    "role)."
)
_WHERE_INSTRUCTIONS = (
    "state.profile lists the applicant's recorded facts as key: value (some empty). Choose the "
    "ONE key this answer is the value of — the same kind of information, so that storing the "
    "answer under that key would answer this question on other forms. Choose 'new' when no "
    "listed key is that kind of information."
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

        self.tripped = False
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
            signature = "answer::{}::{}::{}::{}".format(
                field.kind.value, field.label[:80],
                "|".join(o.label for o in field.options)[:200], state_hash,
            )
            cached = self._options.get(signature)
            if cached is not None:
                out[request_id] = cached
                continue
            pending.append((request_id, field, signature))

        # Batched by *questions*, not requests: a multi-select is one question
        # per option, and the call cap is on questions.
        batch: List[Tuple[str, FormField, str]] = []
        weight = 0
        for item in pending:
            cost = len(item[1].options) if item[1].kind == FieldKind.MULTI_SELECT else 1
            if batch and weight + cost > MAX_QUESTIONS_PER_CALL:
                out.update(self._answer_batch(state, [(r, f) for r, f, _sig in batch]))
                for request_id, _field, signature in batch:
                    self._options.put(signature, *out.get(request_id, (None, 0.0)))
                batch, weight = [], 0
            batch.append(item)
            weight += cost
        if batch:
            out.update(self._answer_batch(state, [(r, f) for r, f, _sig in batch]))
            for request_id, _field, signature in batch:
                self._options.put(signature, *out.get(request_id, (None, 0.0)))

        return out

    def _answer_batch(
        self, state: Dict[str, object], batch: List[Tuple[str, FormField]]
    ) -> Dict[str, Tuple[Optional[str], float]]:
        questions = {}
        option_labels: Dict[str, Dict[str, str]] = {}

        multi: Dict[str, List[Tuple[str, str]]] = {}   # request_id -> [(question_id, option label)]

        for request_id, field in batch:
            if field.kind == FieldKind.MULTI_SELECT:
                per_option = []
                for index, option in enumerate(field.options):
                    question_id = "{}__opt_{}".format(request_id, index)
                    questions[question_id] = noul(
                        "{}\n\nQuestion on the form: {}\nOption being judged: {}".format(
                            _APPLIES_INSTRUCTIONS, field.label, option.label
                        ),
                        true_means="The profile states facts under which the applicant would tick this option.",
                        false_means="The profile rules this option out, or says nothing that bears on it.",
                    )
                    per_option.append((question_id, option.label))
                multi[request_id] = per_option
                continue

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

        decision = self._decide(
            state, questions, description="answer {} question(s) from the profile".format(len(questions))
        )

        self.calls += 1
        self.cost_usd += decision.cost_usd
        self.questions_asked += len(questions)

        out: Dict[str, Tuple[Optional[object], float]] = {}

        for request_id, _field in batch:
            if request_id in multi:
                picked: List[str] = []
                decidedness = 1.0
                for question_id, label in multi[request_id]:
                    answer = decision.get(question_id)
                    p = answer.belief() if answer is not None else 0.5
                    if p >= MULTI_YES:
                        picked.append(label)
                    # Below MULTI_NO is a confident no; in between, undecided.
                    # Either way the field's confidence is its least-decided option.
                    decidedness = min(decidedness, max(p, 1.0 - p))
                if not picked:
                    picked, decidedness = self._none_by_elimination(multi[request_id], decision)
                # An empty selection is not an answer to a required question;
                # report it as undecided so the field stays with the applicant.
                out[request_id] = (picked or None, decidedness if picked else 0.0)
                continue

            answer = decision.get(request_id)
            if answer is None or not answer.choice or answer.choice == "unsure":
                out[request_id] = (None, answer.belief() if answer else 0.0)
                continue
            out[request_id] = (option_labels[request_id].get(answer.choice), answer.belief())

        return out

    @staticmethod
    def _none_by_elimination(per_option, decision):
        """The list's single none-option, when every other option is a confident no."""
        none_options = [(q, label) for q, label in per_option if _NONE_OPTION.search(label)]
        if len(none_options) != 1:
            return [], 0.0
        weakest_no = 1.0
        for question_id, label in per_option:
            if (question_id, label) == none_options[0]:
                continue
            answer = decision.get(question_id)
            p = answer.belief() if answer is not None else 0.5
            if p > MULTI_NO:
                return [], 0.0
            weakest_no = min(weakest_no, 1.0 - p)
        return [none_options[0][1]], weakest_no

    def second_pass(
        self, state: Dict[str, object], requests: List[Tuple[str, FormField, Dict[str, str]]]
    ) -> Dict[str, Tuple[Optional[str], float, float]]:
        """For a free-text question nothing matched: is it covered, and by which fact?

        Victor, 2026-10-01: "compile the questions we couldn't answer, decide
        yes or no whether we have the information, and answer it as well."
        Two bounded questions per field, from the profile alone: a Noul —
        does the profile contain the specific information this question asks
        for — and a Choice over the profile's own keys naming the fact that
        answers it. The answer written is the stored value of that key, never
        text the model composed. Essays, protected and consent fields never
        arrive here (the caller's eligibility rule).

        Returns request_id -> (profile key or None, covered probability,
        key-choice confidence).
        """
        if not requests:
            return {}
        state_hash = hashlib.sha1(
            json.dumps(state.get("profile", {}), sort_keys=True, default=str).encode()
        ).hexdigest()[:12]
        out: Dict[str, Tuple[Optional[str], float, float]] = {}
        pending = []
        for request_id, field, catalog in requests:
            signature = "second::{}::{}".format(field.label[:100], state_hash)
            cached = self._options.get(signature)
            if cached is not None:
                key, packed = cached
                out[request_id] = (key, packed, packed)
                continue
            pending.append((request_id, field, catalog, signature))
        for start in range(0, len(pending), max(1, MAX_QUESTIONS_PER_CALL // 2)):
            batch = pending[start:start + max(1, MAX_QUESTIONS_PER_CALL // 2)]
            questions = {}
            for request_id, field, catalog, _sig in batch:
                questions[request_id + "__cov"] = noul(
                    _COVERED_INSTRUCTIONS + "\n\nQuestion on the form: " + field.label,
                    true_means="state.profile holds the specific fact this question asks for, stated plainly.",
                    false_means="The question asks for something the profile does not state, or only hints at.",
                )
                criteria = {key: "{}: {}".format(key, value[:80]) for key, value in catalog.items()}
                criteria["none"] = "No single profile entry answers this question."
                questions[request_id + "__key"] = choice(
                    _KEY_INSTRUCTIONS + "\n\nQuestion on the form: " + field.label, criteria,
                )
            decision = self._decide(state, questions, description="second pass over {} text question(s)".format(len(batch)))
            self.calls += 1
            self.cost_usd += decision.cost_usd
            self.questions_asked += len(questions)
            for request_id, field, catalog, signature in batch:
                cov = decision.get(request_id + "__cov")
                picked = decision.get(request_id + "__key")
                p_cov = cov.belief() if cov is not None else 0.0
                key = picked.choice if picked is not None and picked.choice and picked.choice != "none" else None
                p_key = picked.confidence if picked is not None and key else 0.0
                if key is not None and key not in catalog:
                    key, p_key = None, 0.0
                out[request_id] = (key, p_cov, p_key)
                self._options.put(signature, key, min(p_cov, p_key) if key else p_cov)
        return out

    def judge_observations(
        self, state: Dict[str, object], items: List[Tuple[str, str, str, Dict[str, str]]]
    ) -> Dict[str, Tuple[float, Optional[str], float]]:
        """Is a correction the applicant made a stable fact, and where does it live?

        The semantic step of the learning loop (§50). For each (id, question,
        answer, catalog): a Noul — would this answer be the same on any
        employer's form, i.e. a fact or standing preference about the
        applicant rather than something about this company, role or moment —
        and a Choice over the profile's own keys plus "new" naming where it
        belongs. Reflexion-style: the applicant's edit is the verbal feedback;
        this turns it into a candidate for memory rather than a one-off.

        Returns id -> (stable probability, key or "new" or None, key confidence).
        """
        if not items:
            return {}
        out: Dict[str, Tuple[float, Optional[str], float]] = {}
        step = max(1, MAX_QUESTIONS_PER_CALL // 2)
        for start in range(0, len(items), step):
            batch = items[start:start + step]
            questions = {}
            for item_id, question, answer, catalog in batch:
                questions[item_id + "__stable"] = noul(
                    _STABLE_INSTRUCTIONS + "\n\nQuestion on the form: {}\nThe applicant's answer: {}".format(question, answer),
                    true_means="A fact or standing preference about the applicant; the same answer belongs on any employer's form asking this.",
                    false_means="Specific to this company, role, posting or moment, or free prose, or a one-off.",
                )
                criteria = {key: "{}: {}".format(key, (value or "(empty)")[:60]) for key, value in catalog.items()}
                criteria["new"] = "None of these keys; this is a new kind of fact about the applicant."
                questions[item_id + "__key"] = choice(
                    _WHERE_INSTRUCTIONS + "\n\nQuestion on the form: {}\nThe applicant's answer: {}".format(question, answer),
                    criteria,
                )
            decision = self._decide(state, questions, description="judge {} correction(s) for memory".format(len(batch)))
            self.calls += 1
            self.cost_usd += decision.cost_usd
            self.questions_asked += len(questions)
            for item_id, _q, _a, catalog in batch:
                stable = decision.get(item_id + "__stable")
                where = decision.get(item_id + "__key")
                p_stable = stable.belief() if stable is not None else 0.0
                key = where.choice if where is not None and where.choice and where.choice != "unsure" else None
                p_key = where.confidence if where is not None and key else 0.0
                if key is not None and key != "new" and key not in catalog:
                    key, p_key = None, 0.0
                out[item_id] = (p_stable, key, p_key)
        return out

    def verify_values(
        self, state: Dict[str, object], items: List[Tuple[str, str, str]]
    ) -> Dict[str, float]:
        """Is writing this stored value into this field a truthful, direct answer?"""
        if not items:
            return {}
        questions = {
            request_id: noul(
                _VERIFY_INSTRUCTIONS + "\n\nQuestion on the form: {}\nProposed answer: {}".format(label, value),
                true_means="The proposed answer is exactly what this applicant would truthfully write here.",
                false_means="The answer is off-topic, only partly responsive, in the wrong form, or not supported by the profile.",
            )
            for request_id, label, value in items
        }
        decision = self._decide(state, questions, description="verify {} second-pass answer(s)".format(len(items)))
        self.calls += 1
        self.cost_usd += decision.cost_usd
        self.questions_asked += len(questions)
        return {request_id: (decision.get(request_id).belief() if decision.get(request_id) is not None else 0.0)
                for request_id, _l, _v in items}

    def _decide(self, state, questions, description):
        """One call, behind a circuit breaker.

        A model that has just timed out will time out again within the same
        plan; paying the budget three times (theme, options, answers) turned a
        10 ms deterministic plan into a 20 s wait. After the first transport
        failure the remaining gates are skipped and the plan is finished
        deterministically; the response says so.
        """
        if self.tripped:
            raise JevServiceError("model skipped: an earlier call in this plan timed out")
        try:
            return self._ensure_client().decide(state, questions, description=description)
        except JevServiceError as e:
            if "timed out" in str(e) or "transport" in str(e):
                self.tripped = True
            raise

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

        decision = self._decide(
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

        decision = self._decide(
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
