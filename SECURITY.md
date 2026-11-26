# Security notes — firstplay

firstplay reads **untrusted web pages**: the browser extension scrapes a job
posting and the application form off a live page (anyone can serve one), and that
content flows two ways — into model prompts (job-description extraction and
LLM-driven autofill) and through a set of hand-written parsers. That makes
**prompt injection (OWASP LLM01)** the headline risk and **parser robustness** on
scraped input the secondary one. The one network egress the server makes on a
caller's behalf (`/api/job/url`) is the third. Tooling and tests live under
`firstplay-backend/tests/security/`; the shared toolkit is `../security-agents/`
(its design: `security-agents/DESIGN.md`, root causes RC-1 … RC-9).

This is defensive security work on Victor's own code. The `firstplay/` root is
not a git repo (this file is a loose copy; the tracked copy is
`firstplay-backend/SECURITY.md`), `firstplay-backend/` and `firstplay-frontend/`
are each their own git repo.

## Threat model

| Asset | Untrusted channel | Risk | State |
|---|---|---|---|
| job extraction (`JobParsed`) | scraped posting text → `app/chains/job_parser.py` `{job_text}` prompt | injected instructions steer skills/qualifications extraction (LLM01) | **FIXED** — hardened at the sink, directive in the system message, output scrubbed |
| resume extraction (`ResumeParsed`) | uploaded PDF text → `app/chains/resume_parser.py` `{resume_text}` prompt | the same class, on a document the applicant did not necessarily author (LLM01) | **FIXED** — same treatment as `job_parser` |
| autofilled answers | field labels / option text / stored answers → `app/autofill/jev_binder.py` questions | hostile form wording coerces an answer the applicant never gave (LLM01) | **FIXED** — fenced where formatted; every instruction opens with the directive; model choice scrubbed |
| improved resume / project plan (second-order) | parser output of the scraped posting (`job_title`, skill lists) → `resume_improver.py`, `project_generator.py` | posting text that survived extraction steers the rewrite or the plan; the models hold no tools and output is schema-validated, so impact is bounded | **FIXED** — fenced at the format site, directive, output scrubbed |
| internal network / metadata service | caller-chosen URL → `/api/job/url` fetch | server used as a proxy inward (SSRF, incl. a DNS answer that changes between check and connect) | **FIXED** — pinned fetch via `secprim.safe_fetch` |
| autofill endpoint uptime | scraped DOM extract → `app/autofill/ashby.py` `parse_ashby_dom` | malformed node crashes the worker (500) instead of a clean 400 | **FIXED** (2026-11-25) |
| bucket matching | stored answer / option label → `app/autofill/buckets.py` parsers | crash/hang on hostile numeric/date text | fuzzed; degrades safely |
| posting ingest | GraphQL posting JSON → `app/autofill/ashby_api.py` `parse_ashby_posting` | malformed payload | 400 (F11); validators now shared |

## What's been done

### Root-cause fixes (PR `security/root-causes`, stacked on `security/baseline`)

Each item below is one of the shared root causes in `security-agents/DESIGN.md`,
closed by importing the toolkit's one tested implementation rather than keeping a
local copy.

- **RC-4 — pinned, egress-validated fetch (FIXED).** `app/routers/job.py`
  `fetch_html` now calls `secprim.safe_fetch` (from the async handler via
  `run_in_threadpool`): the host is resolved once, every address must be public,
  the connection goes to **that address** with the real `Host` header and SNI, and
  each redirect hop is validated the same way before it is connected to. The body
  is read under `MAX_HTML_BYTES` as it arrives (the header is not trusted). The
  API contract is unchanged: 400 for a non-public destination, 413 over the cap,
  502 for a broken or over-long redirect chain, 504 on timeout, upstream status
  otherwise. `assert_public_url` remains as the fast pre-check for a specific
  message; it is no longer what keeps the fetch safe.
  `tests/security/test_safe_fetch_integration.py` runs the real handler against a
  scripted origin (`tests/fetch_fakes.py` swaps the resolver and transport) and
  checks the origin only ever sees the validated IP literal.
- **RC-1 — boundary validation (FIXED).** `app/autofill/greenhouse.py`'s
  `_as_dict` / `_as_list` / `MalformedPayload` are now `secprim.as_dict` /
  `as_list` / `MalformedInput` under the same names, so the Ashby adapters and the
  router import nothing new. `app/main.py` maps `MalformedInput` to 400 once,
  globally; the per-router `except MalformedPayload` blocks stay for their
  context. **Parser registry rule:** `tests/security/test_fuzz_parsers.py` is the
  list of parsers that touch untrusted input — a new one is added there (and
  must pass `assert_robust`) before it is merged. The registry now fuzzes each
  adapter with a **structural** strategy (nodes of the wrong type, not only
  hostile strings), with `MalformedPayload` the only allowed exception, and
  registers `greenhouse.parse_greenhouse_job` (the primary ATS adapter) and
  `job.extract_job_text` (BeautifulSoup over the fetched page). That run found
  and closed: an Ashby option node given as a bare string reached `.get`; a
  non-string `company_name` reached `.strip()`; `data_compliance` and the
  top-level Greenhouse payload were read without a shape check; and non-string
  `company`/`title`/`apply_url` scalars in either adapter became pydantic errors
  (a 500) rather than a 400. `POST /api/job/description/manual` gained a ceiling
  (`MAX_MANUAL_JD_CHARS`, 200 000) to match its floor.
- **RC-8 — prompt assembly chokepoint (FIXED; closes the "wire the sink
  wrapper" item).** `parse_jd_text` and `parse_resume_text` pass
  `harden_untrusted(...)` into their unchanged LangChain templates, whose system
  messages now end with `promptinject.assemble.DIRECTIVE`. In `jev_binder.py`
  every label, option and stored answer goes through one helper (`_untrusted`) at
  the point it is formatted into a question; because the Jev payload has no
  app-controlled system slot, every `_*_INSTRUCTIONS` constant opens with the
  directive so the fences mean something to the model; every model answer's
  `choice` goes through `scrub_output` in `_decide`, the single seam model output
  crosses. Cache signatures and the labels returned to the caller use the raw
  text, so nothing else changed. **Second-order sinks:** `resume_improver.py` and
  `project_generator.py` interpolate strings that descend from the parsed posting
  (`job_title`, required/preferred/missing skills); these models hold no tools
  and their output is pydantic-validated, so the exposure is bounded, but the
  values are fenced where they are bound and the system messages carry the
  directive anyway. **Output side:** each chain returns its result through
  `app.security.scrub_model` (every string in the pydantic model, nested lists
  included), so `JobParsed`, `ResumeParsed`, the improved resume and the project
  plan are scrubbed before they are stored or returned by any router or pipeline
  node. `tests/security/test_prompt_assembly.py` binds the LLM01 corpus to the
  real job and resume templates (8/8 each; the unhardened templates are shown to
  be subverted, so the pass is the hardening), asserts the directive on all five
  prompts, the fenced bindings in the improver/generator, the scrubbed chain
  output, and the binder's question builders.
- **RC-9 — hardening defaults (FIXED).** The four SHA-1 sites are cache keys and
  fingerprints, not signatures; they now declare `usedforsecurity=False` (digests
  unchanged, so stored cache signatures stay valid) with the registry rule
  suppressed inline by id. The toolkit's own `weak-hash-non-crypto` rule accepts
  the flag. `semgrep scan` over `app/` with the custom rules + `p/python` +
  `p/secrets`: **0 findings**.
- **RC-7 — supply chain (FIXED).** `.github/workflows/security.yml` runs
  `security-tests`, `sast` and `sca` on every push and PR, with every `uses:`
  pinned to a commit SHA; `.github/dependabot.yml` bumps pip and actions weekly
  with a 7-day release cooldown. The `sast` job reports WARNING and ERROR rules
  only (`--severity`): `--error` makes any reported finding fatal, and the
  toolkit's INFO rules are review aids, not findings —
  `untrusted-placeholder-in-prompt-template` marks every prompt bind site so a
  reviewer confirms it is hardened (ours are; `test_prompt_assembly.py` proves
  it). Local scans run without the filter and still show them.
  Jobs run on Python 3.11, matching `render.yaml`'s `PYTHON_VERSION`. The
  toolkit is a **public** repository, so every job installs it anonymously
  from the git pin; `sca` installs the PyPI pins only (the toolkit is audited
  in its own CI). An optional **`TOOLKIT_TOKEN`** secret is honored by
  `security-tests` and `sast` only if it is set — the provision for the
  repository ever being made private again. **Render:** the build command
  installs the same pin anonymously; it uses a `TOOLKIT_TOKEN` environment
  variable (declared in `render.yaml`, `sync: false`) only when one is set, and
  is guarded so an unset variable never reaches the URL. Frontend:
  `firstplay-frontend/.github/workflows/test.yml` actions pinned to SHAs, plus an
  `npm audit --audit-level=high` job and `.github/dependabot.yml` (npm + actions,
  weekly), in
  [firstplay-frontend PR #6](https://github.com/victorzhu443/firstplay-frontend/pull/6).
- **Dependencies.** `requirements.txt` pins
  `security-agents @ git+https://github.com/victorzhu443/security-agents@v0.1.1`
  (locally: `pip install -e ../../security-agents`), keeps `httpx` at a
  version with the `sni_hostname` extension the pinned fetch relies on, and moves
  `python-dotenv` to 1.2.2 (the advisory's fix; requires Python ≥ 3.10) under an
  environment marker, with 1.2.1 kept for the 3.9 development venv. No
  `--ignore-vuln` remains in the `sca` job.

### SAST — Semgrep (`semgrep 1.136.0`, custom rules + registry packs)

Backend (Python): `security-agents/semgrep/scan.sh .` →
`rules.yml` + `p/python` + `p/secrets` + `p/django` + `p/owasp-top-ten`, 71 files.
Baseline had **4 findings**, all `insecure-hash-algorithm-sha1` on non-cryptographic
cache keys (`app/autofill/__init__.py`, `jev_binder.py` ×2, `scale.py`) — **closed**
as above. Now **0 findings** on `app/`.

Frontend + extension (JS/TS): `p/javascript` + `p/owasp-top-ten`, 80 files,
**2 findings**, both `github-actions-mutable-action-tag` in the frontend workflow
— **closed** (PR #6). No application JS findings.

Note on the custom LLM01 rule (`untrusted-text-into-prompt-sink`): it does not
fire on `job_parser.py`, because that sink is a LangChain `ChatPromptTemplate`
placeholder + `.partial()`, not an f-string / `.format()` the rule matches. The
sink is covered by `test_prompt_assembly.py` instead, which exercises the real
template.

### Fuzzing — Hypothesis (`fuzzing.fuzz_lib.assert_robust`)

`firstplay-backend/tests/security/test_fuzz_parsers.py`, 300–400 examples each:

- `buckets.parse_number`, `parse_month_year`, `parse_term`, `parse_compound_term`
  — pure string parsers over a stored answer / option label. **Degrade safely.**
- `ashby.parse_ashby_dom` with hostile *text* inside a well-formed extract —
  **degrades safely.**
- `ashby_api.parse_ashby_posting` with a fully fuzzed nested payload shape —
  **degrades safely** (malformed nodes → `MalformedPayload`, a `ValueError`).

**Finding (real bug, FIXED 2026-11-25):** `parse_ashby_dom` did not shape-check
its DOM nodes the way `parse_ashby_posting` does, so a structurally malformed
extract raised an undeclared `AttributeError` / `TypeError` (a 500) where the
posting parser answers 400. It now routes the payload through the shared
`_as_dict` / `_as_list` (now `secprim`'s) and `str()`-coerces control scalars.
Tests `test_parse_ashby_dom_rejects_malformed_structure_cleanly` and
`test_parse_ashby_dom_tolerates_nonstring_scalars` assert it.

### SSRF trace (bandit + manual)

`bandit -r app` flagged 7 `urllib.urlopen` sites (B310). Each is an offline
CLI/batch script fetching hardcoded `https://boards-api.greenhouse.io` URLs
(constant scheme and host; only path segments vary) — not remote-reachable. The
web SSRF surface is `/api/job/url`, closed as RC-4 above. The IPv4-mapped-IPv6
form of the metadata address is `is_global=False` on the backend's Python 3.9.6
and is refused by `secprim.resolve_and_validate` like every other non-public
address.

### Prompt-injection — robustness harness (`promptinject`)

- `tests/security/test_prompt_injection.py` — the stand-in sink: undefended,
  7/7 attacks land; through `harden_untrusted` + `scrub_output`, 8/8 pass.
- `tests/security/test_prompt_assembly.py` — the real sinks: the actual
  `job_parsing_prompt` and `resume_parsing_prompt` with their text hardened, 8/8
  each; the same templates unhardened are subverted; all five system messages
  carry `DIRECTIVE`; `parse_jd_text` / `parse_resume_text` demonstrably hand the
  chain the hardened text; the improver and generator bind fenced job-derived
  values; chain output is scrubbed (`scrub_model`); every binder instruction
  opens with the directive; the binder's questions carry labels/answers fenced
  and option criteria fenced while keys stay bare; a tripwire marker in a model
  choice is scrubbed.

`app/security.py` exports `DIRECTIVE`, `harden_untrusted` and `scrub_output`,
importing from `promptinject` with a self-contained fallback so the app never
hard-depends on the toolkit.

## Remaining

1. ~~Wire the sink wrapper into the live call sites~~ — **DONE** (RC-8).
2. ~~Fix `parse_ashby_dom`'s robustness gap~~ — **DONE**.
3. ~~Close the DNS-rebinding SSRF on `/api/job/url`~~ — **DONE** (RC-4).
4. ~~SHA-1 on the four cache/fingerprint sites~~ — **DONE** (RC-9).
5. ~~Pin GitHub Action tags to commit SHAs in the frontend workflow~~ — **DONE**
   (frontend PR #6).
6. `TOOLKIT_TOKEN` is **optional** (the toolkit is public): nothing needs to be
   set for CI or the Render build. Only if `security-agents` is made private
   again, add a fine-grained read PAT as the `TOOLKIT_TOKEN` repository secret
   and as a Render environment variable; both install paths pick it up.
7. **Burp Suite** pass against the local FastAPI app (manual;
   `../security-agents/runbooks/burp-suite.md`) — not automated here.
8. **Dependency bumps, in their own PR.** The `sca` job on `main` (Python
   3.11, runtime pins only; run 2026-11-26) reports **72 known advisories in 13
   packages**, all against pins that predate this work: `starlette` 0.41.3 (14),
   `python-multipart` 0.0.20 (12), `langchain-core` 0.3.63 (11),
   `langgraph-checkpoint` (6), `langsmith` (5), `pdfminer-six` (4, via
   `pdfplumber`), `langchain-text-splitters` (4), `setuptools` (3), `langgraph`,
   `langgraph-sdk`, `langchain` (3 each), `langchain-openai` and
   `langchain-community` (2 each). The local 3.9 venv additionally shows
   `urllib3`/`pillow`/`aiohttp` through dev-only and transitive packages that
   CI does not install. Nothing in the set is new. The `sca` job is **red until
   the bump PR lands** behind the full suite; that PR is deliberately separate
   (RC-7 edit plan: bumps are isolated behind the tests).
9. Hardening changes the text the model sees for form labels and options (fence
   tokens, redacted override phrasing, the directive at the head of every
   question). Watch the autofill coverage measurement (`DECISIONS`) for a shift
   after deploy; the fence and the per-question directive are the accepted cost.

## Re-run

```bash
cd firstplay-backend

# install the toolkit into the backend venv (one-time; CI uses the git pin)
./.venv/bin/python -m pip install -q -e ../../security-agents
./.venv/bin/python -m pip install -q semgrep      # if not already present

# security tests: fuzzed parsers, pinned fetch, prompt assembly
./.venv/bin/python -m pytest tests/security/ -q

# SAST — backend (Python) and the JS side
PATH="$PWD/.venv/bin:$PATH" ../../security-agents/semgrep/scan.sh .
PATH="$PWD/.venv/bin:$PATH" semgrep scan --config p/javascript --config p/owasp-top-ten \
  --metrics off --exclude node_modules ../firstplay-frontend ../firstplay-extension

# SCA
./.venv/bin/python -m pip install -q pip-audit
# Locally the editable toolkit checkout is skipped (not on PyPI; audited in its own
# CI), which --strict would count as a failure, so --strict is CI-only (sca job).
# The 3.9 venv still carries python-dotenv 1.2.1 (its two advisories); CI and
# production run 3.11 with 1.2.2.
./.venv/bin/pip-audit --skip-editable
```
