# FirstPlay Coach — Backend

FastAPI service that reads a student's resume and a job posting, works out which
required skills are missing, and returns portfolio projects that would close the
gap along with a rewritten resume tailored to that posting.

Built for early-career CS students, whose problem is usually not the resume's
wording but that the resume and the posting are describing different skill sets.

- **Live API:** https://firstplay-backend.onrender.com
- **Interactive docs:** https://firstplay-backend.onrender.com/docs
- **Frontend:** https://firstplay-frontend.vercel.app ([repo](https://github.com/victorzhu443/firstplay-frontend))

> The API is on Render's free tier and sleeps when idle. The first request after
> a period of inactivity takes about **47 seconds** to wake the container;
> requests after that return normally. This is startup latency, not the
> pipeline's.

---

## Contents

- [How it works](#how-it-works)
- [API](#api)
- [Running locally](#running-locally)
- [Running the tests](#running-the-tests)
- [Project layout](#project-layout)
- [Deployment](#deployment)
- [Known limitations](#known-limitations)

---

## How it works

The core is a five-node [LangGraph](https://langchain-ai.github.io/langgraph/)
pipeline in `app/pipeline/`. State flows through it as a `PipelineState`
TypedDict, each node reading what the previous one wrote.

```
          resume_id + job_id
                  │
                  ▼
      ┌───────────────────────┐
      │ 1. parse_resume       │  LLM   PDF text ──▶ ResumeParsed
      └───────────┬───────────┘        (name, skills, experience,
                  │                     projects, education)
                  ▼
      ┌───────────────────────┐
      │ 2. parse_job          │  LLM   posting ──▶ JobParsed
      └───────────┬───────────┘        (title, required/preferred
                  │                     skills, responsibilities)
                  ▼
      ┌───────────────────────┐
      │ 3. analyze_gap        │  ────  deterministic filter + set
      └───────────┬───────────┘        no LLM call
                  │                    ──▶ overlapping / missing skills
                  ▼
      ┌───────────────────────┐
      │ 4. generate_projects  │  LLM   gaps ──▶ 3-5 ProjectIdea
      └───────────┬───────────┘        (difficulty, duration, features)
                  │
                  ▼
      ┌───────────────────────┐
      │ 5. improve_resume     │  LLM   ──▶ ImprovedResumeParsed
      └───────────┬───────────┘        rewritten in Jake's template
                  │
                  ▼
               results
```

Four of the five nodes call `gpt-4o-mini`. Node 3 is deliberately not one of
them: comparing two lists of skills is set arithmetic, and doing it in code
makes it deterministic, free, and testable.

Before comparing, it discards job-description entries that are requirements
rather than technologies — "5+ years experience", "Bachelor's degree in
Computer Science". Nothing in a resume's skills list can ever match those, so
they would sit in `missing_required_skills` permanently, and that list is what
the project generator is asked to build a portfolio project around.

It also reads beyond the skills section. A tool named only in an experience
bullet or a project description counts as `weak_skills` — weaker evidence than a
declared skill, but enough that the candidate is not told to go and learn
something they have demonstrably used.

**Failure behaviour.** Failures are an append-only list of
`{node, error_type, message}` on the state, and a conditional edge after each
node ends the run at the point of failure. A run that breaks at node 1 does not
go on to spend three more LLM calls on data it never received, and because the
list is append-only the first (root) cause is never overwritten by a later
node's complaint about missing input.

Partial work survives. If three nodes succeed and the fourth fails, the run
returns what it produced rather than discarding it — see
[the pipeline endpoint](#pipeline) for how that is reported.

**Blocking calls run off the event loop.** The handlers that call LangChain's
synchronous `.invoke()` are declared `def`, not `async def`, so FastAPI runs
them in its threadpool. Declared `async`, a 30s LLM call would occupy the event
loop and stall every other request in the worker — including `/health`, which on
Render means the platform restarts the service mid-request.

**Structured output.** Each LLM node pipes through a `PydanticOutputParser`
against a schema in `app/schemas.py`. The schemas tolerate nulls in fields a
sparse resume can legitimately omit — a missing employment date is normalised to
`""` rather than failing the parse and taking the whole run down with it.
Because the format is requested rather than
guaranteed, `invoke_with_retry()` in `app/llm_client.py` retries a response
that fails to parse, raising the temperature on each attempt — a retry at
temperature 0.0 resamples the identical completion and fails identically, so the
sampling has to change for the retry to be worth anything.

Every stage is persisted to SQLite (`app/models.py`): `resumes`,
`job_descriptions`, `gap_analyses`, `project_plans`, `improved_resumes`. Parsed
output is cached on the row, so re-running against the same resume skips its
LLM call.

---

## API

Full interactive docs at `/docs`. The frontend uses the four endpoints marked ★.

### Resume

| Endpoint | Description |
|---|---|
| ★ `POST /api/resume/upload` | `multipart/form-data` with `file` (PDF), **max 5 MB**. Extracts text with `pdfplumber`. → `{resume_id, original_filename, raw_text_preview}` |
| `POST /api/resume/parse?resume_id=` | Runs node 1 alone. → `ResumeParsed` |
| `POST /api/resume/improve?resume_id=&job_id=` | Runs node 5 alone. Requires both to be parsed and the gap analysis to exist. → `ImprovedResumeParsed` |

### Job description

| Endpoint | Description |
|---|---|
| ★ `POST /api/job/description/manual` | `{jd_text}`, minimum 50 characters. → `{job_id, text_preview}` |
| `POST /api/job/url` | `{url}`. Fetches and strips the posting with BeautifulSoup. Response body capped at 2 MB, at most 5 redirects, and each hop is re-checked against private address ranges — otherwise the endpoint is a proxy into localhost and the internal network for anyone who can call it. → `{job_id, text_preview}` |
| `POST /api/job/parse?job_id=` | Runs node 2 alone. → `JobParsed` |

### Analysis

| Endpoint | Description |
|---|---|
| `POST /api/analyze` | JSON body `{resume_id, job_id}` → gap analysis only |
| `POST /api/projects?analysis_id=` | Runs node 4 alone → project ideas only |

Note that the parameter style is inconsistent: `/api/analyze` and
`/api/pipeline/run` take a JSON body, while `/api/resume/parse`,
`/api/resume/improve`, `/api/job/parse` and `/api/projects` take query
parameters. Posting a body to the latter returns `422 Field required`.

### Pipeline

| Endpoint | Description |
|---|---|
| ★ `POST /api/pipeline/run` | `{resume_id, job_id}` — all five nodes |

The response always has the same shape, whatever happens. `status` says how far
the run got, and `completed_steps` lists the nodes that succeeded, so a caller
renders whatever is present rather than branching on a separate error format:

| `status` | HTTP | Meaning |
|---|---|---|
| `complete` | 200 | All five nodes succeeded |
| `partial` | 200 | Some nodes succeeded, then one failed. The results that were produced are in the body, alongside why it stopped |
| `failed` | 502 | Nothing was produced |

```jsonc
{
  "status": "partial",
  "resume_id": 1, "job_id": 2,
  "analysis_id": 5, "project_plan_id": 3, "improved_resume_id": null,
  "completed_steps": ["parse_resume", "parse_job", "analyze_gap", "generate_projects"],
  "failures": [{ "node": "improve_resume", "error_type": "LLMOutputError", "message": "..." }],
  "gap_analysis": { }, "projects": [ ], "improved_resume": null
}
```

### Health

`GET /` returns a service banner, `GET /health` returns `{"status": "ok"}`.

### Errors

Single-step endpoints return `500` with a `detail` string. The pipeline endpoint
reports failure through `status` and `failures` instead, as above, reserving
`502` for a run that produced nothing and `500` for the graph itself failing to
execute.

Failures inside an LLM call are classified rather than flattened into one
opaque `Exception` (`app/exceptions.py`), because the caller's options differ:

| Type | Meaning | Retried? |
|---|---|---|
| `LLMOutputError` | Model replied, output failed parsing or validation | Yes, at a higher temperature |
| `LLMServiceError` | Timeout, connection failure, rate limit, upstream 5xx | Already retried by the OpenAI SDK at the transport layer |
| `LLMConfigurationError` | Bad key, no permission, malformed request | No — retrying cannot fix it |

### A full run

```bash
API=https://firstplay-backend.onrender.com

RESUME_ID=$(curl -s -X POST "$API/api/resume/upload" \
  -F "file=@resume.pdf" | python3 -c 'import sys,json;print(json.load(sys.stdin)["resume_id"])')

JOB_ID=$(curl -s -X POST "$API/api/job/description/manual" \
  -H "Content-Type: application/json" \
  -d '{"jd_text": "Backend engineer. Required: Python, FastAPI, PostgreSQL, Docker..."}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')

curl -s -X POST "$API/api/pipeline/run" \
  -H "Content-Type: application/json" \
  -d "{\"resume_id\": $RESUME_ID, \"job_id\": $JOB_ID}"
```

---

## Running locally

Requires Python 3.9+ and an OpenAI API key.

```bash
git clone https://github.com/victorzhu443/firstplay-backend
cd firstplay-backend

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env               # then add your OPENAI_API_KEY

alembic upgrade head               # create the schema

uvicorn app.main:app --reload
```

The API serves on http://localhost:8000, docs on http://localhost:8000/docs.

The schema is managed by Alembic, not created at startup, so `alembic upgrade
head` is a required step on a fresh checkout and after any model change. It is
idempotent, and production runs it on every start.

To run the frontend against it, set `NEXT_PUBLIC_API_URL=http://localhost:8000`
in the frontend's `.env.local`. Ports 3000 and 3001 are already in the CORS
allowlist in `app/main.py`.

### Environment

| Variable | Required | Notes |
|---|---|---|
| `OPENAI_API_KEY` | yes | Four of the five pipeline nodes need it |
| `DATABASE_URL` | no | Defaults to `sqlite:///./firstplay.db`. Production sets a managed Postgres URL. Render emits a `postgres://` scheme, which SQLAlchemy 2.x rejects; `app/db.py` normalises it |
| `LOG_LEVEL` | no | Level for the stdout handler. Defaults to `INFO` |

CORS origins are not configurable by environment: production origins are listed
in `app/main.py`, with Vercel previews admitted by `allow_origin_regex`.

---

## Running the tests

**234 tests, no setup required.**

```bash
source .venv/bin/activate
pytest tests/ -v
```

Expected, on a fresh clone with a cold database and no `OPENAI_API_KEY` set:

```
234 passed
```

The suite mocks every LLM call, so it runs in about a second, costs nothing, and
needs no credentials. `conftest.py` removes each thing that used to be a manual
prerequisite:

- It seeds a dummy `OPENAI_API_KEY` at import time, before `app.llm_client`
  calls `load_dotenv()`. Several tests construct a `ChatOpenAI` client without
  ever calling it, and `get_llm()` raises when the variable is unset. Seeding it
  here also stops a developer's real key in `.env` from being picked up.
- A session-scoped fixture creates the schema before any test runs, so a cold
  database is fine. Previously the first run on a fresh clone failed 5 tests in
  `test_analysis.py` with `OperationalError` and the second run passed with
  nothing changed — the run itself left the tables behind, hiding the failure
  locally while breaking CI, which always starts cold.
- A session-scoped fixture generates the sample resume PDF. It is gitignored, so
  a fresh clone does not have it; `reportlab` is now declared in
  `requirements-dev.txt` rather than left as an undeclared dependency of a
  script someone had to know to run.

Test-only dependencies live in `requirements-dev.txt`, so `requirements.txt`
stays what production installs.

| File | Covers |
|---|---|
| `test_schemas.py`, `test_schemas_nullable.py` | Pydantic models, and tolerance of nulls from a sparse resume |
| `test_pipeline_graph.py`, `test_pipeline_nodes.py`, `test_pipeline.py` | Graph wiring and node behaviour |
| `test_pipeline_failures.py` | Halting at the failing node, partial results, root cause preserved |
| `test_llm_errors.py`, `test_llm_retry.py` | Failure classification, and retry with temperature escalation |
| `test_concurrency.py` | Blocking handlers do not stall the event loop |
| `test_resume_parser.py`, `test_job_parser.py`, `test_project_generator.py`, `test_resume_improver.py` | Chain construction |
| `test_gap_analysis.py`, `test_gap_analysis_filtering.py`, `test_analysis.py` | Skill matching, normalisation, and rejecting non-skills |
| `test_router_correctness.py` | CORS regex, PDF error propagation, newest-analysis lookup |
| `test_resume.py`, `test_job.py`, `test_main.py`, `test_db.py` | Endpoints, PDF extraction, persistence |

---

## Project layout

```
app/
├── main.py              FastAPI app, CORS, lifespan
├── db.py                Engine and session; resolves DATABASE_URL
├── logging_config.py    Stdout logging, level from LOG_LEVEL
├── models.py            Five ORM tables
├── schemas.py           Pydantic models for structured LLM output
├── exceptions.py        Typed LLM/pipeline failures
├── llm_client.py        ChatOpenAI factory, failure classification, retry
├── routers/             resume, job, analysis, pipeline endpoints
├── chains/              One LangChain chain per LLM node
├── analysis/            Deterministic gap analysis (no LLM)
└── pipeline/
    ├── state.py         PipelineState TypedDict, NodeFailure
    ├── nodes.py         The five node functions
    └── graph.py         Graph wiring and run_pipeline()
conftest.py              Dummy API key, schema and PDF fixture for the suite
migrations/              Alembic revisions
tests/                   234 tests
```

`app/pipeline/graphy.py` is not imported anywhere. It is an abandoned variant of
`graph.py` in which nodes 4 and 5 branch from `analyze_gap` and run in parallel,
rather than 5 following 4. Both genuinely depend only on the gap analysis, so
the idea is sound and would cut a run by roughly one LLM call's latency — it was
just never wired up. Either revive it or delete it; leaving two graph
definitions side by side invites editing the wrong one.

---

## Deployment

Deployed on Render from `render.yaml`, auto-deploying on push to `main`.
`OPENAI_API_KEY` is set in the Render dashboard, not in the repo; `DATABASE_URL`
is wired from the managed Postgres instance the blueprint declares, and the
Python version is pinned so production does not drift from local.

The start command is `alembic upgrade head && uvicorn ...`. Migrations run there
rather than in a `preDeployCommand`, which is not available on every instance
type; `alembic upgrade head` is idempotent and a no-op once the schema is
current.

`healthCheckPath` is `/health`. That is only meaningful now that blocking LLM
calls no longer run on the event loop — previously a slow pipeline run could
fail the check and have the platform restart the service mid-request.

The frontend is on Vercel and reaches this service through
`NEXT_PUBLIC_API_URL`. Production origins are listed in `allow_origins` in
`app/main.py`, and Vercel preview deployments are admitted by
`allow_origin_regex`. Note that `allow_origins` is matched by exact string
comparison — a wildcard entry like `https://*.vercel.app` matches nothing at
all, so patterns belong in the regex, never the list.

---

## Known limitations

**Render's free Postgres instance expires.** Storage is a managed instance
rather than a file on the container's disk, so it now survives restarts — but
free instances are removed after a trial period. Move to a paid plan before this
holds anything worth keeping.

**Cold starts.** Measured at 47 seconds from sleep. The frontend gives no
indication that a first request may take this long.

**Skill filtering is a heuristic.** `is_skill_like()` rejects entries that name
a requirement rather than a technology — years of experience, credentials, and
anything longer than four words. A genuinely long technology name is dropped
along with them.

**No authentication or rate limiting.** Every endpoint is open, and each
pipeline run spends four LLM calls against the deployment's API key.

**Resume IDs are sequential integers.** Anyone can request another user's
`resume_id`.
