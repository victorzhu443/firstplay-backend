# R50 engine gaps — what was verified and changed

Branch `r50/backend`. Input: the review rows from the live round of 100
software-engineering internship applications (53 Greenhouse, 47 Ashby) that
replanning with the full profile showed to be engine gaps, not profile gaps.
For the coordinator to fold into DECISIONS §58. No profile values were read
for any test or commit; every test uses a synthetic profile.

Tests: 1,210 passing before, 1,284 after (74 added, all in
`tests/test_autofill_near_miss.py` except the entry-shape tests in
`tests/test_autofill_extraction.py`). Every commit was gated on the pytest
exit code.

Corpus (class, memory key) snapshot — `/tmp/firstplay-audit/classify-stream/
snapshot.py` + `diff.py` over `corpus-large` + `corpus-ashby`, 991 forms,
3,690 distinct (label, kind, shape, option-count) rows, 21,244 instances:

| after item | moves vs previous |
|---|---|
| 1 (legal name) | 4 distinct labels / 7 instances: `('screening', None) -> ('core', 'full_name')` — "Legal Name" x3, "Full Legal Name" x2, "Full legal name:" x1, "Legal Full Name" x1 |
| 2 (month menus) | 0 |
| 3 (graduation yes/no) | 0 |
| 4 (dial codes) | 0 |

Cumulative vs baseline: exactly the 4 / 7 above; nothing missing after.
Items 2–4 change option matching, a binder helper and the plan entry, none
of which is classification, so a zero is the expected reading, not an
absence of effect.

## 1. "Full Legal Name" → `full_name` (commit `989af31`)

Verified: `classify("question_1", "Full Legal Name", TEXT)` was SCREENING
with no memory key, so the entry reached the profile gate and came back
"your profile may cover this (50%), but no single stored fact answers it".
`Memory.full_name()` composes first + last and falls back to a stored
`full_name`; `lookup("full_name")` already routes there.

Changed (`app/autofill/classify.py`): aliases `full legal name`, `legal
name`, `legal full name`, `full name legal` → `full_name`; the core name
pattern accepts a `(full|legal|full legal|legal full)? name` head and an
"as it appears / shown / written / listed / printed on|in …" tail, so "Full
Name (as it appears on your passport)" resolves after `normalize_label`
flattens the parentheses.

Near misses asserted: "Full Legal Name in Native Language" stays LEGAL
(it already was — `is_sensitive_topic` fires on "native") with no key;
"Preferred Name" stays `preferred_first_name`; "Legal First/Last Name" stay
`first_name` / `last_name`; "Name of your reference", "Reference's full
name", "Legal Name of Employer", "Name of the person who referred you"
never reach `full_name` and are not CORE. The F18 second-bare-alias guard
is unaffected: "full legal name" is not in `_BARE_CORE_ALIASES`, so it fills
even when `first_name`/`last_name` vendor fields precede it (same as "Legal
First Name" today).

## 2. Stored month-year against a month-only menu (commit `d7d51b4`)

Verified, by probing `match_option` with synthetic values rather than the
profile: "August 2027" against January…December already matched **by
accident** through the reverse-prefix pass (`"august 2027".startswith(
"august")`). What did not match: "Aug 2027" against full names, "08/2027",
"2027-08", "August 1, 2027" against abbreviations, and every month-range
menu. So the live "no option matches your stored answer" came from one of
those shapes (the stored value was not read; the task's "August 2027
shape" covers all of them).

Changed (`app/autofill/format.py`, `app/autofill/buckets.py`):
`parse_month_year` ("August 2027", "Aug. 2027", "August 1, 2027",
"08/2027", "2027-08"; not seasons, not bare years) and `month_span` (a bare
month or a month range "January–March" / "Nov to Feb", else None). In
`match_option`, right after the §53 year-only pass (kept verbatim): when
any option is a month or month range, the stored month is the answer if
exactly one option holds it; two overlapping ranges holding it return
None (review). Options with a year or any other words are not month
options, so the pass does not run on them.

Also closed, found while probing and squarely in the same domain: a
stored "Aug 2027" against a menu of month-years ("June 2027 / July 2027 /
August 2027") selected **"June 2027"** — `match_term_bucket` reads both
sides as "summer 2027" and takes the first exact term. A new pass before
the term buckets matches a month-year menu by (month, year) or returns
None; season menus ("Spring 2028") keep the bucket arithmetic (asserted).

Near misses asserted: "Summer 2027", "2027", "ASAP", "N/A", "Immediately",
"Fall 2027" select no month; "May 2028" on a Yes/No menu or a text
"Graduation Date" is unchanged; year-only menus still match by year.

## 3. Graduation yes/no computed from `graduation_date` (commit `795a72b`)

Verified: "Will you be graduating in 2028?", "Are you graduating in
December 2027 or May 2028?" and "If you are in a Bachelor's program, do you
graduate by June 2027?" all classified UNKNOWN (0.0) on the deterministic
binder; the GRADUATION_DATE pattern needs `graduat… date|month|year|when|
expect`. "Will you have graduated before the start date?" *did* land on
GRADUATION_DATE and went STORED → "May 2028" on a Yes/No → OPTION_MISMATCH
→ the option-translation gate (a model asked to turn a date into Yes/No).

Changed (`app/autofill/binder.py`): `_graduation_by_date_answer`, a helper
in the same `or` chain as `_class_standing_answer` and
`_based_in_country_answer` (so it runs before the theme router, binder or
not). On a BOOLEAN or two-way yes/no select whose label asks about
graduating and names a year, a month-year or a set of them — plainly or as
a bound (by / before / prior to / no later than / after / later than / no
earlier than / between) — the stored date is compared at the granularity
the question uses and `option_for_bool` picks the option. Reason:
"computed: you graduate May 2028".

Review (value None, reason stated), never a guess, when: the question
refers to a date it does not state ("before the start date", "by the time
the internship begins"); the stored date is a bare year and the test is at
month level in that year; the question is conditional on a degree level
the applicant is not in ("If you are in a Bachelor's program…" for an MS;
"with a Master's degree by 2028" for a BS); or two bounds do not compose
("before May 2027 or after May 2029").

None — the existing paths untouched — when no date is named ("Have you
graduated?": the class-standing logic computes "graduated" for the model
note but does not answer that question deterministically today, so it was
left to the existing path rather than widened); for a high-school,
bootcamp or ceremony graduation; for "graduate program" questions, which
are the GRADUATE_PROGRAM theme's (the first draft intercepted "Are you
enrolled in a graduate program through 2028?" and the suite caught it);
for a three-way select, a date menu or a text control; with no stored
date. End-to-end: "What is your expected graduation date?" and
"Graduation Date" (text) still fill "May 2028".

Not done as a `QuestionTheme`: the sibling computed yes/no helpers are
functions, the theme table is for the model-classified path, and a theme
would also need a THEME_CRITERIA entry that Jev is asked about. A helper
is cheaper and runs even with `binder=None`.

## 4. `iso2` / `dial_code` on the country entry (commit `1d6aa64`)

Verified: Greenhouse's synthetic `country` field (`greenhouse._country_field`)
has no options at extraction time, so the dial code cannot live on the
`FormField`; it depends on the stored country and belongs on the plan
entry the extension reads (`FillPlanEntry`, serialised by `plan.model_dump`).

Changed: `format._COUNTRY_CODES` (canonical name → (iso2, dial)) covering
every country `themes.country_in_text` knows (US, CA, UK→**GB**, IE, DE, IN)
plus MX, FR and the common others (ES, IT, NL, BE, CH, AT, PT, SE, NO, DK,
FI, PL, TR, IL, AE, AU, NZ, SG, CN, JP, KR, TW, HK, PH, VN, PK, BD, NG, ZA,
BR, AR, CO, CL); `country_codes(value)` goes through `canonical_country`,
so "USA", "U.S.", "UK", "South Korea", "The Netherlands" resolve.
`FillPlanEntry.iso2` / `.dial_code` (Optional, default None, older clients
ignore them). `binder._mark_country_codes` runs after `_mark_missing_facts`
and **before** the model gates, so the codes come from the stored country,
not from an option label a gate translates it to. It marks only the
Greenhouse `country` key, a custom field whose memory key is
`country_of_residence`, or an entry on the COUNTRY_OF_RESIDENCE theme.
`value` is unchanged. The UK's ISO code is GB (the task's example was "US";
themes uses "UK" internally and that was left alone).

Asserted: the Duolingo-like payload's country entry is ("United States",
"US", "+1") from `current_location: "Ithaca, NY"` and ("Canada", "CA",
"+1") from `country: "Canada"`; "Narnia" keeps its value with no codes; no
other entry on the form carries codes; "Country of citizenship", "Are you
authorized to work in the United States?" and "Current Location" never
acquire them.

## Left alone, and why

- `themes._COUNTRY_PATTERNS` and `binder._COUNTRY_NAMES` still know six
  countries; widening them is a theme-precision change with its own
  near-miss list and not part of this round.
- "Summer 2027" against month-years picks "June 2027" (first month of the
  season) via the term buckets — pre-existing, arguably intended, not
  changed.
- `Memory._month_of` matches any three-letter prefix ("Mar" in "Market
  St") — pre-existing; the graduation helper only reads it from
  `education.graduation_date`, where it is safe.
- "Have you graduated?" — see item 3.
- `docs/DECISIONS.md` untouched per instruction.
