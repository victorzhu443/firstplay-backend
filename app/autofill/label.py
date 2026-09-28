"""
Label the filled fields:  python -m app.autofill.label

Walks the frozen corpus, shows each field the resolver filled, and asks whether
it is right. Precision cannot be computed any other way — whether a 3.8 GPA
belongs in "3.6 or above (out of 4.0)" is a fact about you, not about the code.

Incremental on purpose: already-labelled fields are skipped, so this can be done
in short sittings, and a label survives code changes because it is keyed on the
posting and field rather than on the value. If the resolver later produces a
*different* value for a field, the old label no longer applies and the field is
offered again.
"""
import sys
from typing import Dict, Optional

from dotenv import load_dotenv

load_dotenv()

from app.autofill.binder import resolve_form  # noqa: E402
from app.autofill.evaluate import (  # noqa: E402
    NOT_COVERABLE,
    format_score,
    label_key,
    load_corpus,
    load_labels,
    save_labels,
    score,
)
from app.autofill.jev_binder import JevBinder, ThemeCache  # noqa: E402
from app.autofill.memory import load_memory  # noqa: E402
from app.autofill.schema import FillSource  # noqa: E402

PROMPT = "    [y]es correct  [n]o wrong  [a]sked? shouldn't have filled  [s]kip  [q]uit > "


def main(limit: Optional[int] = None) -> int:
    forms = load_corpus()

    if not forms:
        print("No frozen corpus. Run:  python -m app.autofill.freeze")
        return 1

    memory = load_memory()
    labels = load_labels()
    binder = JevBinder(cache=ThemeCache(
        __import__("os").path.join(__import__("os").path.expanduser("~"),
                                   ".config", "firstplay", "themes.json")))

    print()
    print("Labelling filled fields for precision.")
    print("Only fields the resolver FILLED are shown — a field left for review")
    print("cannot be wrong, it can only be a coverage miss.")
    print()

    done = 0

    for form in forms:
        classes = {f.key: f.field_class for f in form.fields}
        plan = resolve_form(form, memory, binder=binder)
        pending = []

        for entry in plan.entries:
            if classes.get(entry.field_key) in NOT_COVERABLE:
                continue
            if entry.value is None or entry.needs_review:
                continue
            if label_key(form.posting_id, entry.field_key, entry) in labels:
                continue
            pending.append(entry)

        # Model-decided values first. Precision risk lives entirely here: a
        # value read out of the profile is right unless the profile is wrong,
        # whereas a matched one is a judgement that can miss. Labelling 279
        # memory fills and 2 model fills produced a 100% number that measured
        # almost nothing.
        pending.sort(key=lambda e: 0 if e.source == FillSource.MODEL_DECISION else 1)

        if not pending:
            continue

        print("=" * 70)
        print("{} — {}".format(form.company or "?", (form.title or "?")[:52]))
        print("=" * 70)

        for entry in pending:
            print()
            print("  Q: {}".format(entry.label[:66]))
            print("  A: {}".format(str(entry.value)[:66]))
            print("     (source: {}{})".format(
                entry.source.value,
                ", " + entry.reason if entry.reason else ""))

            try:
                reply = input(PROMPT).strip().lower()
            except (EOFError, KeyboardInterrupt):
                reply = "q"

            if reply == "q":
                path = save_labels(labels)
                print("\nsaved {} labels to {}".format(len(labels), path))
                print()
                print(format_score(score(forms, memory, labels, binder=binder)))
                return 0

            verdict = {"y": "correct", "n": "wrong", "a": "should_have_asked"}.get(reply)

            if verdict is None:
                continue

            # Confirm the consequential verdicts only. Marking a field wrong is
            # what drives engineering priorities, and a session produced 7
            # mis-keyed "wrong"s out of 428 — a 1.6% labelling error rate, the
            # same order as the precision being measured. One of them nearly
            # bought a feature nobody wanted. "Correct" stays a single keystroke
            # because it is the common case and a stray 'y' costs far less.
            if verdict in ("wrong", "should_have_asked"):
                word = "WRONG" if verdict == "wrong" else "SHOULD NOT HAVE FILLED"
                try:
                    confirm = input("    marking {} — sure? [y/N] > ".format(word))
                except (EOFError, KeyboardInterrupt):
                    confirm = "n"
                if confirm.strip().lower() not in ("y", "yes"):
                    print("    not recorded")
                    continue

            labels[label_key(form.posting_id, entry.field_key, entry)] = verdict
            done += 1

            if limit and done >= limit:
                save_labels(labels)
                print("\nreached limit of {}".format(limit))
                print(format_score(score(forms, memory, labels, binder=binder)))
                return 0

    path = save_labels(labels)
    print()
    print("all filled fields labelled. saved {} labels to {}".format(len(labels), path))
    print()
    print(format_score(score(forms, memory, labels, binder=binder)))

    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else None))
