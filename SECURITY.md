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
| job extraction (`JobParsed`) | scraped posting text → `app/chains/job_parser.py` `{job_text}` prompt | injected instructions steer skills/qualifications extraction (LLM01) | **FIXED** — hardened at the sink, directive in the system message |
| autofilled answers | field labels / option text / stored answers → `app/autofill/jev_binder.py` questions | hostile form wording coerces an answer the applicant never gave (LLM01) | **FIXED** — fenced where formatted; model choice scrubbed |
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
  must pass `assert_robust`) before it is merged.
- **RC-8 — prompt assembly chokepoint (FIXED; closes the "wire the sink
  wrapper" item).** `parse_jd_text` passes `harden_untrusted(job_text)` into the
  unchanged LangChain template, whose system message now ends with
  `promptinject.assemble.DIRECTIVE`. In `jev_binder.py` every label, option and
  stored answer goes through one helper (`_untrusted`) at the point it is
  formatted into a question, and every model answer's `choice` goes through
  `scrub_output` in `_decide`, the single seam model output crosses. Cache
  signatures and the labels returned to the caller use the raw text, so nothing
  else changed. `tests/security/test_prompt_assembly.py` binds the LLM01 corpus
  to the real template (8/8 resisted; the unhardened template is shown to be
  subverted, so the pass is the hardening) and to the binder's question builders.
- **RC-9 — hardening defaults (FIXED).** The four SHA-1 sites are cache keys and
  fingerprints, not signatures; they now declare `usedforsecurity=False` (digests
  unchanged, so stored cache signatures stay valid) with the registry rule
  suppressed inline by id. The toolkit's own `weak-hash-non-crypto` rule accepts
  the flag. `semgrep scan` over `app/` with the custom rules + `p/python` +
  `p/secrets`: **0 findings**.
- **RC-7 — supply chain (FIXED).** `.github/workflows/security.yml` runs
  `security-tests`, `sast` and `sca` on every push and PR, with every `uses:`
  pinned to a commit SHA; `.github/dependabot.yml` bumps pip and actions weekly.
  The toolkit is a private repository, so the workflow needs a fine-grained PAT
  with read access to `security-agents` as the **`GH_PAT`** secret — without it
  the install step fails, by design. Frontend:
  `firstplay-frontend/.github/workflows/test.yml` actions pinned to SHAs in
  [firstplay-frontend PR #6](https://github.com/victorzhu443/firstplay-frontend/pull/6).
- **Dependencies.** `requirements.txt` pins
  `security-agents @ git+https://github.com/victorzhu443/security-agents@v0.1.0`
  (locally: `pip install -e ../../security-agents`) and keeps `httpx` at a
  version with the `sni_hostname` extension the pinned fetch relies on.

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
- `tests/security/test_prompt_assembly.py` — the real sink: the actual
  `job_parsing_prompt` with `job_text` hardened, 8/8 pass; the same template
  unhardened is subverted; the system message carries `DIRECTIVE`;
  `parse_jd_text` demonstrably hands the chain the hardened text; the binder's
  questions carry labels/answers fenced and option criteria fenced while keys
  stay bare; a tripwire marker in a model choice is scrubbed.

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
6. **Set the `GH_PAT` secret** on `firstplay-backend` so the `security` workflow
   can install the private toolkit; until then its three jobs fail at install.
7. **Burp Suite** pass against the local FastAPI app (manual;
   `../security-agents/runbooks/burp-suite.md`) — not automated here.
8. **Dependency bumps, in their own PR.** `pip-audit` over the installed
   runtime set (2026-11-26) reports known advisories against the pinned
   `fastapi`/`starlette` 0.115/0.41, `python-multipart`, `urllib3`, `langchain-*`
   and transitive `pillow`/`aiohttp` versions. The `sca` job is therefore **red
   until a bump PR lands** behind the full suite; that PR is deliberately
   separate from this one (RC-7 edit plan: bumps are isolated behind the tests).
9. Hardening changes the text the model sees for form labels and options (fence
   tokens, redacted override phrasing). Watch the autofill coverage measurement
   (`DECISIONS`) for a shift after deploy; the fence is the accepted cost.

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
./.venv/bin/pip-audit --skip-editable --ignore-vuln GHSA-mf9w-mj56-hr94 --ignore-vuln PYSEC-2026-2270
```
