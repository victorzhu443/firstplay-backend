"""
Threshold tuning against audited verdicts:  python -m app.autofill.tune <model_decisions.jsonl>

The engine accepts a model decision when its confidence clears a threshold
(AUTOFILL_CONFIDENCE for the gate, SECOND_PASS_* for the second pass). Those
numbers were chosen by cost asymmetry, not measured. Once a human has marked
each model-decided row in `model_decisions.jsonl` as correct / wrong / unsure
(the reward), this sweeps the threshold and prints, per candidate value, how
many decisions would be accepted, how many of those are correct (precision),
and how many correct ones are given up — so the threshold is the one that
meets the precision target with the most coverage, and the choice is a
recorded measurement rather than a guess. Rows without a verdict are reported
and excluded.
"""
import collections
import json
import sys


def sweep(rows, thresholds=(0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95), precision_target=0.99):
    judged = [r for r in rows if r.get("verdict") in ("correct", "wrong")]
    out = []
    for t in thresholds:
        accepted = [r for r in judged if float(r.get("confidence") or 0) >= t]
        correct = sum(1 for r in accepted if r["verdict"] == "correct")
        wrong = len(accepted) - correct
        total_correct = sum(1 for r in judged if r["verdict"] == "correct")
        precision = correct / len(accepted) if accepted else 1.0
        out.append({"threshold": t, "accepted": len(accepted), "correct": correct, "wrong": wrong,
                    "precision": precision, "recall_of_correct": (correct / total_correct) if total_correct else 1.0,
                    "meets_target": precision >= precision_target})
    return out, len(judged), len(rows) - len(judged)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__); return 2
    rows = [json.loads(line) for line in open(argv[0]) if line.strip()]
    by_source = collections.defaultdict(list)
    for r in rows:
        src = "second_pass" if str(r.get("theme") or "").startswith("profile.") else (r.get("source") or "?")
        by_source[src].append(r)
    for src, group in by_source.items():
        table, judged, unjudged = sweep(group)
        print("\n== {}: {} rows, {} judged, {} awaiting a verdict".format(src, len(group), judged, unjudged))
        print("  thr   accepted  correct  wrong  precision  recall  target")
        for row in table:
            print("  {threshold:.2f}  {accepted:8d}  {correct:7d}  {wrong:5d}  {precision:9.3f}  {recall_of_correct:6.3f}  {mark}".format(
                mark="PASS" if row["meets_target"] else "----", **row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
