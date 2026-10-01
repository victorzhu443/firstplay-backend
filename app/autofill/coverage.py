"""
Coverage, offline and repeatable:  python -m app.autofill.coverage [--model] [--out report.json]

The target is "everything except the essays". This runs the engine over
the frozen corpora — Greenhouse (`~/.config/firstplay/corpus-large`, deduped
on question set) and Ashby (`~/.config/firstplay/corpus-ashby`, the
ApiJobPosting definitions) — with the applicant's real profile, and reports:

  * coverage per ATS: filled / (fields − essays − files), where an essay is
    a LONG_TEXT or NARRATIVE field;
  * every uncovered non-essay field, grouped by normalised label, with its
    kind, options, class and the plan's reason — the list a round starts
    from: each cluster is either a missing fact (ask the applicant), a
    missing alias/theme (code), a guard, or a policy decision.

Train on this, test on fresh boards through the extension (DECISIONS §48).
"""
import argparse
import collections
import json
import os
import re
import sys
import time

from app.autofill.ashby_api import parse_ashby_posting
from app.autofill.binder import resolve_form
from app.autofill.classify import normalize_label
from app.autofill.evaluate import load_corpus
from app.autofill.memory import load_memory
from app.autofill.schema import FieldClass, FieldKind

ASHBY_DIR = os.path.expanduser("~/.config/firstplay/corpus-ashby")

ESSAY = {FieldKind.LONG_TEXT}


def load_ashby(directory=ASHBY_DIR):
    forms = []
    if not os.path.isdir(directory):
        return forms
    for name in sorted(os.listdir(directory)):
        if not name.startswith("ab_") or not name.endswith(".json"):
            continue
        with open(os.path.join(directory, name)) as handle:
            payload = json.load(handle)
        try:
            forms.append(parse_ashby_posting(payload, org=payload.get("_org")))
        except Exception:  # noqa: BLE001 — a malformed capture is not a measurement
            continue
    return forms


def is_essay(field):
    return field.kind in ESSAY or field.field_class == FieldClass.NARRATIVE


def run(forms, memory, binder, ats):
    fields = {f.key: f for form in forms for f in form.fields}  # last wins; keys repeat across forms only by accident
    totals = collections.Counter()
    gaps = []
    errors = []
    cost = 0.0
    for form in forms:
        by_key = {f.key: f for f in form.fields}
        try:
            plan = resolve_form(form, memory, binder=binder)
        except Exception as e:  # noqa: BLE001
            totals["form_errors"] += 1
            errors.append({"ats": ats, "posting": form.posting_id, "company": form.company,
                           "error": "{}: {}".format(type(e).__name__, str(e)[:160])})
            continue
        for entry in plan.entries:
            field = by_key.get(entry.field_key)
            if field is None:
                continue
            totals["fields"] += 1
            if is_essay(field):
                totals["essays"] += 1
                continue
            if field.kind == FieldKind.FILE:
                totals["files"] += 1
                continue
            totals["scope"] += 1
            filled = (entry.value is not None or entry.values) and not entry.needs_review and not entry.skipped
            if filled or entry.satisfied_by or entry.attach:
                totals["filled"] += 1
                continue
            if entry.skipped:
                # A stated decision to leave it blank (skip list, nothing recorded
                # for an optional secondary fact, an untriggered follow-up).
                totals["blank_by_choice"] += 1
                totals["filled"] += 1
                continue
            totals["gap"] += 1
            gaps.append({
                "ats": ats, "company": form.company, "posting": form.posting_id,
                "key": field.key, "label": field.label, "norm": normalize_label(field.label),
                "kind": field.kind.value, "class": field.field_class.value, "required": field.required,
                "options": [o.label for o in field.options][:12], "n_options": len(field.options),
                "reason": entry.reason, "theme": entry.theme, "suggested": entry.values or entry.value,
                "confidence": entry.confidence,
            })
    if binder is not None:
        cost = getattr(binder, "cost_usd", 0.0)
    return totals, gaps, cost, errors


def clusters(gaps, top=60):
    by = collections.defaultdict(list)
    for g in gaps:
        by[(g["norm"][:70], g["kind"], g["class"])].append(g)
    rows = []
    for (norm, kind, klass), items in by.items():
        rows.append({
            "label": items[0]["label"][:90], "norm": norm, "kind": kind, "class": klass,
            "count": len(items), "forms": len({i["posting"] for i in items}),
            "required": sum(1 for i in items if i["required"]),
            "reasons": collections.Counter((i["reason"] or "")[:60] for i in items).most_common(2),
            "options": items[0]["options"][:8],
            "ats": collections.Counter(i["ats"] for i in items),
        })
    rows.sort(key=lambda r: -r["count"])
    return rows[:top]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="store_true", help="use the Jev binder (costs a few cents)")
    parser.add_argument("--out", default=os.path.expanduser("~/.config/firstplay/coverage.json"))
    parser.add_argument("--ats", choices=["greenhouse", "ashby", "both"], default="both")
    parser.add_argument("--profile", default=None, help="a profile other than the applicant's (e.g. a ceiling copy)")
    args = parser.parse_args(argv)

    memory = load_memory(args.profile)
    binder = None
    if args.model:
        from app.autofill.jev_binder import JevBinder
        from app.routers.autofill import _OPTION_CACHE, _THEME_CACHE
        binder = JevBinder(cache=_THEME_CACHE, option_cache=_OPTION_CACHE)
    else:
        from app.autofill.binder import DeterministicBinder
        binder = DeterministicBinder()

    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": args.model, "ats": {}}
    all_gaps = []
    started = time.perf_counter()
    if args.ats in ("greenhouse", "both"):
        forms = load_corpus(os.path.expanduser("~/.config/firstplay/corpus-large"))
        totals, gaps, cost, errors = run(forms, memory, binder, "greenhouse")
        report["ats"]["greenhouse"] = {"forms": len(forms), **totals, "coverage": round(totals["filled"] / max(1, totals["scope"]), 4), "cost_usd": cost, "errors": errors}
        all_gaps += gaps
    if args.ats in ("ashby", "both"):
        forms = load_ashby()
        totals, gaps, cost, errors = run(forms, memory, binder, "ashby")
        report["ats"]["ashby"] = {"forms": len(forms), **totals, "coverage": round(totals["filled"] / max(1, totals["scope"]), 4), "cost_usd": cost, "errors": errors}
        all_gaps += gaps
    report["seconds"] = round(time.perf_counter() - started, 1)
    report["clusters"] = clusters(all_gaps)
    report["gaps"] = all_gaps
    with open(args.out, "w") as handle:
        json.dump(report, handle, indent=1, default=str)

    for ats, t in report["ats"].items():
        if t.get("errors"):
            kinds = collections.Counter(e["error"][:80] for e in t["errors"])
            print("{:10s} {} forms raised: {}".format(ats, len(t["errors"]), kinds.most_common(3)))
        print("{:10s} forms {:4d}  fields {:5d}  essays {:4d}  files {:3d}  in scope {:5d}  filled {:5d} (blank by choice {:4d})  gaps {:5d}  coverage {:.1%}  ${:.3f}".format(
            ats, t["forms"], t["fields"], t["essays"], t["files"], t["scope"], t["filled"], t.get("blank_by_choice", 0), t["gap"], t["coverage"], t["cost_usd"]))
    print("top gap clusters:")
    for r in report["clusters"][:40]:
        print("  {:4d}x {:3d}req [{:12s}] {:62s} {}".format(r["count"], r["required"], r["kind"], r["label"][:62], (r["options"][:4] if r["options"] else r["reasons"][0][0][:40])))
    print("report:", args.out, "in", report["seconds"], "s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
