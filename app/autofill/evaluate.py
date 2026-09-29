"""
Eval harness: a frozen corpus, human labels, and a score.

Why frozen. Live postings change under you — a company edits a question, a role
closes — so a number measured today against live data cannot be compared with
one measured tomorrow. Every posting is snapshotted to disk and every later run
scores the same inputs, which is the only way to tell a real improvement from
the corpus having shifted.

Why human labels. Coverage is self-evident: the plan either filled a field or it
did not. **Precision is not.** Whether "3.6 or above (out of 4.0)" is the right
bucket for a 3.8 GPA is a fact about the applicant that nothing in the system
knows. So precision requires someone who knows the answers to look, once, and
the labels are then reused forever.

The two metrics and their denominators, kept separate because conflating them
hides which one is failing:

    coverage  = filled / fillable
                fillable excludes NARRATIVE (a separate essay agent's job),
                CONSENT (per-application attestations) and UNKNOWN (questions
                whose text is not readable)

    precision = correct / (correct + wrong)
                over labelled *filled* fields only. A field left for review
                cannot be wrong; it can only be a coverage miss.
"""
import json
import os
from typing import Dict, List, Optional, Tuple

from app.autofill.binder import resolve_form
from app.autofill.greenhouse import parse_greenhouse_job
from app.autofill.memory import Memory
from app.autofill.schema import FieldClass, FormSchema

#: Classes excluded from the coverage denominator. Not failures — they are
#: deliberately out of scope.
NOT_COVERABLE = frozenset({FieldClass.NARRATIVE, FieldClass.CONSENT, FieldClass.UNKNOWN})

CORPUS_DIR = os.path.join(os.path.expanduser("~"), ".config", "firstplay", "corpus")
LABELS_PATH = os.path.join(os.path.expanduser("~"), ".config", "firstplay", "labels.json")

#: What a human can say about a field.
VERDICTS = {
    "y": "correct",          # filled, and the value is right
    "n": "wrong",            # filled, and the value is wrong
    "a": "should_have_asked",  # filled, but it was not safe to guess
    "f": "should_have_filled",  # left for review, but the answer was available
    "o": "correctly_asked",  # left for review, and that was right
}


def freeze_corpus(payloads: List[dict], directory: str = CORPUS_DIR) -> int:
    """Snapshot raw ATS payloads so every later run scores identical inputs."""
    os.makedirs(directory, exist_ok=True)
    written = 0

    for payload in payloads:
        posting_id = str(payload.get("id") or "")
        if not posting_id:
            continue
        with open(os.path.join(directory, "gh_{}.json".format(posting_id)), "w") as handle:
            json.dump(payload, handle)
        written += 1

    return written


def form_signature(form: FormSchema) -> str:
    """Identity of a form's *question set*, ignoring which posting it came from.

    Companies post the same role across several locations, so the corpus holds
    near-duplicates: one Scale AI internship appeared three times with identical
    questions. Duplicates add no information to a measurement and multiply
    labelling effort — the same first name was confirmed four times in one
    40-field session.
    """
    # Labels only. Greenhouse allocates custom-question names per posting
    # (`question_8581808008`), so a signature including them never matches
    # between two copies of the same form — which is why the first version of
    # this found zero duplicates in a corpus that visibly had them.
    return "|".join(sorted(f.label.strip().lower() for f in form.fields if f.label))


def load_corpus(directory: str = CORPUS_DIR, *, deduplicate: bool = True) -> List[FormSchema]:
    """Parse every frozen posting, optionally collapsing identical question sets."""
    if not os.path.isdir(directory):
        return []

    forms = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(directory, name)) as handle:
            forms.append(parse_greenhouse_job(json.load(handle)))

    if not deduplicate:
        return forms

    seen = set()
    unique = []
    for form in forms:
        signature = form_signature(form)
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(form)

    return unique


def load_labels(path: str = LABELS_PATH) -> Dict[str, str]:
    if not os.path.exists(path):
        return {}

    with open(path) as handle:
        return json.load(handle)


def save_labels(labels: Dict[str, str], path: str = LABELS_PATH) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump(labels, handle, indent=1, sort_keys=True)

    return path


def label_key(posting_id: str, field_key: str, entry=None) -> str:
    """Key a label on the *resolution*, not the instance.

    What a human judges is whether a rule is right — "First Name, filled from
    `first_name`, gives Victor" — and that judgement holds on every posting
    asking the same question. Keying per posting made the same person confirm
    their own first name four times in one 40-field session, because the corpus
    contains several postings of the same role.

    The key is question text + value + the path that produced it. All three
    matter: the same question with a different value is a different judgement,
    and so is the same value arrived at differently — "Yes" for US work
    authorisation and "Yes" for UK are the same words and not the same claim.

    Falls back to the old per-instance key when no entry is supplied, so
    existing labels can be migrated rather than discarded.
    """
    if entry is None:
        return "{}::{}".format(posting_id, field_key)

    from app.autofill.classify import normalize_label

    return "{}||{}||{}".format(
        normalize_label(entry.label)[:70],
        str(entry.value)[:50],
        (entry.reason or "")[:50],
    )


def score(
    forms: List[FormSchema],
    memory: Memory,
    labels: Dict[str, str],
    *,
    binder=None,
) -> dict:
    """Coverage, precision and the per-class breakdown.

    Args:
        forms: Frozen corpus
        memory: The applicant's profile
        labels: label_key -> verdict
        binder: Theme classifier, or None

    Returns:
        A dict of metrics; `unlabelled_filled` is how much work is left before
        precision means anything.
    """
    fillable = filled = skipped = attached = 0
    correct = wrong = asked_wrongly = 0
    unlabelled_filled = 0
    missed_but_available = 0
    by_class_gap: Dict[str, int] = {}
    total = 0

    for form in forms:
        classes = {f.key: f.field_class for f in form.fields}
        plan = resolve_form(form, memory, binder=binder)

        for entry in plan.entries:
            total += 1
            field_class = classes.get(entry.field_key, FieldClass.UNKNOWN)

            if field_class in NOT_COVERABLE:
                continue

            # A control answered by its sibling needs no action from anyone, so
            # it is neither a coverage win nor a miss — it leaves the denominator.
            if entry.satisfied_by is not None:
                continue

            fillable += 1

            # Counted as handled, but reported on its own line. A standing
            # decision genuinely means the applicant does not touch the field —
            # yet folding it silently into "filled" would let anyone reach 100%
            # by skipping everything, so the number stays inspectable.
            if entry.skipped is not None:
                skipped += 1
                continue

            # The tool did its part — it knows exactly which file. Only the
            # upload click is left to the applicant, because no script may
            # perform it. Counted as handled and shown separately.
            if entry.attach is not None:
                attached += 1
                continue

            was_filled = entry.value is not None and not entry.needs_review
            verdict = labels.get(label_key(form.posting_id, entry.field_key, entry))

            if was_filled:
                filled += 1
                if verdict == "correct":
                    correct += 1
                elif verdict == "wrong":
                    wrong += 1
                elif verdict == "should_have_asked":
                    asked_wrongly += 1
                else:
                    unlabelled_filled += 1
            else:
                name = field_class.value
                by_class_gap[name] = by_class_gap.get(name, 0) + 1
                if verdict == "should_have_filled":
                    missed_but_available += 1

    judged = correct + wrong + asked_wrongly

    return {
        "postings": len(forms),
        "fields_total": total,
        "fillable": fillable,
        "filled": filled,
        "skipped": skipped,
        "attach": attached,
        "handled": filled + skipped + attached,
        "coverage": ((filled + skipped + attached) / fillable) if fillable else None,
        "labelled_filled": judged,
        "unlabelled_filled": unlabelled_filled,
        "correct": correct,
        "wrong": wrong,
        "should_have_asked": asked_wrongly,
        # Filling something it should have left alone counts against precision:
        # a confident wrong answer and an unsafe guess are the same harm.
        "precision": (correct / judged) if judged else None,
        "missed_but_available": missed_but_available,
        "gap_by_class": by_class_gap,
    }


def format_score(metrics: dict, *, coverage_target=0.95, precision_target=0.99) -> str:
    """One readable block, with an explicit verdict against the targets."""
    lines = []
    coverage = metrics["coverage"]
    precision = metrics["precision"]

    lines.append("corpus     : {} postings, {} fields".format(
        metrics["postings"], metrics["fields_total"]))
    lines.append("fillable   : {} (excludes essays, attestations, unreadable)".format(
        metrics["fillable"]))
    lines.append("")

    mark = lambda ok: "PASS" if ok else "----"
    if coverage is not None:
        lines.append("COVERAGE   : {:.1f}%  ({}/{})   target {:.0f}%  [{}]".format(
            100 * coverage, metrics["handled"], metrics["fillable"],
            100 * coverage_target, mark(coverage >= coverage_target)))
        lines.append("             {} filled from your profile, {} skipped by standing "
                     "decision, {} files to attach yourself".format(
                         metrics["filled"], metrics["skipped"], metrics["attach"]))

    if precision is not None:
        lines.append("PRECISION  : {:.1f}%  ({}/{} labelled)  target {:.0f}%  [{}]".format(
            100 * precision, metrics["correct"], metrics["labelled_filled"],
            100 * precision_target, mark(precision >= precision_target)))
        if metrics["wrong"]:
            lines.append("             {} wrong, {} filled when it should have asked".format(
                metrics["wrong"], metrics["should_have_asked"]))
    else:
        lines.append("PRECISION  : not yet measurable — no filled fields labelled")

    if metrics["unlabelled_filled"]:
        lines.append("")
        lines.append("{} filled fields still unlabelled — precision covers only the rest".format(
            metrics["unlabelled_filled"]))

    if metrics["gap_by_class"]:
        lines.append("")
        lines.append("coverage gap by class:")
        for name, count in sorted(metrics["gap_by_class"].items(), key=lambda kv: -kv[1]):
            lines.append("  {:10} {:4}".format(name, count))

    if metrics["missed_but_available"]:
        lines.append("")
        lines.append("{} fields you marked as answerable but were left for review".format(
            metrics["missed_but_available"]))

    return "\n".join(lines)
