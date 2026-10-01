"""
Does learning from one form answer the next? Offline, on the frozen corpora.

    python -m app.autofill.learn_eval [--ats greenhouse|ashby|both] [--model] [--limit N]

Simulates an applicant who, on each form in corpus order, answers every
SCREENING text/select question the deterministic plan left for review with a
fixed value, and feeds those answers to `learn`. Reports how often a question
seen on an earlier form is auto-answered on a later one (the replay rate),
with sample sizes. With --model, also counts the proposals the semantic step
would raise (costs a few cents).
"""
import argparse
import collections
import os
import sys

from app.autofill.binder import DeterministicBinder, resolve_form
from app.autofill.coverage import load_ashby
from app.autofill.evaluate import load_corpus
from app.autofill.learn import Observation, learn
from app.autofill.memory import Memory, load_memory
from app.autofill.schema import FieldClass, FieldKind


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--ats", choices=["greenhouse", "ashby", "both"], default="both")
    parser.add_argument("--model", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--profile", default=None)
    args = parser.parse_args(argv)

    forms = []
    if args.ats in ("greenhouse", "both"):
        forms += load_corpus(os.path.expanduser("~/.config/firstplay/corpus-large"))
    if args.ats in ("ashby", "both"):
        forms += load_ashby()
    if args.limit:
        forms = forms[: args.limit]

    memory = load_memory(args.profile)
    binder = None
    if args.model:
        from app.autofill.jev_binder import JevBinder
        binder = JevBinder()

    seen_labels = collections.Counter()
    simulated_labels = set()
    opportunities = replayed = simulated = 0
    proposals = collections.Counter()
    per_form_review_before = []
    for form in forms:
        plan = resolve_form(form, memory, binder=DeterministicBinder())
        by_key = {f.key: f for f in form.fields}
        observations = []
        for entry in plan.entries:
            field = by_key.get(entry.field_key)
            if field is None or field.field_class != FieldClass.SCREENING:
                continue
            if field.kind not in (FieldKind.TEXT, FieldKind.SINGLE_SELECT):
                continue
            fingerprint = Memory.fingerprint(field.label)
            # Only a label the simulated applicant answered on an earlier form
            # counts as an opportunity; only a replay from `answers` counts.
            if fingerprint in simulated_labels:
                opportunities += 1
                if entry.reason == "you answered this before":
                    replayed += 1
            if entry.needs_review and not entry.skipped and not entry.satisfied_by:
                value = field.options[0].label if field.options else "SIM-{}".format(fingerprint[:6])
                observations.append(Observation(ats=form.ats, company=form.company, posting_id=form.posting_id,
                                                field_key=field.key, label=field.label, kind=field.kind.value,
                                                options=[o.label for o in field.options], required=field.required,
                                                plan_value=entry.value, plan_source=str(entry.source), user_value=value))
        per_form_review_before.append(len(observations))
        if observations:
            result = learn(memory, observations, binder=binder)
            simulated += len(observations)
            simulated_labels.update(Memory.fingerprint(o.label) for o in observations)
            for p in result.proposals:
                proposals[p.key] += 1
        for f in form.fields:
            seen_labels[Memory.fingerprint(f.label)] += 1

    print("forms {}  simulated answers {}  later occurrences of a label the applicant answered earlier {}  replayed from answers {}  replay rate {:.1%}".format(
        len(forms), simulated, opportunities, replayed, replayed / max(1, opportunities)))
    print("answers stored {}  distinct proposals {}".format(len(memory.answers), len(proposals)))
    if proposals:
        for key, n in proposals.most_common(15):
            print("  {:3d}x {}".format(n, key))
    return 0


if __name__ == "__main__":
    sys.exit(main())
