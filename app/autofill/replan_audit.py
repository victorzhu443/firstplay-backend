"""
Re-plan a held-out draw through the running backend and dump what the next
round needs:  python -m app.autofill.replan_audit --draws <survey/draws dir> --out <dir>

  * `unanswered.json` — every entry still for review, with label, kind, reason;
    clustered by normalised label in `unanswered_clusters.json`;
  * `model_decisions.jsonl` — every value a model produced (source
    model_decision / model_draft, or a second-pass fill), with its confidence
    and the options offered. This is the audit list: a human marks each row
    correct / wrong / unsure, and `tune.py` sweeps the acceptance thresholds
    against those verdicts (the reward) rather than against intuition.

**Every output carries the owner's answers.** `unanswered.json` holds each
plan entry's `value`/`values`, `model_decisions.jsonl` is nothing but values
the model chose for this applicant, and the profile they came from is the
real one. The three files are written owner-only (0600, atomic, via
`private_json`); keep them under `~/.config/firstplay`, never in a
repository or a shared drive (audit 2026-10-12, F5).
"""
import argparse
import collections
import json
import os
import re
import sys
import urllib.request

from app.autofill.private_json import private_file, write_private_json

ASHBY_CORPUS = os.path.expanduser("~/.config/firstplay/corpus-ashby")
KEEP = ("field_key", "label", "value", "values", "needs_review", "skipped", "satisfied_by",
        "attach", "reason", "theme", "required", "source", "confidence")


def plan(backend, ats, form, profile, source, attempts=6):
    """One plan; waits out the backend's own per-client rate limit (429)."""
    import time
    import urllib.error
    body = json.dumps({"ats": ats, "form": form, "profile": profile, "source": source}).encode()
    for attempt in range(attempts):
        req = urllib.request.Request(backend + "/api/autofill/plan", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            return json.load(urllib.request.urlopen(req, timeout=180))
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == attempts - 1:
                raise
            time.sleep(float(e.headers.get("Retry-After") or 10) + 1)


def norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())).strip()[:70]


def load_draws(directory):
    ashby, greenhouse = [], []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        rows = json.load(open(os.path.join(directory, name)))
        for r in rows:
            if isinstance(r, dict) and r.get("org") and r.get("id"):
                ashby.append((str(r["org"]), str(r["id"])))
            elif isinstance(r, dict) and r.get("board") and r.get("id"):
                greenhouse.append((str(r["board"]), str(r["id"])))
            elif isinstance(r, (list, tuple)) and len(r) >= 2:
                (ashby if "-" in str(r[1]) else greenhouse).append((str(r[0]), str(r[1])))
    return sorted(set(ashby)), sorted(set(greenhouse))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--backend", default="http://127.0.0.1:8000")
    ap.add_argument("--profile", default=os.path.expanduser("~/.config/firstplay/profile.json"))
    ap.add_argument("--only", choices=["ashby", "greenhouse", "both"], default="both")
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    profile = json.load(open(args.profile))
    ashby, greenhouse = load_draws(args.draws)
    rows, errors = [], []

    skipped = []

    def take(ats, company, posting, r):
        # A plan the model never reached is a different measurement; it is
        # recorded per form so a run with an expired key or a flaky model is
        # not mistaken for an engine regression (2026-10-11: 125 errors and
        # two model decisions across 500 forms — the OpenRouter key had expired).
        if r.get("model_skipped") or not r.get("jev_calls"):
            skipped.append((ats, company, posting))
        for e in r["plan"]["entries"]:
            rows.append({"ats": ats, "company": company, "posting": posting, "model_skipped": bool(r.get("model_skipped")),
                         "jev_calls": r.get("jev_calls", 0), **{k: e.get(k) for k in KEEP}})

    if args.only in ("ashby", "both"):
        for org, jid in ashby:
            p = os.path.join(ASHBY_CORPUS, "ab_{}.json".format(jid))
            if not os.path.exists(p):
                errors.append((org, "not frozen")); continue
            try:
                take("ashby", org, jid, plan(args.backend, "ashby", json.load(open(p)), profile,
                                             "https://jobs.ashbyhq.com/{}/{}/application".format(org, jid)))
            except Exception as e:  # noqa: BLE001
                errors.append((org, str(e)[:100]))
    if args.only in ("greenhouse", "both"):
        for board, jid in greenhouse:
            try:
                payload = json.load(urllib.request.urlopen(
                    "https://boards-api.greenhouse.io/v1/boards/{}/jobs/{}?questions=true".format(board, jid), timeout=20))
                take("greenhouse", board, jid, plan(args.backend, "greenhouse", payload, profile,
                                                    "https://job-boards.greenhouse.io/{}/jobs/{}".format(board, jid)))
            except Exception as e:  # noqa: BLE001
                errors.append((board + "/" + jid, str(e)[:100]))

    un = [r for r in rows if r["needs_review"] and not r["skipped"] and not r["satisfied_by"] and not r["attach"]]
    by = collections.defaultdict(list)
    for r in un:
        by[(r["ats"], norm(r["label"]))].append(r)
    clusters = sorted(({"ats": k[0], "label": v[0]["label"][:100], "count": len(v),
                        "required": sum(1 for x in v if x["required"]),
                        "reasons": collections.Counter((x["reason"] or "")[:60] for x in v).most_common(2),
                        "companies": sorted({x["company"] for x in v})[:6]}
                       for k, v in by.items()), key=lambda c: -c["count"])
    decided = [r for r in rows if (r.get("source") in ("model_decision", "model_draft")
                                   or str(r.get("theme") or "").startswith("profile."))
               and (r.get("value") is not None or r.get("values"))]
    write_private_json(os.path.join(args.out, "unanswered.json"), {"rows": rows, "errors": errors}, indent=0, default=str)
    write_private_json(os.path.join(args.out, "unanswered_clusters.json"), clusters, indent=1)
    with private_file(os.path.join(args.out, "model_decisions.jsonl")) as h:
        for r in decided:
            h.write(json.dumps({**r, "verdict": None}, default=str) + "\n")
    tot = collections.Counter(r["ats"] for r in rows)
    forms = len({(r["ats"], r["posting"]) for r in rows})
    print("forms", forms, "entries", dict(tot), "unanswered", dict(collections.Counter(r["ats"] for r in un)),
          "model-decided", len(decided), "errors", len(errors), "forms without a model call", len(skipped))
    if skipped and len(skipped) >= forms // 2:
        print("WARNING: the model was not reached on most forms; check /api/autofill/health model_last_error before reading the numbers")
    for c in clusters[:25]:
        print("{:3d}x {:3d}req [{}] {:70s} | {}".format(c["count"], c["required"], c["ats"][:2], c["label"][:70], c["reasons"][0][0][:50]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
