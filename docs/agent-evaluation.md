# How the FirstPlay agent manages state, tools and retrieval, failures, and evaluation

An honest assessment as of 2026-10-01, written against the four questions
Cheiron's application asks about a system you built. Every number here is
from a recorded run (DECISIONS §45–§50, the extension's `survey/` folder).

## State

**Where state lives.** Three places, by design, and nowhere else.

| state | where | why there |
|---|---|---|
| the applicant's profile (facts, education, legal status, preferences, protected answers, standing consents, skip list, replayed answers, learned facts) | the extension's `chrome.storage.local`, sent with every request; a JSON file under `~/.config/firstplay/` for the CLI | the backend is stateless on purpose: nothing about a person is stored anywhere a second person's data could sit next to it |
| per-page run state (the plan, the outcome record, what has been written) | the content script, plus a `data-firstplay-outcome` attribute on the page | the page is the only ground truth about what landed; the attribute makes each run auditable from outside the extension |
| caches (label → theme, option translations, plan cache keyed on posting + profile fingerprint + engine fingerprint) | backend memory and the extension's storage | the same question recurs 8.5× on average across postings; the engine fingerprint in the key is what stopped stale plans after a backend change (§42) |

**What went wrong with state, and the fix.** Two react-select picks in the
same frame lost the first in Greenhouse's form state (§44) — one pick per
frame now. A plan cached before a backend change kept serving the old
answer — the engine fingerprint joined the cache key. Ashby autosaves every
written value to its server, so on Ashby a fill is never purely local; the
design accepts that and never clicks Submit there (§47). The weak point
that remains: the profile is a single JSON blob the user pastes into a
popup; a schema version and migration are not there yet.

## Tools and retrieval

**Tools.** The agent's "tools" are deliberately few and exact: the ATS's own
form definition (Greenhouse board API; Ashby's `ApiJobPosting` GraphQL
operation, found by reading the front-end bundle after the codebase had
said no such API existed, §46), the page DOM for writing only, the form's
own validation as the oracle on Greenhouse (a blocked dry-run submit and
`aria-invalid` read-back), and a local required check from the API's flags
on Ashby where validation is server-only.

**Retrieval.** Retrieval is lookup before judgement, cheapest first: exact
field key → normalised label alias → sentence-shaped label pattern →
cached theme → one batched Jev classification → computed or stored resolver
→ option translation → the profile-answer gate → the second pass. Only the
last four involve a model, and every model question is bounded (a Noul or a
Choice over options the form or the profile supplies), never free text.
Measured on the frozen corpora with the real profile: 85.2% of non-essay,
non-file fields filled on 578 Greenhouse forms and 81.9% on 413 Ashby forms;
the model decides a small share of those (4% of filled values in the
42-posting labelled set, 12/12 correct).

**What went wrong.** Retrieval that was too loose was the main precision
risk: "College Recruiting – Careers Services" matched the employer's own
site; a graduation date matched "SAT score" at low confidence; a location
answered a yes/no. Each is now a guard or a narrower pattern, with a test
that names the board it came from.

## Failures

**The failure taxonomy the system reports on itself**, per page, in the
outcome record: filled; left for the applicant (with a reason); known but
could not be entered (the one number that measures the filler rather than
the profile); in the plan but not on the page; and, since 0.4.33, what the
form's own validation still wanted after the fill, with what the plan had
said about each field. That last column is how the worst defect of this
round was found: three Ashby forms reported a yes/no as filled while
nothing was pressed (Ashby's Boolean is two `aria-pressed` buttons; the
filler had treated the hidden checkbox as the control). The record said
"plan said FILL, form still wants it"; a DOM read showed why.

**Failure handling in the loop.** Known-but-could-not-enter went 21 → 0 on
Greenhouse across 64 consecutive unseen boards after 0.4.19, and 4 → 0 → 1
on Ashby across 212 organisations (the 1 is an employer-scoped geocoder
that will never offer a US city, now reported as a mismatch for the
applicant rather than a filler failure). Every defect gets: a reproduction
on the live page, a measurement before and after, a test that names the
board, and a DECISIONS entry. Misdiagnoses are recorded too (Espa/Fanvue
were first read as timing, §47; a "dry-run race" theory was wrong before the
one-pick-per-frame finding, §44).

**What the system refuses to do.** Submit. Write model-composed text under
the applicant's name. Let a model near a protected-class or consent field
(string logic and the applicant's own standing decisions only). Fill a
required follow-up it cannot read. Learn anything without the applicant
turning learning on, and promote a learned answer to a profile fact without
two companies and an Accept.

## Evaluation

**Three layers, all with sample sizes.**
1. *Offline, every change:* 620 backend tests (no network, ~13 s), plus
   `coverage.py` over 991 frozen forms with the real profile, reported as
   coverage per ATS and a ranked list of what is still unfilled — the
   hypothesis list for the next round. The ceiling is measured separately
   on a placeholder profile so "the engine is missing it" and "the applicant
   has not said" are never confused.
2. *Live, held out:* fresh draws from the Simplify lists, never a board
   used before (a ledger of 212 Ashby organisations and 196 Greenhouse
   boards), through the installed extension, with the page's own record as
   the log. 116 Greenhouse boards and 212 Ashby organisations so far; fill
   median 88–110 ms on Ashby, 0.1–0.4 s on Greenhouse without a geocoder.
3. *Precision by hand:* 428 human-labelled fields at 100% on the early set;
   every second-pass answer across 224 postings read line by line (14/14).

**What evaluation does not yet cover, honestly.** The Greenhouse hundred of
this round is 8 of 100 run (the window must be on screen). The learning
loop is measured offline only (replay 94.3% / 96.6% on simulated answers);
its live metric — questions the applicant has to answer twice — has no
data yet. Essays are out of scope by decision and are not counted. A June
2026 budget-matched study warns that agent-memory gains vanish at equal
token cost, so the learning loop will be judged by that live metric, not
by coverage lift.

**The one-paragraph verdict.** State is explicit and auditable because the
page itself carries the run record; tools are exact rather than general;
retrieval is lookup-first with bounded model questions last; failures are
measured on unseen boards and each one leaves a test and a record; and
evaluation has three layers with stated sample sizes. The gaps are the
ones the numbers point at: the Greenhouse live hundred, the learning loop's
live metric, a profile schema version, and the Ashby education-history
block.
