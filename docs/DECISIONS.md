# Autofill: decisions and why

Every decision below was made against measured data, and each records what
would change it. Numbers come from **42 unique live SWE-intern postings** on
Greenhouse (57 frozen, 15 duplicates collapsed) totalling 909 fields, and from
**428 human-labelled fields**.

---

## The rule that governs every decision below

**Never conclude from assumption. Measure against real postings, record the
number, then decide — and say how many postings the number came from.**

This is not a stylistic preference; it has caught real errors, repeatedly:

| Assumption | What the data said |
|---|---|
| "The long tail is full of *do you have experience with X*" | 3 fields across 42 applications. The résumé gate was rejected on that number |
| "Label-wording regexes catch EEOC questions" | Decided **0 of 2,429** fields; vendor structure caught all of them |
| "Multi-selects are conflict-of-interest disclosures" | True of 46/52 in the general corpus — but Stripe uses one for "select the cohort dates that work best", the **only** way any form lets an applicant say "any term works". A blanket rule made it unanswerable |
| "No form offers a no-preference option" | Stated from **57** postings. Widening to **156** found the multi-select case. The claim should have named its sample |
| "The risky path is the model path" | 100% precision on model decisions; **all 7 errors came from memory lookups** |

Two corollaries:

1. **Quote the sample size with the claim.** "None of the 9 term questions in
   57 postings" is a fact. "No form does this" is a guess wearing a fact's
   clothes.
2. **When a number moves the wrong way, that is the finding.** Coverage
   dropping 4 points after a caching change exposed a bug no test caught; the
   live endpoint not improving after a fix exposed an edit that had silently
   not applied.

---

## 0. What this is for

Victor is a CS student applying to software engineering internships. Internship
recruiting is volume-driven — a student sends dozens to low hundreds of
applications. Each Greenhouse form asks about **20 fields**, and roughly 16 of
them are things he has already answered on a previous application.

The tool exists to remove that retyping. It is not an agent that reasons about
applications; it is a fact store with a recogniser in front of it.

Two consequences follow, and they decide almost everything else:

1. **The output is submitted under his name.** A wrong answer is not a bug, it
   is a false statement on a job application. So precision dominates coverage.
2. **The chore is repetitive, not hard.** Value comes from volume, which means a
   marginal field saved matters far less than it would in a one-off task.

---

## 1. Coverage excludes essays, attestations, and unreadable questions

**Decision.** The coverage denominator is *fillable* fields only: total, minus
NARRATIVE, minus CONSENT, minus UNKNOWN.

**Why.** Essays are a separate problem with a separate solution. Attestations
("I certify the facts are true") are made at submission, not replayed. A
question whose text is unreadable cannot be answered by anyone. Counting these
as misses measures something nobody intends to fix.

**Evidence.** 909 fields → 753 fillable. The excluded 156 are 2.9% narrative,
and the rest attestations and unreadable controls.

---

## 2. Precision target is 99%, not 95%

**Decision.** Precision ≥ 99% over labelled filled fields.

**Why.** 95% means roughly one wrong answer per 20-field application, submitted
under his name. The asymmetry is one-directional: a review costs seconds, a
false claim about work authorisation or qualifications does not.

**Status.** 100% on 428 labelled fields, including 12/12 model decisions.

---

## 3. The real target is review-fields-per-application, not 95% coverage

**Decision.** Optimise for **median ≤ 2 review fields per application at 100%
precision**. Treat 95% coverage as a proxy that has now outlived its usefulness.

**Why.** "95%" was named before there was data. The number that maps to lived
experience is how many fields he actually touches. At **median 3** (mean 2.9,
21/42 applications at ≤2), the chore is already ~85% gone. Moving coverage from
84% to 95% saves roughly 1.5 fields — about fifteen seconds — and the only
remaining route to it adds model judgement about his qualifications.

Fifteen seconds is not worth a category of error that does not currently exist.

**What would change it.** If median review load climbed above 4, or if a
deterministic route to 95% appeared.

---

## 4. No résumé-screening gate

**Decision.** Do not let a model answer "do you have experience with X?" by
reading the résumé. Rejected, not deferred.

**Why.** Measured value versus measured cost:

| | |
|---|---|
| Fields it would answer | **3 across 42 applications** = 0.07 per application |
| Cost | the full work history sent to OpenRouter on every run |
| New risk | a model asserting *qualification claims* about him |

The intuition that the long tail was full of "do you have experience with X"
questions was simply wrong. The 39 unmatched fields are transcript uploads (8),
an obliquely-worded GPA question (5), interview-recording consent (4), salary
acceptance, WhatsApp opt-in, and an alternate email — almost none of them
résumé-answerable.

**What would change it.** A corpus where résumé-answerable questions are
common — a different role type, or senior postings where "15+ years of X"
screens are routine. Re-measure before reconsidering.

---

## 5. Memory replays; models never infer protected characteristics

**Decision.** `ALLOWED_FILL_SOURCES` gates every field by *where a value may
come from*. Protected characteristics are `MEMORY`-only: replayed verbatim,
never reachable by model judgement.

**Why.** Two different acts were initially conflated. A model *inferring*
veteran status from a résumé is a guess about protected data. The applicant
*stating* it once and the tool replaying it is not a guess — it is byte-for-byte
what he would have typed. Blocking the second threw away most of the product's
value; an early version left 212 fields for manual entry on every application.

---

## 6. Attestations stay human; stable facts do not

**Decision.** "I certify that the facts are true" → CONSENT (human only).
"Are you a government official?" → LEGAL (memory-only, replayed).

**Why.** An attestation is *made* at submission — there is no prior fact to
replay, the act is the click. Being a government official is a stable fact about
him. Treating both as permanently manual cost ~70 fields of coverage and asked
the same questions on every application.

---

## 7. Classification reads vendor structure, never label wording

**Decision.** EEOC questions are identified by Greenhouse's `compliance` block
and Ashby's `_systemfield_eeoc` name prefix. Not by matching words in the label.

**Why.** Measured: a label-wording regex decided **0 of 2,429 fields**. Worse,
on a real Ashby form it would have missed race and gender entirely, because the
radio inputs are labelled with the *answer* ("Hispanic or Latino") and the
question text is absent from the DOM. Removing the regex and reading the
vendor's own marker deleted code *and* closed a hole where 11 protected fields
were model-fillable.

**Standing rule.** A heuristic may only ever make a field *less* auto-fillable.
Misreading a screening question as sensitive costs one manual answer; the
reverse puts a guess on a protected characteristic.

---

## 8. Themes, not direct fact binding

**Decision.** Classify each question into one of ~35 canonical themes, then
resolve the theme against the profile.

**Why.** Measured: 123 distinct screening phrasings collapse to 21 themes.
`work_authorization` alone covers **18 different wordings** at confidence 1.00.
A theme is an atomic question with a stable option set, so it is cacheable —
and the cache drives marginal cost toward zero. Direct fact binding would need a
different option set per applicant and could never be shared.

---

## 9. Arithmetic and calendars in code; semantics to Jev

**Decision.** GPA ranges, graduation terms, phone formats, country aliases and
"have you worked here before" are computed deterministically. Only genuinely
semantic option matching reaches Jev.

**Why.** Counting, arithmetic and date comparison are documented Jev failure
modes, and an `if` that costs nothing beats a call that can be wrong. Every
field moved off the model path is one that cannot be confidently wrong.

**Evidence.** Moving three shapes to code shrank the model-decided share of
fills from **7.2% to 4.2%** with no loss of coverage.

---

## 10. Never auto-submit

**Decision.** The FillPlan is a proposal. Every field is filled, satisfied, or
flagged — and the applicant reviews and submits.

**Why.** It is his name on the application. It also makes the review step a data
source: every correction becomes a remembered answer, replayed thereafter.

---

## 11. Standing decisions are answers, not gaps

**Decision.** A `skip` list records fields he never supplies. A skipped optional
field counts as handled; a skipped **required** field surfaces for review.

**Why.** "These applications don't require a cover letter, so I always skip it"
is an answer to the question. Counting it as a coverage miss measures a chore
that does not exist. The required-field exception exists because silently
leaving a required field blank would block submission.

**Reported separately** from filled, so coverage cannot be inflated by skipping
everything.

---

## 12. Labels key on the resolution, not the instance

**Decision.** A label identifies question-text + value + resolution path, so one
judgement covers every posting asking the same thing.

**Why.** Per-posting keying made the same person confirm his own first name four
times in a 40-field session. Re-keying turned 40 labels into 375 labelled
fields.

**Related.** The labeller confirms `wrong` and `should-have-asked` but not
`correct`. A session produced 7 mis-keyed "wrong"s out of 428 — a 1.6% labelling
error rate, the same order as the precision being measured — and one of them
nearly bought a feature nobody wanted.

---

## 13. Caches invalidate when the taxonomy changes

**Decision.** The theme cache stores a fingerprint of the theme set and discards
entries made under a different one.

**Why.** Adding a theme did nothing for any label already cached, so a confident
wrong answer survived the fix that should have removed it — invisibly, because
the cache reported a hit. 158 stale entries were masking a known bug.

---

## 14. Granting something is a decision, not a fact

**Decision.** Interview-recording consent, marketing opt-ins and salary-range
acceptance are CONSENT — never auto-filled.

**Why.** Each *grants* something rather than reporting a pre-existing fact, so
there is nothing to replay. This is the same line that keeps arbitration
agreements manual. Found as unclassified fields in the intern corpus:
Brighthire recording consent (4), WhatsApp opt-in (2), salary acceptance (2).

---

## 15. Never infer a country from an institution name

**Decision.** "Are you currently attending a university in Canada?" is computed
by comparing the question's country to a stored `university_country`. If that is
unset, the field goes to review — the institution name is never used to guess.

**Why.** Answering it from a generic "Currently Enrolled" is the same
generic-onto-specific error that produced a wrong Masters/PhD answer at 0.92
confidence. And a wrong answer here is a false statement about where the
applicant studies.

---

## 16. Greenhouse dropdowns are driven through react-select's instance, not the DOM

**Decision.** The extension selects a dropdown value by finding the react-select
component instance in the React fiber tree and calling its own `selectOption`.
It never tries to open the menu.

**Why.** Measured on a live Greenhouse form, in order:

| Attempt | Result |
|---|---|
| `el.value = x` | React re-renders over it; value reverts on blur |
| synthetic `mousedown` on control, on input; `keydown` ArrowDown / Space / Enter | `aria-expanded` stays `false` |
| calling the control's React `onMouseDown` via `__reactProps` | focuses, does not open |
| `instance.openMenu('first')`, `instance.setState({menuIsOpen:true})` | overridden on next render |
| **trusted** click (automation) | opens — proves the widget works |
| synthetic click on an option, once open | **selects** |
| `instance.selectOption(option)`, menu closed | **selects, and the displayed value confirms it** |

The reason: `menuIsOpen` is a **controlled prop** (`false`, not `undefined`).
Greenhouse's wrapper owns the open state and re-asserts it every render, so no
synthetic event and no instance method can open the menu — only a trusted
pointer event, which a content script cannot produce. Selection does not need
the menu, so that is the path. Every selection is read back from the widget's
displayed value before it is reported as filled.

**What would change it.** react-select removing `selectOption`, or Greenhouse
moving off react-select. The fallback would be `chrome.debugger` for trusted
input events, at the cost of the "is debugging this browser" banner.

---

## 17. The API's compliance block is not authoritative for what the page renders

**Decision.** Plan entries are reconciled against the live DOM; an entry with no
element is reported as "not on this page", never filled elsewhere.

**Why.** The board API listed an EEOC field `race` (Asian, White, …) for a
posting whose rendered form has only `hispanic_ethnicity` (Yes / No / Decline).
Keyed on `race`, no element exists; the label fallback finds no "Race" label
either. Had either matched loosely, "Asian" would have gone into a Yes/No
question — a protected field filled with an invalid value. It did not, because
`locate()` requires an exact label match, but the plan still *reported* it as
filled. The counts a plan reports are therefore an upper bound until the page
has been read.

---

## 19. Ashby radio-group names carry a per-page-load UUID; only the suffix is stable

**Decision.** The extension matches Ashby system fields on the
`_systemfield_…` suffix, never on the full name.

**Why.** Measured on the same Notion posting across two loads:

| Load | Gender radio group `name` |
|---|---|
| fixture capture | `d5e6e981-7a2e-44ba-8884-94a2836454c9__systemfield_eeoc_gender` |
| retest | `9fbac581-62c1-4826-b4c7-5ee7e671e1a1__systemfield_eeoc_gender` |

Within one load the exact name works, which is the extension's normal flow
(read and fill happen on the same page). The backend already keyed on the
suffix for classification (`ASHBY_EEOC_MARKER`, `PROTECTED_FIELD_SUFFIXES`), so
this was consistent there; the extension's `locate()` now does the same. With
the fallback, all three EEOC radios filled and read back correctly.

---

## 18. File uploads are attachments, not fills

**Decision.** `input[type=file]` entries are a distinct plan state, `attach`,
reported separately in coverage.

**Why.** Browsers forbid setting a file input from a script, by design. Calling
the résumé "filled" would draw a green outline over an empty upload. The tool
still knows exactly which file, so it is not a review item either — it is the
one click per application that is structurally the applicant's. 53 such fields
across the 42-posting corpus.

---

## 20. Employer-hosted Greenhouse postings come in three shapes; two are reachable today

**Measured, 2026-09-27.** Of the 57 live SWE-intern Greenhouse postings in the
frozen corpus, 26 apply on `*.greenhouse.io` and **31 on the employer's own
domain** — every one of the 31 with `?gh_jid=<job id>` in the URL and none with
the board token. Seven distinct hosts; the second-level domain equals the board
token for six (stripe, samsara, coinbase, databricks, duolingo → `careers.
duolingo.com`) and not for Lyft's 13 postings on `app.careerpuck.com`.

Opening three of them live showed three different renderings:

| Shape | Example | What the page holds | Reached by |
|---|---|---|---|
| Greenhouse's own job board served on the employer's domain | Duolingo `careers.duolingo.com/jobs/…?gh_jid=…` | The full form in the top document, control ids = API field names (14/24 by id; the other 10 are react-selects and the hidden `resume_text`, located by label as on Figma) | click-to-run; board guessed from the domain |
| Employer page that opens a Greenhouse iframe on "Apply" | Lyft `app.careerpuck.com/…?gh_jid=…` | Nothing until Apply is clicked; then `job-boards.greenhouse.io/embed/job_app?for=lyft&token=<id>` with 45 controls, ids = API names (21/22; `race` renders as `hispanic_ethnicity`, §17) | content script auto-runs **inside the frame** (`all_frames`); `for`/`token` name the posting exactly |
| Employer page whose embed never renders | Samsara `www.samsara.com/…/roles/8082091?gh_jid=…` | `#grnhse_app` stays empty ≥10s, before and after "Apply Now" (a `<button>` with no href/handler); no Greenhouse script, no iframe, no consent banner detected | **not reached**; 2/31 postings, cause unknown |

**Decisions.**

1. `detectPosting` recognises `?gh_jid=` on any host. The board comes from the
   page's `/embed/job_board/js?for=<board>` script when present, else the
   second-level domain, and a guess is flagged `boardGuessed`.
2. A guessed board is **verified**: the service worker fetches the schema and
   rejects it unless the returned `id` equals the page's `gh_jid`. Board tokens
   are global, so a wrong guess could otherwise return a different company's
   form with a colliding id. The failure names the guess and points at
   `job-boards.greenhouse.io`.
3. Content scripts run with `all_frames: true`, and inside the frame the
   posting is read from `for`/`token` — no guessing there. The top frame,
   seeing the frame, steps aside rather than reporting an empty page.
4. Lyft's frame appears only after Apply is clicked, so the frame path is
   automatic (it is a matched host) while the top-frame message on a page with
   no controls says to click Apply and run again.
5. Samsara is recorded as unreached rather than worked around; two postings do
   not justify guessing at an embed trigger nobody has observed.

**Why not always guess from the domain and skip verification?** The API check
costs the same request the plan needs anyway, and turns the one wrong case
(careerpuck → "careerpuck" is not a board) into a clear message instead of a
stranger's form.

---

## 21. One control that throws must not stop the form; dropdowns are three widgets, not one

**What happened (2026-09-27).** The first real click-to-run on Duolingo filled
name, email, phone and preferred name, then nothing after them. The plan's id
for "expected graduation timeline" sits on a `<div role="group">`, not an
input; `applyPlan` reached for the input value setter, it threw `Illegal
invocation`, and the exception unwound the whole loop — GPA, LinkedIn, work
authorisation and the rest were never attempted although the plan marked them
FILL. Victor found it before I did, because I had checked that ids matched and
not run a fill.

**Decisions.**

1. Every control is filled inside its own `try`; a throw becomes
   `failed: threw …` on that field and the loop continues. Nothing about one
   widget may cost the applicant the other twenty.
2. Dropdowns are dispatched by what is on the page, not by what the API calls
   the field. Measured widgets so far:

| Widget | Where measured | How to tell | Driver |
|---|---|---|---|
| react-select, controlled `menuIsOpen` | `job-boards.greenhouse.io` (Figma, Lyft embed) | `input[role=combobox]` / `.select__input` | fiber → `instance.selectOption`, read back `.select__single-value` (§16) |
| button + listbox | Duolingo's own renderer on `careers.duolingo.com` | id on a `<div>` holding `button[aria-haspopup=listbox][aria-controls=ID]`; `#ID` fills with `li[role=option]` on a synthetic click | click button, poll for `#ID`, click the option, read back the group's text |
| native `<select>` | not yet seen on a live SWE-intern form | `<select>` | set `.value`, dispatch `change` |

3. The listbox driver never re-clicks a selected option: on Duolingo that
   **deselects** it ("No" → "Select…"). A field already showing the wanted
   text is reported filled without touching it; an option with
   `aria-selected="true"` closes the list instead of being clicked.
4. Waits are generous because the page, not the extension, sets the pace: on
   Duolingo a 300 ms timer fired after ~1 s. The list is polled for up to
   1.5 s; the CDP tool itself timed out at 45 s while the fill kept running
   and completed.

**Verified end to end** by applying the backend's real plan for Duolingo
8805925002 in the page: 13/13 FILL fields read back correct (5 text, 1 GPA,
1 email, 1 URL, 5 listboxes), 1 attach, 6 review, 0 failed, 0 missing.

**Rule adopted:** a change to the fill layer is not done until a real plan has
been applied to a real page and read back. Structural checks (ids match the
API) are necessary, not sufficient — they were green here.

---

## 22. Dropdowns across the sites people actually apply on; Ashby has almost none

Victor's instruction: don't stop at Duolingo — find out how dropdowns work on
the sites people will always apply through. Measured 2026-09-27, live:

| Site | Path to the form | Dropdown widget | Handled by |
|---|---|---|---|
| Stripe (`stripe.com/careers/apply/…`) | `job-boards.greenhouse.io/embed/job_app?for=stripe` iframe | react-select | auto-run in frame (§16, §20) |
| Databricks | same iframe, present on load | react-select | auto-run in frame |
| Coinbase | Apply link opens `job-boards.greenhouse.io/embed/job_app?for=coinbase` in a **new tab** | react-select | auto-run, top-level `/embed/` URL |
| Lyft (careerpuck) | iframe after Apply | react-select | auto-run in frame |
| Duolingo (own renderer) | form on their domain | button + listbox | click-to-run, §21 |
| Samsara | never renders | — | not reached |
| **Ashby** (Notion ×2, Perplexity; 45 field entries) | `jobs.ashbyhq.com` | **none** — every choice is radios or checkboxes; one Location autocomplete per form | see below |

So across seven Greenhouse employers there are exactly two dropdown widgets
(react-select, Duolingo's listbox), and Ashby contributes a third kind that
is not a dropdown at all: an autocomplete whose suggestions come from a
remote geocoder about 2 s after typing.

**What the Ashby measurement actually found (bigger than dropdowns).**

1. Every Ashby question sits in a `div[class*="fieldEntry"]` whose first
   `<label>` is the question text. The extractor never looked there, so radio
   groups shipped with blank labels and the classifier — correctly — refused
   to answer an unreadable question. Reading the container label makes
   sponsorship, prior internships, pronouns, Gender/Race/Veteran readable.
2. **The adapter was throwing those labels away.** `_group_controls` regrouped
   radios the extension had already grouped, blanked the label, and made the
   question text an option of itself. Pre-grouped controls now pass through.
3. **Checkbox options were being read as questions.** "New York, NY",
   "Billboard/Outdoor Ads" and "Infra" each carried only their option text,
   and the planner matched them to the applicant's location, heard-about
   answer and track — three wrong FILLs on one form. Checkboxes sharing a
   container are now one `checkbox-group` control (MULTI_SELECT) labelled by
   the container; a lone checkbox stays a yes/no question. The group key is
   the id prefix Ashby gives its options (`…-labeled-checkbox`).
4. The Location combobox has neither name nor id; its container label's `for`
   names `_systemfield_location`, which nothing carries. The extractor adopts
   that dangling id as the key, the classifier maps it to `current_location`,
   and the filler types, waits for suggestions, clicks the match and reads the
   input back.
5. Two label aliases were missing on Ashby's wording: "Phone" (the key is a
   UUID) and "Github Link".

**Verified end to end** on Notion's Software Engineer Intern (Winter 2027)
form with the real backend plan: v1 (before) 15 FILL of which 5 were wrong;
v2 (after) **16 FILL, 0 failed, 0 wrong**, 8 review, 1 attach, 1 unlabelled
file input skipped. Read back: text ×7, autocomplete ×1, lone checkboxes ×3,
radio groups ×5.

**Still open:** Ashby's date input rewrote "May 2028" as `05/01/2028` — a
date widget accepting free text; harmless but unverified against submission.
The profile's `engineering_track` still holds onboarding hint text
(`'Backend,' 'Full Stack'`), so the track multi-select correctly went to
review.

**A Greenhouse regression pass (Lyft 8802198002, react-select frame) found two
backend precision defects the 42-posting corpus never exercised:**

- *"Can you perform these essential functions of the job with reasonable
  accommodation?"* matched the **accommodation** alias before the **essential
  functions** one; the stored "no accommodation needs" rendered as **"No"** —
  the inverted answer, marked FILL with confidence 1.0. Alias order now puts
  the more specific phrase first (test pinned). The corrected plan answers
  "Yes" from `essential_functions`.
- *"Work Authorization"* offered three sentence-long options; the computed
  resolver returned "Yes", the plan said FILL, and the page could select
  nothing. Every computed theme composed `option.label if option else
  <fallback>`, and the fallback is right for a text input and wrong for a
  select. `_within_options` now downgrades any value outside the form's
  options to OPTION_MISMATCH, which reaches the model gate; on Lyft it
  translated to the full sentence at 0.99.

Both are the failure the user reported on Duolingo in a different coat: the
table says FILL, the form says otherwise. After the fixes: 553 tests, corpus
**unchanged** at 89.1% coverage / 100% precision (381 labelled) — these
defects were invisible to it, so the corpus needs postings beyond SWE-intern
Greenhouse boards before its numbers are trusted for other forms.

**Measurement note.** The CDP tool timed out at 45 s on Duolingo and on the
Lyft frame while the fill kept running and finished. Both tabs were in a
background tab group, where Chrome throttles timers to about one per second;
a foreground tab — the real use — does not. The timeouts said nothing about
the extension.

---

## 23. Education and self-identification are API blocks, not questions — and the education block is on 63% of postings

Victor: "it works up to the education part"; and: build it for all postings,
not Duolingo, with Workday next. Measured 2026-09-27 across the 57-posting
corpus and four live forms.

**Prevalence.** `education: education_required` on **36/57** postings
(Coinbase, Databricks, Duolingo, Robinhood, Scale AI, Stripe, Verkada);
`demographic_questions` on **12/57** (Duolingo, Robinhood, Samsara, Stripe);
`compliance` (EEOC) on 27/57. Neither block appears under `questions`, which
is why the plan never mentioned them.

**Education.** The API describes the block only by that mode flag. The
board's public `/education/degrees` (10 entries) and `/education/disciplines`
(73) endpoints hold the vocabularies; schools are a typeahead against
`/education/schools?term=`. Decisions:

1. The service worker fetches degrees and disciplines alongside the job and
   attaches them as `education_options`; the adapter synthesises
   `educations[0].school_name_id / degree_id / discipline_id /
   end_date.month / end_date.year` as CORE fields (required when the mode
   says so). Start date is not synthesised: neither measured form renders it.
2. The profile keeps what it already had — `degree: "BS Computer Science"`,
   `graduation_date: "May 2028"`, `university` — and `Memory.lookup` derives
   `degree_type` (in Greenhouse's own words, so "Bachelor's Degree" matches
   exactly), `discipline`, `graduation_month`, `graduation_year`. Nothing new
   to onboard; anything that does not parse goes to review.
3. Two renderers, two DOMs. The standard renderer (`job-boards.greenhouse.io`
   and its embed frame — the majority) names the controls `school--0`,
   `degree--0`, `discipline--0`, `end-year--0`, and they are **async
   react-selects** whose `props.options` stay empty until loaded; typing into
   them from a script shows nothing. Calling the `loadOptions` prop found up
   the fiber tree returns `{options, hasMore}` immediately, and
   `selectOption` on the result renders the value. Duolingo's renderer keeps
   the submitted names as ids on **hidden inputs** with the widget beside the
   `<id>--label` label; the filler resolves a hidden input to that widget.

   Verified: Scale AI 4730834005 (standard renderer) — **4/4 rendered
   education fields filled and read back** (Cornell University, Bachelor's
   Degree, Computer Science, 2028; no month field there). Duolingo — end date
   month and year fill through the listbox and number input; its
   School/Degree/Discipline are a custom combobox (100 preloaded `items`,
   `onSelect`) that ignores synthetic input, focus, click, React `onChange`
   and even a trusted click-and-type in this session. **Open**; those three
   are outlined amber there with the reason, on one employer.

**Self-identification.** `demographic_questions.questions` are keyed by
number and rendered as `demographic_answers.<id>`. Treated as LEGAL on the
strength of arriving in that block (like `compliance`), with the stored
answer chosen by wording — gender identity, racial/ethnic, sexual
orientation, transgender, disability, veteran — **only** for keys in the
block, so "gender pronouns" elsewhere stays what it was. Replay is exact-or-
nothing plus one unambiguous synonym pair (Male↔Man, Female↔Woman); "Asian"
against "East Asian / South Asian" and the EEOC veteran sentence against a
differently worded one go to review, which is the right answer for a
protected characteristic. Duolingo's plan: gender and disability FILL, four
review. On the standard renderer (Robinhood) the whole block sits behind a
GDPR consent checkbox — CONSENT is human-only, so the questions appear after
the applicant ticks it and runs the fill again.

**Duolingo's comboboxes, resolved.** Victor's report — "it knows what the
right answer is but does not select it … i think the extension isn't working
but it is" — pointed at two defects, one in the filler and one in what it
shows.

1. Each of those comboboxes renders **two** search inputs. The first carries
   `aria-haspopup`/`aria-controls` but has `tabindex="-1"` and never receives
   typing; the second is the one a person uses. Every scripted attempt had
   targeted the first. A trusted click showed `document.activeElement` was the
   second. The hidden-input resolver now prefers focusable, visible inputs
   (the last of them) and looks for the suggestion list through the decoy's
   `aria-controls`.
2. Even on the right input, nothing opened when the **window was not
   focused** — which is exactly the state after the popup is clicked.
   `focus()` then fires no focus events. Dispatching `focusin` by hand opens
   the widget; the filler now does so before typing.

   Verified on a fresh Duolingo page, hidden ids read back: School →
   11784602002 (Cornell University), Degree → 11786718002 (Bachelor's Degree),
   Discipline → Computer Science; month May, year 2028, gender Man,
   disability No.

3. **A known answer that cannot be written must be visible on the page.** An
   amber outline plus a console row looked like "nothing happened". Now: an
   orange note beside the field with the value to pick and the reason, a
   console warning that counts them, and the popup shows the outcome ("13
   filled · 3 known but not enterable · 2 need you") instead of "Done". The
   table's FILL column is the plan; the page is the result, and the two must
   never be allowed to disagree silently again.

4. **Select what the menu provides, or nothing.** Victor: "when you fill
   something out you must select the exact thing that the select menu
   provides." One matcher now serves react-select, listboxes, autocompletes
   and radio/checkbox groups: the option whose text is exactly the wanted
   value; failing that, a prefix match only when exactly one option has it;
   failing that, no selection, an orange note with the value and the options
   offered. The autocomplete's old "first suggestion" fallback is gone — it
   had picked "Ithaca, New York, United States" for "Ithaca, NY" by luck of
   ordering. US state abbreviations are expanded before comparing so the same
   case now matches on meaning ("ithaca new york" is a unique prefix of one
   suggestion among five).

**Toward "all postings" and Workday.** Everything above is keyed on
Greenhouse's API shapes and two renderers, not on Duolingo. What remains
Greenhouse-specific is the ATS adapter and the widget drivers; the plan
model, memory, classification and review discipline are ATS-neutral. Workday
has no public form API, a multi-page flow, and its own widget set — it will
need a DOM extractor like Ashby's plus per-page detection, and should start
the same way this did: freeze a corpus of real Workday intern postings and
measure before writing a driver.

---

## 24. Location is on every posting; the resume is stored in the extension and attached as a real upload

Victor: "now also try to do location which you have failed to do", and for
the resume: "ask the user for their resume which they will provide and
upload and that will be stored in the extension so that it can be autofilled
in applications."

**Location.** Yet another block outside `questions`: `location_questions`,
present and **required on 57/57** corpus postings — `location` (text) plus
hidden `longitude`/`latitude`. The plan had never carried it. Decisions:

1. The adapter emits `location` as a CORE field (→ `current_location`) and
   drops the two hidden coordinates: the page fills them itself when a
   suggestion is chosen (measured: Duolingo wrote `-76.5018807 /
   42.4439614` a moment after the pick).
2. Two widgets. The standard renderer's `candidate-location` is a react-select
   with **no `loadOptions`**; it fills `props.options` from a geocoder a few
   seconds after typing — focusin + native setter + `input`, then poll the
   instance. Duolingo's is its two-input combobox again (`web-ui6` decoy,
   `web-ui7` typed) around hidden `#location`, handled by the resolver from
   §23. Both offered "Ithaca, NY(, USA)" wordings; the exact-or-unique
   matcher with state expansion picked the right one on each.
3. A stored "Ithaca, NY" is enough; the geocoder's canonical form is what
   gets written, and read back.
4. Geocoders repeat entries: Scale AI offered "Ithaca, New York, United
   States" twice among six, and the exact-or-unique rule of §23.4 first read
   that as ambiguous and refused. Uniqueness is now judged on *distinct*
   texts, so identical suggestions are one answer and the first is taken.
   Verified after the change: standard renderer shows "Ithaca, New York,
   United States"; Duolingo's hidden fields read "Ithaca, NY, USA" with
   coordinates.

**Resume.** A script cannot give a file input a *path*, which is why the
plan had an `attach` state. It can build a `File` from bytes and assign a
`DataTransfer`'s files. Measured on both renderers: the page showed the
filename and fired its own presigned S3 upload (Greenhouse's bucket on Scale
AI, Duolingo's own bucket on theirs) — indistinguishable from a manual
attach. Decisions:

1. The popup asks for the resume once (file picker → bytes → base64 →
   `chrome.storage.local["firstplay.resume"]`, capped at 6 MB against the
   ~10 MB store). It never goes to the backend; the plan still carries only
   the stored path as a label.
2. The content script reads it from storage and the filler attaches it to
   every file input the plan marks `attach`, verifying `input.files[0].name`
   and counting it as filled ("attached"). With no stored resume the field
   gets the old dashed outline plus a note saying to upload it once in the
   popup.
3. Readback of the page's own confirmation is best-effort only — Duolingo
   renders the name upper-cased through CSS, so text matching is not a
   reliable oracle; the input's `files` is.

5. **Suggestions come only from the field's own list.** Victor's first real
   run after 0.4.0 filled 21 fields and failed Location with the message
   "offered: Cornell University | Bachelor's Degree | Computer Science" — the
   other comboboxes' lists. The autocomplete driver had a last-resort "any
   open listbox on the page" fallback, and it fired before Duolingo's
   geocoder answered, ending the wait with another field's options. Removed:
   the driver reads only the listbox ids its own field (or its decoy twin)
   points at, waits up to 8 s for remote geocoders, and for a hidden-backed
   field counts a pick only when the hidden value is set — the typed text
   equalling the suggestion is true by construction and proves nothing.

**Still open:** a second document (cover letter) would reuse the same path
with a second storage key; the API's `cover_letter` field is already parsed
as DOCUMENT.

---

## 25. Inapplicable follow-ups select "N/A"; a bounded model gate answers what no lookup reaches

Victor, on Duolingo's "If so, are you eligible or currently in a period of
OPT?": "it's obvious if I'm a US citizen that I would not need to care about
eligibility and the options allow NA … some agent that can think should be
allowed to exist." Then a screenshot: that field filled **YES, in green**,
and its 24-month follow-up left amber.

**Three defects, measured 2026-09-27.**

1. *The filler wrote a sibling entry.* The plan had marked the OPT question
   "not applicable — you answered 'No' above" (`satisfied_by`), but the entry
   still carried "Yes" from the work-authorisation theme, and `applyPlan`
   skipped only `skipped` entries. It wrote the leftover. Sibling entries are
   now never written (extension 0.4.3). This was a precision failure on a
   live form: the table said "answered by a sibling", the page said YES.
2. *Inapplicable ≠ blank when the form says otherwise.* `_resolve_conditionals`
   left inapplicable follow-ups empty, which is right for free text ("If yes,
   please provide details") and wrong for a required select offering NA.
   Corpus: 19 conditional fields, 5 fields with an N/A-type option, 3 both —
   all three required. An inapplicable follow-up now selects the form's N/A
   option when one exists, deterministically.
3. *"After the OPT…" has no "if so".* Nothing conditional in its wording,
   no theme, no stored answer — only a person who knows the applicant is a US
   citizen can answer it. That is the agent Victor asked for, and it is built
   as Jev's shape rather than a free-form LLM: a **bounded choice over the
   form's own options**, state = the applicant's non-protected profile plus
   the answers already settled on this form, criteria = the options plus
   `unsure`, accepted only at ≥0.85 as MODEL_DECISION (so it is labelled and
   measured like every model output), 0.5–0.85 pre-filled but amber. It runs
   **last**, only over single-choice SCREENING fields still marked review.
   LEGAL, CONSENT and free-text fields never reach it; the protected section
   and contact details are never in its state (tested).

   The state also carries one *derived* note, computed in code from
   citizenship: "US citizen: F-1/CPT/OPT/STEM-OPT/H-1B/TN/sponsorship do not
   apply." Without it the follow-up came back NA at 0.81–0.83 — right, but
   under the bar; with it, 0.89. Stating a consequence a person draws
   instantly is not adding a fact.

**Measured.** Duolingo: both OPT questions FILL "NA" (first from rule 2,
second from the gate at 0.89). Corpus (42 postings, 1,087 fields after the
new blocks): the gate filled **13** fields at ≥0.85 and pre-filled 9, one
call per form, **$0.0013 total**. Every one of the 13 was checked against its
option list: "Are you located in Qatar / London?" → No; "attending a
university in Mexico?" → No; Figma "How did you connect with us?" → Other
(options: FigFest, partnership, on-campus, virtual, Other; stored answer is
"Careers Website"); Lyft "Do you currently reside in commutable proximity…" →
"I am willing to relocate before starting employment." (applicant in Ithaca,
relocation Yes). Coverage 89.1% → **90.4%** (859/950 fillable). The 13 are
unlabelled until Victor labels them; precision on the labelled 381 is
unchanged at 100%.

**Rule adopted.** Model judgement is allowed exactly where the answer is
determined by the profile and the question is a bounded choice — never for
protected characteristics, consent, or prose — and every such answer is
counted, labelled and priced. "An agent that can think" here means a
calibrated choice with a refusal option, not a writer.

---

## 26. Where internships actually are, and how the engine is judged at scale

Victor: run this across thousands of internships, provide the results to
score, then expand to Workday and company portals. And, rightly: "I don't
know if I can trust you running it versus running it in the browser — it's
like testing vs real life."

**Where the postings are (measured 2026-09-27).** SimplifyJobs' Summer-2026
list, 16,933 entries, 4,516 active, 3,206 engineering-ish by title:

| ATS | active | engineering-ish |
|---|---|---|
| Workday | 1,691 (37%) | 1,128 (35%) |
| company portals / other | 973 (22%) | 766 (24%) — TikTok/ByteDance, Tesla, AMD, L3Harris, Qorvo, Eightfold… |
| Greenhouse | 666 (15%) | 469 (15%) |
| Oracle / Taleo | 341 | 221 |
| Ashby | 287 | 226 |
| iCIMS | 190 | 118 |
| Lever | 107 | 83 |
| SmartRecruiters | 98 | 76 |
| Google / Apple / Microsoft / Amazon / Meta own portals | 63 | — |

Greenhouse + Ashby, the two launch targets, are 22% of the market. Workday
alone is larger than both combined, so it is the next platform, and it will
need what Greenhouse never did: account creation per employer (the
applicant's, never the tool's), a multi-page flow with its own widget set,
and no public form API — a DOM extractor like Ashby's, measured first.

**Two kinds of evidence, kept apart.**

1. *Deciding* — what to answer. The extension sends the Greenhouse payload
   and the profile to `POST /api/autofill/plan`; the same call can be made
   headlessly for thousands of postings, and every FILL/REVIEW decision is
   identical to what the extension would receive. From the 205 board tokens
   in those listings, 190 answered; they hold **1,269 intern postings**,
   which reduce to **578 unique forms** once identical question sets are
   removed. Frozen to `~/.config/firstplay/corpus-large` with each board's
   education vocabularies attached, exactly as the service worker attaches
   them. Deterministic pass: 6,658 fields filled, median 12 per form, 5,957
   left for review, **85 distinct (question, answer) pairs** — the unit
   Victor scores, each carrying how many postings it covers. The model pass
   follows and is reported in §27.
2. *Filling* — whether the page takes it. Nothing headless proves this, and
   every filler defect found today (a sibling written as YES, a decoy input,
   a fallback that read another field's list) lived here. It is judged only
   by the installed extension running on real postings in Victor's Chrome:
   a stratified sample across renderers (hosted boards, embed frames,
   employer renderers like Duolingo, Ashby), the extension's own
   `applied:` line read back per posting, screenshots kept. The sample is
   small by construction because it is expensive; the headless run is large
   because it is cheap. Neither substitutes for the other.

**Why distinct decisions, not fields.** 6,658 fills are not reviewable;
85 rows are. Precision is then computed per fill, weighting each verdict by
how many postings the decision covered, so one wrong row costs what it
would cost in the world.

---

## 27. The scale run: 576 forms, 9,386 fills, 983 decisions to score, ten cents

Measured 2026-09-27 with `python -m app.autofill.scale` over the 578 unique
forms of §26 (two forms failed and are counted below).

| | deterministic | with model gates |
|---|---|---|
| forms planned | 578 | 576 |
| fields filled | 6,658 | **9,386** |
| median filled / form | 12 | **17** |
| fields left for review | 5,957 | 3,121 (median 5 / form) |
| distinct (question, answer) pairs | 85 | **983** |
| model-decided fills | 0 | 825 (254 distinct) |
| Jev calls / cost | 0 | 802 calls, 2,662 questions, **$0.10** |

The 983 distinct decisions are on the scorecard for Victor, each weighted by
the forms it covers, so precision is computed per fill, not per row.

**Seen before scoring, and fixed:**

1. *"Select your anticipated master's degree graduation date" → 05/2028 at
   0.99.* The applicant is pursuing a bachelor's; the question presupposes a
   degree he is not taking. The gate's state now carries a derived note from
   `degree_type` — "Bachelor's only; questions presupposing another degree
   level do not apply" — the same device that settled OPT for a citizen.
   Unlabelled rows of this shape stay on the scorecard for him to confirm.
2. *Rothesay Graduates:* a university list of 552 options failed the whole
   form, because a Jev Choice takes at most 255. Both gates now skip a field
   with more than 254 options and leave it for review.

**What the remaining 3,121 reviews are.** 1,695 "nothing stored answers
this" — bespoke questions, the long tail; 284 human-only by class (consent,
attestations); 291 stored answers that match no option unambiguously; 89
onboarding answers not yet given; 198 self-identification questions
(sexual orientation, transgender, personal preferences) the profile does
not hold. The first bucket is where any further coverage lives, and it is
what the scorecard's "Ask me" verdicts will refine.

**Real-browser tier (pending).** A stratified sample is drawn — 14 hosted
postings on 14 boards, 8 employer-hosted pages — to be run by the installed
extension with its `applied:` line and a screenshot per posting. It waits
on a foreground Chrome window.

---

## 28. Speed is a requirement: where the seconds went, and the budget that stops them coming back

Victor: "the time it takes to choose items is slow … if it is slower than by
hand then what is the point." Measured 2026-09-28 before changing anything.

**Where the time was.** Fetching the posting and its education vocabularies:
0.4 s. Building the plan on the backend: **10–20 s — with zero model calls
counted.** The server log had the cause: every Jev call in that server
process was hitting the 10 s read timeout ("The read operation timed out")
and the endpoint fell back deterministically after each one, up to three
times per plan. A fresh process reached Jev in 0.24–0.39 s in the same
minute, and after a restart the same plans took **1.3 s (Scale AI), 0.9 s
(Gallup), 4.6 s (a cold posting, three sequential calls), 0.01 s
(repeat)**. The process was sick; the design let a sick model cost 20 s.

**Decisions.**

1. *A 4 s per-call budget* (`JEV_TIMEOUT_SECONDS`, was 10). A healthy call
   is 0.3–3 s; anything slower is not worth waiting for on a form.
2. *A circuit breaker per plan.* After one transport failure the remaining
   gates are skipped and the plan finishes deterministically; the response
   says `model_skipped: true` and carries `elapsed_ms`, and the extension
   prints both, so a slow fill is blamed correctly instead of looking like
   the extension.
3. *The plan request no longer waits for the page.* A Greenhouse plan is a
   function of the API payload and the profile, not the DOM, so the content
   script fires it the moment the posting is recognised and it runs while
   the page settles. Settling itself samples every 250 ms and stops at the
   first repeat (was 500 ms, two repeats).
4. *Re-runs are free.* The service worker keeps each plan in session storage
   under a hash of posting + profile for six hours; a second run on the same
   page (after Apply, after a reload, after fixing a field) costs 0.01 s.
   Plans where the model was skipped are not cached, so they retry.
5. *Waits are polls.* Every fixed nap in the filler (80–150 ms) is now a
   40 ms poll that exits as soon as the widget reflects the value; the
   long caps for remote geocoders (8 s) are unchanged but rarely reached.
6. *Every run reports its timing:* `page settled · plan ready (backend, or
   "from cache") · filled · total`, in the console and, as total seconds, in
   the popup — so the next slowness is measured, not felt.

**Still on the table:** the option-translation and profile-answer gates run
one after the other and are independent; running them concurrently would
take roughly a second off a cold plan. Not done until a measurement says the
cold plan is what people feel.

---

## 29. Fill in tiers: start every lookup at once, pick afterwards

Victor: "the education is still quite slow — I'm expecting 10 ms for each
option"; and: "try to do things in parallel … fill out multiple sections at
the same time." Measured 2026-09-28 before the change: the filler walked
the plan in order and *awaited each widget in turn*, so education on the
standard renderer paid three sequential network lookups (school, degree,
discipline, ~0.3–0.8 s each) and Duolingo waited for its school search and
then, separately, for its location geocoder.

**Decision — four tiers, waits overlapping instead of adding up.**

| tier | widgets | how |
|---|---|---|
| instant | text, checkboxes, radios, native selects, résumé | synchronous; no waits at all |
| react-select | Greenhouse's standard selects, incl. school/degree/discipline | every option lookup fires concurrently (pure network, no focus); each pick is a synchronous `selectOption` with a 40 ms readback poll |
| autocomplete | Duolingo's comboboxes, Ashby's Location, geocoders | **all searches are started first** (focusin + typed value), then each list is picked from; the first check is immediate, polls are 20 ms |
| listbox | button-and-menu widgets | one after another — opening one may close another |

Widgets that need focus are never driven concurrently; only their lookups
are. Measured on Duolingo with the new order: after the start pass, all four
lists (school, degree, discipline, location) were populated at once,
including the two remote ones — the waiting now happens once, in parallel.

**What stays slow, honestly.** A remote lookup costs what the network
costs; nothing local should cost more than a frame or two. Every run prints
its tier timing so the next complaint points at a number.

---

## 30. Coinbase: the speed work exposed a hydration race, and an unbounded search

Victor: "now I want this to be reproducible across many many different
Greenhouse applications … for example Coinbase doesn't work." Measured on
`job-boards.greenhouse.io/embed/job_app?for=coinbase&token=8175459`,
2026-09-28.

**What the page said.** The plan built in 0.38 s with 24 fills. On the page,
text fields filled; **every dropdown carried an orange note offering
"Afghanistan+93 | Åland Islands+358 | …"** — the phone widget's country
list. Two defects behind one symptom:

1. *Hydration race.* Since §28 the plan request leaves at first sight of the
   posting and arrives before React has hydrated the form. `widgetKind`
   recognised react-select by finding its instance through the fiber; with
   no fiber yet, the standard selects fell through to the autocomplete
   path. Fix: recognise react-select from its markup (`select__input`,
   `react-select-*` ids, the container classes) and, before the react-select
   tier runs, wait until an instance exists — bounded at 3 s, measured as
   already true by the time the first lookup returned.
2. *Unbounded list search.* The autocomplete path found a field's list by
   walking up to the nearest ancestor holding any `aria-controls`/`aria-owns`
   — nine levels, to the `<form>` with 55 inputs, whose first pointer is the
   phone picker's. Now at most three levels, and never a container holding
   other fields' inputs.

**Also measured.** Coinbase renders an employment block (`company-name-0`,
`start-date-*-0`, `end-date-*-0`) and **no education dates**, so the plan's
end-date entries are correctly "not on page" there; Scale AI renders
`end-year--0` and no month. The id table stays as it is.

**Verified after the fixes.** All three education lookups (school, degree,
discipline) prefetched in parallel in **67 ms**; each pick selected the one
option its loader returned.

**Reproducibility, as a method.** One employer's page found two defects the
previous ten did not. The browser-tier sample in §26 (14 hosted boards, 8
employer pages) is the next run, and each page's `applied:` and `timing:`
lines are the record.

---

## 31. Structural survey across ten Greenhouse boards: ids hold; two things generalise

Victor: "don't fit this for only Duolingo — the purpose is this extension
works across all Greenhouse applications." Measured 2026-09-28 on the
standard renderer for Gallup, ATOMS, Baidu, Appian, Faraday Future,
Brevium, Businessolver, SingleStore, Coinbase (and the EEO sequence on
Gallup): for every posting, the headless plan's fields were checked against
the live page — can the filler locate each one, and by what means?

**Result.** Every planned field that a page renders is located, 96% by id
and the rest by label. The misses fall into exactly two classes:

1. *Education fields a board does not render.* Discipline is absent on
   Gallup, Appian and Brevium; the end date on six of nine; Coinbase renders
   an employment block (`company-name-0`, `start-/end-date-*-0`) and no
   education dates at all. "Not on page" is the correct outcome and costs
   nothing. The id table (§23) needs no change.
2. *`race` on 7 of 9 boards.* The API lists one compliance field; the page
   renders Greenhouse's two-question EEO block — `hispanic_ethnicity` (Yes /
   No / Decline To Self Identify) first, and `race` is **mounted only after it
   is answered** (verified on Gallup: absent before, present with "Asian" among
   its options after). The filler now derives the Hispanic answer from the
   applicant's stored race — "Hispanic or Latino" → Yes, a decline stays a
   decline, anything else → No — fills it, waits for `race`, fills that. This
   is replay of the applicant's own answer, not judgement; no model sees it.

**Also seen live.** Gallup at first paint: 0 of 26 selects hydrated — the
race §30 fixed, caught on a second board.

**What the survey does not prove.** That the widgets take the values at
speed; that is the foreground browser run (§26), still pending a visible
window. The structural half is what could be measured from a hidden tab,
and it is the half that finds id and rendering variance across boards.

---

## 32. The form is the oracle: a dry-run submit after every fill

Victor: "we must always check … run it across different Greenhouse
applications, then try to submit, and if it doesn't submit figure out what
fields have been filled, what have not, whether the filled ones are correct,
and whether the unfilled ones are 'filled' with the right answer but not
selected."

**Measured on Gallup, 2026-09-28.** With every way of leaving the page
blocked, clicking "Submit application" made Greenhouse mark **71 elements
`aria-invalid`** with its own messages — "School is required.", "Country:
Select a country", "Address Line 1: This field is required" — fired no
submit event and attempted no network write. Greenhouse validates
client-side before it posts, so the attempt is a read of the form's verdict.

**Decision.** After every fill the extension performs that dry run and
prints the diff: for each field the form still wants, what the plan had
said — `FILL` (a filler defect: the value did not take), `review` (expected),
`not in plan` (a coverage gap the API did not describe, such as Gallup's
Address Line 1). The popup shows "form still wants N". The check runs in the
page's main world from the service worker, with `fetch`, `XMLHttpRequest`,
`sendBeacon`, `HTMLFormElement.submit` and the `submit` event all blocked
for the duration and restored after.

**The line.** The tool does not submit applications. Victor set that rule on
day one ("don't actually apply and submit jobs but practice and see"), and
a submission under his name is not reversible. The dry run gives the same
information without crossing it; a real submission would need him to say so
explicitly, and would still be his click.

**Method for the survey runs.** Per posting: fill → dry-run → diff, recorded
alongside the `applied:` and `timing:` lines. This is the loop that turns
"reproducible across all Greenhouse applications" from a claim into a
table.

---

## 33. The plan endpoint gets its own rate limit

Measured 2026-09-28 during the round-2 survey: the extension logged
`backend returned 429: Rate limit exceeded for LLM-backed endpoints: 30
requests per 60 minutes` on live forms — the plan endpoint shared the
limit meant for the résumé pipeline's four-call LLM chains. One plan is one
page the applicant opened, from their own machine, at most one model
round-trip and about a tenth of a cent. `RATE_LIMIT_AUTOFILL`, default 600
per hour: enough for any evening of applying and for a survey run, small
enough to stop a loop.

---

## 34. Round 2 on twelve unseen boards: what the held-out set found

Victor: "do this over and over again … the point is to not use the same
testing group." Round 1's fourteen boards are now the training set; round 2
drew twelve boards none of which had been opened before, and ran the
**installed extension** (0.4.9) on each page with its dry-run check, reading
the page and the console afterwards. First three boards, 2026-09-28:

| board | applied | timing | form still wants | new defect |
|---|---|---|---|---|
| Mill | 6 filled, 0 failed | 1.8 s total, fill 0.1 s | **Country** (`#country`, beside Phone) | coverage: a required select no API block describes |
| Chicago Trading Campus | 10 filled, **12 "not a react-select instance"**, 7 review | 8.0 s | 48 incl. school, degree, discipline, **start month/year** | hydration wait too short and only on the first select; "How did you hear" is a **fieldset of radios**; education **start dates** never planned |
| NISC | 6 filled, 6 "not a react-select instance" | 4.3 s | **0** (false negative) | in a hidden tab React never hydrated: 0 of 12 selects owned minutes later, so neither the selects nor the form's validation existed |

**Fixed from this round.**

1. `country` is synthesised for every standard form (required; answered
   from `country_of_residence`, derived: an explicit `country`, else a US
   state in `current_location` → United States; else review).
2. `educations[0].start_date.month/year` are synthesised; the profile gains
   `education.start_date` (onboarding: "August 2024"); absent on most boards,
   required on some.
3. The filler waits for **each** select's React instance, up to 20 s, and
   treats a `<fieldset>` of radios/checkboxes as a choice group.

**A fact about hidden tabs, recorded.** Greenhouse's standard form does not
hydrate while the tab is hidden. In that state the selects cannot be driven
and a dry-run submit validates nothing — the "still wants 0" on NISC was the
un-hydrated form, not a clean form. The extension now says so rather than
guessing; the survey itself needs the tab visible for the select and
validation tiers, exactly as §26 said.

**Method note.** Each round: new boards only; the extension as installed;
`applied:` / `timing:` / `form wants:` lines as the record; defects
diagnosed and fixed before the next draw. Rounds continue until a draw finds
nothing.

---

## 35. The select driver never ran where it could work: isolated world vs. page world

Round 2, Clockwork, 2026-09-28. The extension's notes said "not a
react-select instance" on School, Degree, Discipline, End month; a probe run
in the page at the same moment showed all six selects owned by React. Both
were true. Chrome runs content scripts in an **isolated world**: the DOM is
shared, but properties that page scripts attach to nodes are not — and
React's fiber (`__reactFiber$…`), which the driver walks to reach
`selectOption`, is such a property. From inside the extension the react-
select driver has therefore never worked; Coinbase's, Chicago Trading's,
NISC's and Clockwork's "known but not selected" dropdowns were all this one
cause, and §30's and §34's hydration explanations were only part of the
story (hydration is real and still waited for, but it was measured from the
page world, which the extension does not share).

Why it hid for so long: every fill verification I ran was a page-world
injection through the browser tools, and it passed. The installed
extension's own runs on standard forms were read only by their console
counts, which said "filled" for text and "known but could not be entered"
for selects — and the selects were blamed on hydration. Victor's rule — the
installed extension, on real pages, read back — is the one that caught it.

**Decision.** The fill runs in the page's world: the service worker injects
`fill.js` with `chrome.scripting.executeScript({world: "MAIN"})` and awaits
`applyPlan` there; the content script keeps extraction, the plan request and
the report. The isolated copy stays only as a fallback when injection is
refused, and says so. Duolingo's and Ashby's drivers, which never needed
fibers, are unaffected. Extension 0.4.13.

**What this changes about the numbers.** Every standard-renderer dropdown
result before 0.4.13 that came from the installed extension is void; the
page-world verifications (Figma, Scale AI, Coinbase, Gallup) stand, because
that is the world the filler now runs in.

---

## 36. Round 2 complete: twelve unseen boards through the extension itself

All twelve round-2 boards, run by the installed extension (0.4.13 from
Clockwork on — the first build whose select driver runs in the page world),
tab visible, each page's own `applied:` / `timing:` and dry-run `form wants:`
read back. 2026-09-28.

| board | filled / could-not-enter | total | the form still wanted |
|---|---|---|---|
| Mill | 6 / 0 | 1.8 s | Country → fixed (§34) |
| Chicago Trading | 10 / 12 (pre-page-world) | 8.0 s | re-run in round 3 |
| NISC | 6 / 6 (hidden tab, pre-page-world) | 4.3 s | re-run in round 3 |
| Clockwork | **15 / 1** | 4.2 s | phone-country shows "+1" for "United States +1" → readback accepts a partial display (0.4.14); Location → id fixed (0.4.15) |
| Garda | 9 / 0 | 2.8 s | Location |
| Verkada | 19 / 0 | 1.7 s | Location, start dates, an availability checkbox group (review) |
| TribalScale | 12 / 0 | — | start dates |
| Docugami | 8 / 0 | — | nothing |
| Internship List | 23 / 0 | 3.3 s | bespoke (term, duration, pay), consents |
| Vercel | 19 / 0 | 3.4 s | two essays, two attestations |
| Integra FEC | 22 / 0 | 1.9 s | start dates, **heard-about multi-select**, availability, essay |
| Apera | 10 / 0 | 1.8 s | one bespoke eligibility question |

**Found and fixed this round.** `location` never located on the standard
renderer (its control is `candidate-location`; every board above wanted it)
— an id mapping, 0.4.15. The phone-country readback. And the heard-about
multi-select: "Careers Website" now matches the one option that names the
employer's own website or careers page and no third-party site — 26 such
fields in the large corpus, 25 required.

**What remains wanted is the honest residue:** education start dates (a
profile gap — `education.start_date`), essays, attestations and consents
(human by design), and bespoke questions.

**Method.** Rounds 1 and 2 are training sets now; round 3 (ten boards never
opened: Geneva Trading, Schonfeld, Integra Interns, Virtu, Relay, Clarity,
Figure, Duolingo University, DoorDash Canada, Workshop) validates 0.4.14–15
and this round's backend changes on unseen forms.

---

## 37. Never write into a form React has not taken over

Round 3, Schonfeld, 2026-09-28, tab occluded (`visibilityState: hidden`):
the extension filled 9 fields green and noted one select as having no
React instance; a minute later the same page showed 12 of 13 selects with
instances and **no green outlines at all**. Greenhouse hydrates its form
lazily and not while the tab is hidden; the fill had landed on the
server-rendered DOM, and hydration's re-render replaced it. The earlier
20 s hydration wait only delayed the same outcome.

**Decision.** Before writing anything, the filler waits: if the document is
hidden, for it to be shown (a `visibilitychange`, not a timer — postings
opened in background tabs fill the moment they are looked at); then for the
last select on the page to be owned by React. The console says "this tab is
in the background — the fill starts when you switch to it". Extension 0.4.16.

**Measurement note for the survey.** macOS reports a window covered by
another window as hidden, so "keep the tab active" is not enough; the Chrome
window must be on screen. Rounds run under that condition only.

---

## 38. Rounds 3–5: 38 more unseen boards, one class of failure left and it is environmental

Held-out boards are drawn from the ledger (`used_boards.json`, never reused)
and run through the **installed** extension on `job-boards.greenhouse.io`,
with the page's own `aria-invalid` fields read back after the dry-run
submit. Totals for rounds 3–5 (closed postings excluded, 2026-09-28):

| | |
|---|---|
| boards run | 38 (54 logged since round 1, 2 closed since freeze) |
| fields filled green | 583 |
| "known but could not be entered" | 20, on 2 boards (Pacific Fusion 1, Lightmatter 19) |
| boards with zero entry failures | 36/38 |

Pacific Fusion's one failure was a school lookup that returned nothing for
the typed term, whereupon the driver fell back to the default A–Z page and
refused, correctly, to pick from it (fixed in 0.4.18: retry the term, never
the default page). Lightmatter loaded while the tab was hidden, flickered
visible for a moment, then was covered again: the 20 s hydration budget
was spent while Greenhouse was not hydrating, the wait gave up, and the
fill landed on the server-rendered DOM (§37's failure, through a gap in
§37's fix). Extension **0.4.19**: the budget counts only visible time; a
hidden page parks on `visibilitychange` and resumes with its budget intact.

**What the forms still want after a fill**, by kind, across those boards:

| remaining requirement | boards |
|---|---|
| bespoke per-company question | 12 |
| essay / narrative | 9 |
| attestation | 3 |
| security clearance | 2 |
| consent / attestation | 2 |
| availability multi | 1 |
| street address (profile gap) | 1 |
| state/province | 1 |
| salary | 1 |
| pronunciation | 1 |
| duolingo account | 1 |
| competing offer | 1 |
| previously completed | 1 |
| team preference group | 1 |
| desired salary | 1 |
| class year group (derivable from start_date) | 1 |
| co-op registered | 1 |
| heard-about (no own-site option) | 1 |
| employment block: company, title, end year | 1 |
| School | 1 |
| experience checkbox group | 1 |
| preferred pronouns | 1 |
| office applying to | 1 |
| referral conditional | 1 |
| prior internship | 1 |
| heard-about | 1 |
| GPA (Undergraduate) alias gap | 1 |
| SAT/ACT | 1 |
| office multi | 1 |

None of these is a widget failure. Essays and consents are out of scope by
standing decision (§14, §21); bespoke questions ("which office", "can you
lift 50 lb", "which shifts") have no taxonomy and stay for the applicant.
Alias gaps found and closed this round: "GPA (Undergraduate)", "What is your
preferred first name", and a State select whose label carries an "if N/A
select Other" clause.

**Measurement hazard, recorded for anyone repeating this.** Two of the
38 boards were first read too early (ZipRecruiter's page is slow) and
looked like failures until a second read; a JavaScript read that waits on a
hidden tab longer than the debugger's 45 s timeout freezes the reader, not
the page. Reads wait at most 30 s and report `visibilityState` with every
count, so a "0 filled" on a hidden tab is never logged as a defect.

---

## 39. One hundred held-out Greenhouse boards

The run Victor asked for — "try and go through 100 different applications
and check if everything is filled properly for greenhouse" — finished on
2026-09-28 with the installed extension (0.4.19) and the backend at this
branch. Every board was drawn from the ledger and never reused; the form's
own validation after a dry-run submit is the oracle.

| | |
|---|---|
| boards opened | 110 |
| excluded | 9 (8 postings closed since the freeze, 1 redirected to the employer's own domain — the click-to-run case, out of scope for the auto-fill hosts) |
| **live boards measured** | **101** |
| fields filled green | 1,625 |
| "known but could not be entered" | 21 — 1 on Pacific Fusion (fixed 0.4.18), 19 on Lightmatter (fixed 0.4.19), 1 in round 4 (Coinbase-era, fixed 0.4.6) |
| boards with zero entry failures | 98 / 101 |
| boards where the form wanted nothing more after the fill | 29 / 101 |
| rounds 6–7 (38 boards, after 0.4.19) | 641 filled, **0** could not be entered |

Per round:

| round | boards | filled | could not enter |
|---|---|---|---|
| 1 | 2 | 46 | 0 |
| 2 | 12 | 182 | 1 |
| 3 | 9 | 149 | 0 |
| 4 | 17 | 252 | 1 |
| 5 | 23 | 355 | 19 |
| 6 | 35 | 593 | 0 |
| 7 | 3 | 48 | 0 |

**What the forms still wanted**, 184 requirements across the 101 boards,
classified by hand from the `form wants:` lines:

| kind | count | disposition |
|---|---|---|
| bespoke per-company question (which department, lift 50 lb, tutored a class, FINRA disclosures) | 58 | applicant; no taxonomy reaches these |
| essay / free prose | 26 | out of scope by §14 |
| consent / acknowledgement | 15 | out of scope by §21 |
| office / location preference | 14 | applicant; a preference per posting |
| availability / term (start date, duration, hours, which semesters) | 14 | **coverage candidate**: term multi-selects and preferred-start selects recur |
| profile or alias gap | 13 | 6 fixed this round (State/Province/Region, Province/State, Legal First/Last Name, required conditionals); street address and county need a stored fact |
| custom self-identification wording (gender, race, pronouns, veteran, age range, LGBTQ, household occupation) | 13 | applicant, by rule — a protected answer is replayed only on an exact match and never inferred |
| source / referral variant (career fair, campus, options without an own-site entry) | 12 | applicant |
| academic detail not in profile (SAT/ACT, GPA range, grading scale, high-school year, academic status) | 10 | **coverage candidate**: class year from the stored start date; SAT/ACT would be new facts |
| pay expectation | 5 | applicant |
| security clearance | 4 | applicant |

**Defects found in rounds 6–7 and their fixes.** Xaira's required
sponsorship conditional and Compeer's required "if yes, explain" (§38's
follow-up; PR #19), Visier's "Province/State", DRW's "Legal First Name" /
"Legal Last Name" — each an alias or binder change with a test, each
re-verified on the board that exposed it (Xaira re-run: 19 filled, the
conditional no longer wanted).

**Decision.** Greenhouse is done for this pass: the widget layer has had zero
entry failures over 38 consecutive unseen boards, and what remains is
either policy (essays, consents, protected wording) or profile content.
Next is Ashby, on the same protocol; then Workday, then Oracle.

---

## 40. Widening the profile-answer gate: multi-selects, class standing, background

After §39, 184 requirements remained across 101 boards. Sorted by hand,
about 50 were bounded decisions from facts the applicant already holds:
term availability, class standing, clearance, career fairs, prior
internships, the GPA scale. The gate of §25 could not reach them for three
reasons, none of them about the model.

| gap | example | why the gate missed it |
|---|---|---|
| multi-selects refused | General Matter "When are you available… check all that apply"; Optiver's 11 offices; Rocket Lab's clearance list | the gate accepted single-choice and boolean only |
| state too thin | Compeer "current academic status"; QuEra "highest level of education obtained"; Varda "seeking a Spring internship?" | no class standing, no today's date, no term calendar in state |
| facts absent | BTI "US government security clearance?"; Perpay "fall career fair?"; Klaviyo "prior internships"; Radix "GPA range" | not in the profile at all |

Arithmetic questions (a GPA to a bucket, a graduation year) stay in code;
they were already answered by `match_numeric_bucket` where the label was
recognised.

**Decision.** A MULTI_SELECT with at most 12 options is asked as one Noul
per option: tick at p ≥ 0.90, no at p ≤ 0.10, anything between leaves the
whole field with the applicant, carrying the partial set as a suggestion.
Batches are cut by question count, not request count. `_applicant_state`
adds `today`, class standing derived from the education dates (academic
years start in August; four years assumed when only the graduation date is
stored), an availability note that names the month each term starts, and a
whitelisted `background` dict of four new onboarding facts —
`security_clearance`, `attended_career_fair`, `prior_internships`,
`gpa_scale`. Protected and contact data still never reach the state.

**Three things went wrong on the way, each caught by the live model on the
board that exposed the question**, run in-process against the frozen posting
with per-option beliefs printed:

1. *A silent profile guessed.* Asked "would the applicant tick this option?",
   the Noul put "Never held a clearance" at 0.90 with no clearance fact in
   the profile — a plausible guess about a student, exactly on the
   threshold. The wording now makes silence a no ("if the profile says
   nothing that bears on the option, it does not apply — the applicant
   decides"); the same option fell to 0.28 and the field went to review.
2. *The note contradicted a fact.* "Any listed term is acceptable" beside an
   earliest start of May 2027 left "Spring 2027" at p = 0.50. The note now
   states the rule (a term applies only if it starts on or after the
   earliest start) and the calendar (Spring = January, Summer = May/June,
   Fall = August/September, Winter = December/January); Spring fell to 0.15
   and the field filled at 0.89.
3. *"Not Applicable" is not a proposition.* With the clearance fact present,
   every level scored ≤ 0.05 and "Not Applicable" 0.15 — a model does not
   read the opt-out as something one ticks. When every concrete option is a
   confident no and exactly one option means none, that one is chosen by
   elimination with the weakest no's confidence (0.93 on Rocket Lab).

**Before / after, real Jev, the applicant's real profile** (facts rows used a
temporary profile copy carrying the four new facts; nothing was written into
the real profile):

| board · question | before | after |
|---|---|---|
| General Matter · available terms (multi) | review | Summer 2027 · 0.89 |
| Compeer · current academic status | review | Junior · 1.00 |
| QuEra · highest level of education obtained | review | Some College, No Degree · 0.87 |
| General Matter · active clearances (multi), fact present | review | Never held a clearance · 0.92 |
| Rocket Lab · active clearances (multi), fact present | review | Not Applicable · 0.93 by elimination |
| BTI360 · US government clearance?, fact present | review | No · 1.00 |
| Perpay · fall career fair?, fact present | review | No · 0.97 |
| Klaviyo · prior internships, fact present | review | 1 · 0.94 |
| Radix · GPA range, fact present | review | 0.0 – 4.0 · 0.89 |
| Optiver · offices open to (multi) | review | review — the profile is silent on offices, and the model now says so |
| Varda · seeking a Spring internship? | review | review at 0.56 — for a term-flexible applicant with a May 2027 earliest start, a Spring 2028 role is open; the question is genuinely undetermined |

Cost: about $0.0001 per multi-select field; a plan with three of them is
still under 2 s end to end.

**Held-out check.** Ten fresh boards (round 8): 10 model decisions, every one
traceable to a stored fact (graduation year ×3, degree level, degree
subject, work-eligibility statement, education status, based in, nationality
— all from education and legal_status) and no multi-select on those forms.
One policy catch: Maven Securities' UK-worded "If you require any support or
adjustments during the recruitment process" was classified SCREENING and
the gate answered it "No" from nothing. It is disability-adjacent; it is now
a memory-only pattern and is replayed only from the stored answer.

**Still open.** The multi-select write path has not yet run through the
installed extension (the tab was hidden every time; it fills on the next
look). The four facts are not yet in Victor's profile; until he adds them
those questions stay amber, by design.

---

## 41. A plan is a function of the engine, and a fill must not outlive the worker

The first attempt to see §40's multi-select write through the installed
extension (General Matter, 2026-09-28) produced neither a fill nor a clean
failure, and the console explained both halves:

- `plan ready 2.2s (plan from cache)` — the session plan cache is keyed on
  posting and profile, so the plan served was the one built *before* the
  gate was widened. Twelve minutes of waiting tested nothing.
- `could not run the fill in the page's world (… the message channel closed
  before a response was received); falling back` and `filled 756.7s` — the
  content script asked the service worker to run the page-world fill while
  the tab was hidden; the worker's `executeScript` sat in §37's visibility
  wait, MV3 stopped the idle worker, the channel closed, and the fill fell
  back to the isolated world, where 15 selects could not be driven.

**Decision.** The backend exposes an `engine` fingerprint — a hash of the
autofill package's source — on the plan response and the health route, and
the extension's cache key carries it (0.4.20): a backend change is never
served a stale plan. The content script waits for visibility *itself* before
asking the worker, so the worker's call lasts seconds (hydration), not
minutes; and a closed channel is retried once before any fallback, because
it is the worker having been stopped, not a page problem.

**Measurement note.** The same session showed a hazard in the tooling that
had nothing to do with the product: two shell commands run concurrently
share one working directory, and a `cd` in one moved the other. A checkout
meant for the extension repository ran in the backend's. Nothing was lost
(the commit had already landed and been pushed), but every command since
names its repository explicitly.

---

## 42. Plan latency: the gates run in the same breath

Victor's target is a fill under one second. The extension's own numbers on
round 9 (0.4.20, window on screen): page settled 0.3–2.2 s, plan ready
1.0–4.5 s (backend 0.6–2.1 s), fill 0.3–1.6 s on seven boards and 14–27 s
on three. The fill outliers are being measured per tier (0.4.21) before
anything is changed there. The plan side was measured first:

| board | first visit, before | after | second visit |
|---|---|---|---|
| Truveta | 783 ms = classify 433 → answer 348 | 367 ms | 2 ms |
| DV Trading | 2,114 ms = classify 409 → match 319 → answer 1,377 | 1,294 ms | 5 ms |
| Amperesand | 689 ms = classify 349 → answer 338 | 317 ms | 2 ms |
| HPR | 590 ms = classify 258 → match 331 | 918 ms* | — |

Every millisecond of a first-visit plan is Jev round trips (230–2,400 ms
each, the spread is the network's), run one after another because each
gate consumed the previous one's output. But the profile-answer gate needs
only the form and the profile, and the option mismatches memory already
knows about need only the stored value and the option list. Both now run
concurrently with theme classification (a two-worker pool around one HTTP
client), and their results are kept for whichever fields the themes leave
open; mismatches that appear only once a theme resolves are matched
afterwards, as before. Model decisions are identical in both modes on all
four boards.

\* HPR's "after" was slower because one call took 571 ms instead of 331 —
the per-call spread is larger than the structural gain on a small form.
The structural gain is one round trip per plan; the spread belongs to the
model host and is bounded by the 4 s budget of §28.

**Where this leaves the target.** A repeat visit is instant (the session
cache, now keyed on the engine). A first visit is one Jev round trip plus
whatever the page itself takes to settle, and the request goes out at first
sight of the posting, so on most boards the plan is ready before the page
is. What remains above one second is the fill's own outliers — next.

---

## Current state

Measured against 42 unique live SWE-intern postings, 909 fields:

| | |
|---|---|
| **Coverage** | **87.2%** (650/745 fillable) — 618 filled, 32 skipped by standing decision |
| **Precision** | **100%** over 428 human-labelled fields, including 12/12 model decisions — target 99% **met** |
| **Review load** | **median 2** fields per application, mean 2.3, worst 6 |
| | 7/42 applications need no review at all; 24/42 need ≤2 |
| **Model-decided share** | 4.1% of filled values |
| **Cost** | ~$0.006 for a full 42-posting run; near zero warm, since caches persist |
| **Tests** | 553, offline, no credentials, ~13s |

The remaining 95 unfilled fields are 86 screening, 8 core (profile gaps), 1
document. Most of the 86 are bespoke per company — a track preference, an
availability confirmation — and no taxonomy reaches them.

---

## Open, with numbers attached

- **The last ~39 unmatched fields are bespoke per company** — interview-recording
  consent, salary acceptance, track preference. No taxonomy reaches them, and
  several should be reclassified as CONSENT rather than chased.
- **Essays** remain out of scope for coverage, by decision, pending a separate
  agent.
