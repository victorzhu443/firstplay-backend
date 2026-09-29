"""
Run the engine over the large frozen corpus and collect every decision.

    python -m app.autofill.scale [output-dir]

Input: `gh_interns.json` in the output dir — `{"interns": [[board, job_id,
title], ...]}` as produced from a listings crawl. Freezes each posting (with
the board's education vocabularies attached, exactly as the extension does)
into ~/.config/firstplay/corpus-large, dedupes on question set, then runs a
deterministic pass and a model pass, writing `scale_deterministic.json` and
`scale_model.json`: one row per distinct (question, answer) with how many
forms it covered — the unit a human scores. See docs/DECISIONS.md §26–27.
"""
import json, os, sys, time, re, pathlib, collections, urllib.request, concurrent.futures as cf, hashlib

from app.autofill.evaluate import freeze_corpus, load_corpus, form_signature
from app.autofill.memory import load_memory
from app.autofill.binder import resolve_form
from app.autofill.jev_binder import JevBinder, ThemeCache
from app.autofill.schema import FieldClass, FillSource
from app.autofill.format import normalize_value

SCR = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.config/firstplay/scale")); SCR.mkdir(parents=True, exist_ok=True); LARGE = os.path.expanduser("~/.config/firstplay/corpus-large")
log = open(SCR / "scale_run.log", "a")
def say(*a):
    print(*a, file=log, flush=True)

interns = json.load(open(SCR / "gh_interns.json"))["interns"]
say(f"[{time.strftime('%H:%M:%S')}] freezing {len(interns)} postings")
def fetch(t):
    board, jid, title = t
    try:
        req = urllib.request.Request(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}?questions=true", headers={"User-Agent": "Mozilla/5.0"})
        j = json.load(urllib.request.urlopen(req, timeout=20)); j["_board"] = board; return j
    except Exception as e:
        return None
if not os.path.isdir(LARGE) or len(os.listdir(LARGE)) < 100:
    with cf.ThreadPoolExecutor(16) as ex: payloads = [p for p in ex.map(fetch, interns) if p]
    # education options per board, attached like the extension does
    boards = {p["_board"] for p in payloads if p.get("education")}
    opts = {}
    def edu(b):
        try:
            return b, {k: json.load(urllib.request.urlopen(f"https://boards-api.greenhouse.io/v1/boards/{b}/education/{k}", timeout=20)).get("items", []) for k in ("degrees", "disciplines")}
        except Exception: return b, None
    with cf.ThreadPoolExecutor(8) as ex:
        for b, o in ex.map(edu, boards):
            if o: opts[b] = o
    for p in payloads:
        if p.get("education") and p["_board"] in opts: p["education_options"] = opts[p["_board"]]
    written = freeze_corpus(payloads, LARGE)
    say(f"[{time.strftime('%H:%M:%S')}] froze {written} payloads ({len(payloads)} fetched)")

forms = load_corpus(LARGE)  # deduplicated on form signature
say(f"[{time.strftime('%H:%M:%S')}] {len(forms)} unique forms after dedupe")
memory = load_memory()

def run(binder, tag):
    decisions = collections.OrderedDict(); per_form = []; reviews = collections.Counter(); classes = collections.Counter()
    t0 = time.time()
    for i, form in enumerate(forms):
        try:
            plan = resolve_form(form, memory, binder=binder, company=form.company)
        except Exception as e:
            say(f"  ! {form.company} {form.posting_id}: {type(e).__name__}: {e}"); continue
        fields = {f.key: f for f in form.fields}
        filled = review = 0
        for e in plan.entries:
            f = fields.get(e.field_key)
            if f is None or e.skipped or e.satisfied_by or e.attach: continue
            if (e.value is not None or e.values) and not e.needs_review:
                filled += 1
                value = " + ".join(e.values) if e.values else str(e.value)
                key = hashlib.sha1((normalize_value(f.label) + "||" + normalize_value(value) + "||" + str(e.source)).encode()).hexdigest()[:12]
                d = decisions.get(key)
                if d is None:
                    d = decisions[key] = {"id": key, "question": f.label[:200], "answer": value[:200], "source": str(e.source).split(".")[-1].lower(),
                                          "field_class": str(f.field_class).split(".")[-1].lower(), "reason": (e.reason or "")[:120],
                                          "confidence": round(e.confidence, 2), "options": [o.label for o in f.options][:12],
                                          "count": 0, "companies": [], "postings": []}
                d["count"] += 1
                if form.company and form.company not in d["companies"] and len(d["companies"]) < 8: d["companies"].append(form.company)
                if len(d["postings"]) < 3: d["postings"].append(str(form.posting_id))
                d["confidence"] = min(d["confidence"], round(e.confidence, 2))
            elif e.needs_review:
                review += 1; reviews[(e.reason or "")[:60]] += 1
            classes[str(f.field_class).split(".")[-1].lower()] += 1
        per_form.append({"company": form.company, "posting": str(form.posting_id), "title": form.title, "filled": filled, "review": review})
    say(f"[{time.strftime('%H:%M:%S')}] {tag}: {len(per_form)} forms in {time.time()-t0:.0f}s | distinct decisions {len(decisions)} | fills {sum(d['count'] for d in decisions.values())} | reviews {sum(reviews.values())}")
    return decisions, per_form, reviews

det, pf_det, rev_det = run(None, "deterministic")
json.dump({"decisions": list(det.values()), "per_form": pf_det, "reviews": rev_det.most_common(40)}, open(SCR / "scale_deterministic.json", "w"))

binder = JevBinder(cache=ThemeCache(os.path.expanduser("~/.config/firstplay/themes.json")))
mod, pf_mod, rev_mod = run(binder, "with model")
binder.save_caches()
say(f"[{time.strftime('%H:%M:%S')}] jev calls {binder.calls} questions {binder.questions_asked} cost ${binder.cost_usd:.4f}")
json.dump({"decisions": list(mod.values()), "per_form": pf_mod, "reviews": rev_mod.most_common(40),
           "jev": {"calls": binder.calls, "cost_usd": round(binder.cost_usd, 4)}}, open(SCR / "scale_model.json", "w"))
say("DONE")
