"""
Resolution pipeline: a form plus a memory becomes a FillPlan.

Five gates, cheapest first, and the ordering is the whole efficiency argument:

    1. memory, by exact ATS key or normalised label alias   0 cost, 0 latency
    2. theme classification                                 cached, else a model
    3. computed themes                                      exact, in code
    4. stored themes                                        0 cost
    5. anything left                                        review

Measured on 150 live postings, gate 1 alone answers 67.4% of fields, so the
model is reached by a minority of a minority. `DeterministicBinder` below closes
gate 2 with high-precision patterns and no model at all; it exists so that the
Jev binder added later has a measured baseline to beat, rather than being
adopted on the assumption that it helps.
"""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from app.autofill.format import OPTION_MISMATCH, match_option, option_for_bool
from app.autofill.memory import Memory, Resolution
from app.autofill.schema import FieldClass, FieldKind, FillSource, FormField, FormSchema
from app.autofill.themes import (
    QuestionTheme,
    Resolver,
    THEME_MEMORY_KEYS,
    THEME_RESOLVERS,
    country_in_label,
)

#: Confidence at or above which a theme classification may fill without asking.
#: Chosen by cost asymmetry rather than convention: a misclassified theme is a
#: confident wrong answer submitted under the applicant's name, where a
#: needless review costs one glance. Jev's default 0.5 is the right default and
#: the wrong setting here.
AUTOFILL_CONFIDENCE = 0.85

#: A "check all that apply" question is asked as one yes/no per option; more
#: than this many options is a list (offices worldwide, every clearance level)
#: that the applicant scans faster than a model can be trusted on.
MULTI_SELECT_OPTION_CAP = 12

#: Ask the profile-answer gate in the same breath as theme classification.
#: Measured on three unseen boards (Truveta, DV Trading, Amperesand): a
#: first-visit plan was 0.7–0.95 s, all of it two or three Jev round trips
#: of 230–465 ms run one after another. The answer gate needs only the form
#: and the profile, so it runs concurrently with classification and its
#: results are kept for whichever fields the themes leave for review.
SPECULATE_ANSWERS = True

#: Below this, do not even show a suggestion.
SUGGEST_CONFIDENCE = 0.50


class FillPlanEntry(BaseModel):
    """One field's outcome, ready for a review UI."""

    field_key: str
    label: str
    value: Optional[str] = None

    #: Option labels to select, for a multi-select field.
    values: List[str] = Field(default_factory=list)

    source: FillSource
    confidence: float = 1.0
    needs_review: bool = False
    #: The form's own required flag, so a client can check "still wants"
    #: without asking the page (Ashby validates only on its server, §47).
    required: bool = False
    reason: Optional[str] = None
    theme: Optional[str] = None

    #: Set when a standing decision says to leave this blank.
    skipped: Optional[str] = None

    #: Set for a file upload the profile can name but no script may perform.
    #: Browsers forbid setting `input[type=file]` programmatically, by design,
    #: so a résumé is always attached by hand. Reported as its own state rather
    #: than as "filled" — a green outline over an empty upload would be a lie —
    #: and rather than as "needs review", since the tool knows exactly which
    #: file; only the last click is the applicant's.
    attach: Optional[str] = None

    #: Set when another control on the same question already answers it —
    #: Greenhouse offers "Resume/CV" as a file upload *and* a textarea, and
    #: supplying either satisfies the question. Such a field carries no value
    #: and needs no action, which is a third state distinct from both "filled"
    #: and "needs review"; without it, 93 already-answered controls across 57
    #: postings scored as coverage misses.
    satisfied_by: Optional[str] = None


class FillPlan(BaseModel):
    """Everything proposed for one application.

    Never submitted automatically. The applicant reviews, corrects and submits;
    each correction becomes a remembered answer and therefore a labelled
    example for whatever classifies themes next time.
    """

    posting_id: str
    company: Optional[str] = None
    title: Optional[str] = None
    entries: List[FillPlanEntry] = Field(default_factory=list)

    def filled(self) -> List[FillPlanEntry]:
        return [
            e for e in self.entries
            if (e.value is not None or e.values) and not e.needs_review
            and e.attach is None
        ]

    def to_attach(self) -> List[FillPlanEntry]:
        """Files the applicant attaches themselves; the tool names which."""
        return [e for e in self.entries if e.attach is not None]

    def satisfied(self) -> List[FillPlanEntry]:
        """Answered by a sibling control; nothing to type, nothing to review."""
        return [e for e in self.entries if e.satisfied_by is not None]

    def skipped_entries(self) -> List[FillPlanEntry]:
        """Left blank by a standing decision."""
        return [e for e in self.entries if e.skipped is not None]

    def for_review(self) -> List[FillPlanEntry]:
        return [
            e for e in self.entries
            if e.satisfied_by is None and e.skipped is None and e.attach is None
            and (e.needs_review or (e.value is None and not e.values))
        ]

    def summary(self) -> Dict[str, int]:
        return {
            "total": len(self.entries),
            "filled": len(self.filled()),
            "satisfied": len(self.satisfied()),
            "skipped": len(self.skipped_entries()),
            "attach": len(self.to_attach()),
            "review": len(self.for_review()),
        }


# --- theme classification ----------------------------------------------------

#: High-precision patterns for the largest themes, most specific first. Each was
#: written against the measured label corpus, and `test_autofill_binder.py`
#: asserts precision on it — a pattern that fires on the wrong theme produces a
#: wrong answer, so coverage is worth less than precision here.
_THEME_PATTERNS: Tuple[Tuple[QuestionTheme, "re.Pattern"], ...] = (
    # Academic first: on intern forms these are the highest-frequency themes and
    # their wording is unusually consistent.
    (QuestionTheme.GRADUATION_DATE,
     re.compile(r"\b(graduat\w*)\b.{0,30}\b(date|month|year|when|expect)"
               r"|\bwhen do you graduate|\banticipated graduation", re.I)),
    (QuestionTheme.GPA, re.compile(r"\bgpa\b|\bgrade point average\b", re.I)),
    (QuestionTheme.CURRENT_UNIVERSITY,
     re.compile(r"\b(current (university|college|school)|which (university|college|school)"
               r"|school name|university name)\b", re.I)),
    (QuestionTheme.STUDYING_IN_COUNTRY,
     re.compile(r"\b(attending|enrolled (at|in)|stud(y|ying)|university|school)\b"
               r"[^?]{0,40}\bin\s+(the\s+)?(canada|mexico|united states|us|uk|"
               r"united kingdom|india|germany|ireland|singapore)\b", re.I)),
    # Before ENROLLMENT_STATUS: "currently enrolled in a Masters or PhD
    # Program" matches both, and the degree-level reading is the specific one.
    (QuestionTheme.GRADUATE_PROGRAM,
     re.compile(r"\b(master'?s?|phd|ph\.d|doctoral|doctorate|graduate (program|programme|"
               r"degree|student))\b", re.I)),
    (QuestionTheme.ENROLLMENT_STATUS,
     re.compile(r"\b(currently (enrolled|attending)|year (of study|in school)"
               r"|are you a (current )?student)\b", re.I)),
    (QuestionTheme.DEGREE_PROGRAM,
     re.compile(r"\b(major|field of study|degree (program|programme|type)"
               r"|what degree|area of study)\b", re.I)),
    # Before INTERNSHIP_TERM: "I confirm my availability for a Summer 2027
    # (May/June starts)" names a season but asks yes/no, not which.
    (QuestionTheme.INTERNSHIP_AVAILABILITY,
     re.compile(r"\bi confirm my availability\b|\bare you available for\b"
               r"|\bconfirm .{0,20}availability\b|\bflexibility to consider\b", re.I)),
    (QuestionTheme.INTERNSHIP_TERM,
     re.compile(r"\b(winter or summer|summer or winter|internship (term|season|cohort)"
               r"|cohort dates|prefer a (winter|summer))\b", re.I)),
    (QuestionTheme.ENGINEERING_TRACK,
     re.compile(r"\b(type of engineering|which (track|team)|most excited to do"
               r"|engineering work)\b", re.I)),
    (QuestionTheme.POSTAL_CODE, re.compile(r"\b(zip|postal)\s*/?\s*(code)?\b", re.I)),
    (QuestionTheme.COUNTRY_OF_RESIDENCE,
     re.compile(r"\bcountry (where|in which) you (currently )?(reside|live)"
               r"|\bcountry of residence\b", re.I)),
    (QuestionTheme.AGE_ELIGIBILITY,
     re.compile(r"\bat least \d+ years? of age\b|\bare you (18|over 18)\b", re.I)),
    (QuestionTheme.CURRENT_JOB_TITLE,
     re.compile(r"\b(current|previous).{0,12}\bjob title\b|\byour (job )?title\b", re.I)),
    (QuestionTheme.CONTACT_CURRENT_EMPLOYER,
     re.compile(r"\bmay we contact\b|\bcontact your (current|previous)\b"
               r"|\bpermission to contact\b", re.I)),
    (QuestionTheme.CURRENT_EMPLOYER,
     re.compile(r"\b(who is your )?(current|previous).{0,12}\bemployer\b", re.I)),
    (QuestionTheme.VISA_SPONSORSHIP, re.compile(r"\bsponsor", re.I)),
    (QuestionTheme.WORK_AUTHORIZATION,
     re.compile(r"\b(authoriz\w*|entitled|eligible)\b[^?]{0,40}\bwork\b", re.I)),
    (QuestionTheme.INTERVIEWED_HERE_BEFORE, re.compile(r"\binterview", re.I)),
    # Lookaheads rather than a sequence: real forms write both "worked for X
    # before" and "have you previously worked for X", and an ordered pattern
    # caught only the first — 56 instances across 149 postings went to review
    # for word order alone. "worked" is past tense deliberately, so "open to
    # working in-person" does not match.
    (QuestionTheme.WORKED_HERE_BEFORE,
     re.compile(r"(?=.*\b(?:worked|employee|employed|contractor|intern(?:ed)?)\b)"
               r"(?=.*\b(?:before|previously|previous)\b)", re.I)),
    (QuestionTheme.CURRENT_STATE, re.compile(r"\b(state or province|which state)\b", re.I)),
    (QuestionTheme.WORK_ADDRESS, re.compile(r"\baddress\b", re.I)),
    (QuestionTheme.IN_OFFICE_TOLERANCE,
     re.compile(r"\b(in[-\s]?person|in[-\s]?office|days?\s*/\s*week|hybrid)\b", re.I)),
    (QuestionTheme.WILLING_TO_RELOCATE, re.compile(r"\brelocat", re.I)),
    (QuestionTheme.CURRENT_LOCATION,
     re.compile(r"\b(currently (located|based|live)|where .{0,30}"
               r"(based|work from|intend to work))", re.I)),
    (QuestionTheme.HEARD_ABOUT, re.compile(r"\bhear about\b", re.I)),
    (QuestionTheme.EARLIEST_START,
     re.compile(r"\b(earliest|notice period|start date|when .{0,20}start)\b", re.I)),
    (QuestionTheme.TIMELINE_NOTES,
     re.compile(r"\b(deadline|timeline consideration)", re.I)),
    (QuestionTheme.PERSONAL_PREFERENCES,
     re.compile(r"\b(personal preferences|pronounce)\b", re.I)),
)


class DeterministicBinder:
    """Theme classification by pattern, with no model and no network.

    The baseline. Its patterns are high precision and deliberately incomplete:
    an unmatched label returns UNKNOWN at zero confidence and the field goes to
    review, which is the correct outcome for a question nothing recognises.
    """

    name = "deterministic"

    def classify_themes(
        self, labels: List[str]
    ) -> Dict[str, Tuple[QuestionTheme, float]]:
        out: Dict[str, Tuple[QuestionTheme, float]] = {}

        for label in labels:
            out[label] = (QuestionTheme.UNKNOWN, 0.0)
            for theme, pattern in _THEME_PATTERNS:
                if pattern.search(label or ""):
                    # 1.0 because a pattern either matched or did not; there is
                    # no calibrated belief here, and pretending otherwise would
                    # make the threshold meaningless.
                    out[label] = (theme, 1.0)
                    break

        return out


# --- resolution --------------------------------------------------------------

def _within_options(resolution: Optional[Resolution], field: FormField) -> Optional[Resolution]:
    """Refuse to call a value filled when the form offers options it is not one of.

    Every computed theme composes `option.label if option else <fallback>`, and
    the fallback is right for a text input but wrong for a select: on a live
    Lyft posting "Work Authorization" offered three sentence-long options, the
    resolver produced "Yes", the plan said FILL, and the page could select
    nothing. Marked OPTION_MISMATCH instead, the entry reaches the model gate
    that translates stored answers into a form's wording — or review.
    """
    if resolution is None or resolution.needs_review or resolution.skipped:
        return resolution
    # A yes/no control takes yes or no and nothing else. Color Health (Ashby)
    # asks "Are you based in the San Francisco Bay Area?" as a checkbox; the
    # location theme handed it "Ithaca, NY" and the filler rightly refused.
    # Left for the model gate, which answers such questions from the profile.
    if field.kind == FieldKind.BOOLEAN and resolution.value is not None and not resolution.values:
        if not re.match(r"^(yes|no|true|false|y|n)$", str(resolution.value).strip(), re.I):
            return Resolution(
                field_key=field.key,
                value=None,
                source=FillSource.HUMAN,
                confidence=0.0,
                needs_review=True,
                reason="this is a yes/no question; {!r} is not an answer to it".format(str(resolution.value)[:30]),
            )
    if not field.options or (resolution.value is None and not resolution.values):
        return resolution

    labels = {o.label for o in field.options}
    wanted = resolution.values or [resolution.value]

    if all(v in labels for v in wanted):
        return resolution

    return Resolution(
        field_key=field.key,
        value=resolution.value if resolution.value is not None else wanted[0],
        source=resolution.source,
        needs_review=True,
        reason=OPTION_MISMATCH,
    )


def _resolve_computed(
    theme: QuestionTheme,
    field: FormField,
    memory: Memory,
    company: Optional[str],
    default_country: Optional[str] = None,
    form_remote: Optional[bool] = None,
) -> Optional[Resolution]:
    """Answer a parameterised theme from the posting plus the applicant's history."""
    if theme == QuestionTheme.WORKED_HERE_BEFORE:
        answer = memory.has_worked_at(company)
        if answer is None:
            return None
        option = option_for_bool(answer, field)
        return Resolution(
            field_key=field.key,
            value=option.label if option else ("Yes" if answer else "No"),
            source=FillSource.MEMORY,
            reason="computed from your employment history",
        )

    if theme == QuestionTheme.INTERNSHIP_TERM and field.kind == FieldKind.MULTI_SELECT:
        # The one place in the corpus where "any term works" is expressible.
        # Selecting every real cohort says exactly that; the opt-out option is
        # excluded because ticking it alongside the others is incoherent.
        if not memory.preferences.get("term_flexible", "").lower().startswith("y"):
            return None
        workable = [
            o.label for o in field.options
            if not _OPT_OUT.search(o.label)
        ]
        if not workable:
            return None
        return Resolution(
            field_key=field.key,
            values=workable,
            source=FillSource.MEMORY,
            reason="you are flexible about terms, so every workable cohort is selected",
        )

    if theme == QuestionTheme.INTERNSHIP_TERM:
        # A preference, not a fact — so a suboptimal answer is a choice to
        # revisit in review, not a false statement. That is why a conditional
        # is acceptable here where it would not be for work authorisation.
        #
        # Deliberately one condition rather than a rules engine: remoteness is
        # the only thing this preference turns on, and it was true of 5 of 57
        # postings, none of which even asked this question.
        preferred = memory.preferences.get("internship_term")
        if form_remote and memory.preferences.get("internship_term_if_remote"):
            preferred = memory.preferences["internship_term_if_remote"]
        if not preferred:
            return None
        option = match_option(preferred, field) if field.options else None
        return Resolution(
            field_key=field.key,
            value=option.label if option else preferred,
            source=FillSource.MEMORY,
            needs_review=bool(field.options) and option is None,
            reason=("your preferred term for a remote role" if form_remote
                    else "your preferred term"),
        )

    if theme == QuestionTheme.STUDYING_IN_COUNTRY:
        asked = country_in_label(field.label)
        studying_in = memory.education.get("university_country")
        if not asked or not studying_in:
            return None
        from app.autofill.themes import country_in_text

        mine = country_in_text(studying_in)
        if not mine:
            return None
        option = option_for_bool(asked == mine, field)
        return Resolution(
            field_key=field.key,
            value=option.label if option else ("Yes" if asked == mine else "No"),
            source=FillSource.MEMORY,
            reason="your university is in {}".format(studying_in),
        )

    if theme == QuestionTheme.GRADUATE_PROGRAM:
        degree = memory.education.get("degree")
        if not degree:
            return None
        lowered = degree.lower()
        undergraduate = any(
            marker in lowered
            for marker in ("bachelor", "b.s", "bs ", "b.a", "ba ", "undergrad", "bsc")
        )
        graduate = any(
            marker in lowered
            for marker in ("master", "m.s", "ms ", "phd", "ph.d", "doctor", "msc", "meng")
        )
        if undergraduate == graduate:
            # Neither or both: not determinable from the stored degree.
            return None
        option = option_for_bool(graduate, field)
        return Resolution(
            field_key=field.key,
            value=option.label if option else ("Yes" if graduate else "No"),
            source=FillSource.MEMORY,
            reason="computed from your stored degree",
        )

    if theme in (QuestionTheme.WORK_AUTHORIZATION, QuestionTheme.VISA_SPONSORSHIP):
        general_key = (
            "work_authorization"
            if theme == QuestionTheme.WORK_AUTHORIZATION
            else "needs_sponsorship"
        )
        # The question names a country, or the posting does. Neither is
        # guaranteed: Greenhouse locations often name only a city, and 28
        # postings ask about "the country for which you applied" without saying
        # which. So an undeterminable country is normal, not exceptional.
        country = country_in_label(field.label) or default_country

        # Per-country first, then the general answer. Most applicants have one
        # status ("US citizen", so authorised everywhere they will apply) and
        # should not have to enumerate countries to get a form filled. Anyone
        # whose status genuinely differs by country sets `per_country`, which
        # takes precedence — so the convenience never overrides the specific.
        specific = None
        if country:
            specific = (
                memory.authorization_for(country)
                if theme == QuestionTheme.WORK_AUTHORIZATION
                else memory.sponsorship_for(country)
            )

        general = memory.legal_status.get(general_key)

        if specific is None and country is None:
            # Falling back to the general answer only when it cannot be
            # contradicted. If per-country answers disagree with each other,
            # "which country?" is load-bearing and guessing would put a false
            # statement about work eligibility on the form.
            per_country = memory.per_country.get(
                "work_authorization" if theme == QuestionTheme.WORK_AUTHORIZATION
                else "needs_sponsorship", {}
            )
            distinct = {v for v in per_country.values() if v}
            if len(distinct) > 1:
                return Resolution(
                    field_key=field.key,
                    source=FillSource.MEMORY,
                    needs_review=True,
                    reason="your answer differs by country and this form does not "
                           "say which",
                )

        stored = specific or general
        if not stored:
            return Resolution(
                field_key=field.key,
                source=FillSource.MEMORY,
                needs_review=True,
                reason=("you have not set this for {}".format(country)
                        if country else "you have not set this"),
            )
        option = match_option(stored, field)
        return Resolution(
            field_key=field.key,
            value=option.label if option else stored,
            source=FillSource.MEMORY,
            reason="your stored answer for {}".format(country),
        )

    return None


def _resolve_stored(
    theme: QuestionTheme, field: FormField, memory: Memory
) -> Optional[Resolution]:
    key = THEME_MEMORY_KEYS.get(theme)

    if not key:
        return None

    stored = memory.lookup(key)

    if not stored:
        return Resolution(
            field_key=field.key,
            source=FillSource.MEMORY,
            needs_review=True,
            reason="recognised as {} but you have not set it".format(key),
        )

    option = match_option(stored, field) if field.options else None

    # A multi-select answered from a single stored value selects exactly that
    # option. This is the "select all that apply" case where only one applies —
    # distinct from the cohort case, where the answer is genuinely a set.
    if field.kind == FieldKind.MULTI_SELECT and option is not None:
        return Resolution(
            field_key=field.key,
            values=[option.label],
            source=FillSource.MEMORY,
            reason="stored as {}".format(key),
        )

    if field.options and option is None:
        return Resolution(
            field_key=field.key,
            value=stored,
            source=FillSource.MEMORY,
            needs_review=True,
            reason=OPTION_MISMATCH,
        )

    return Resolution(
        field_key=field.key,
        value=option.label if option else stored,
        source=FillSource.MEMORY,
        reason="stored as {}".format(key),
    )


#: "If you answered 'Yes' to the above question, please provide details."
#: x20 across 57 intern postings — the single largest unclassifiable label, and
#: unclassifiable for a good reason: its meaning lives in the *previous* field,
#: not in its own words. No theme can ever cover it.
#: An option that means "this question does not apply to me". Measured on 5
#: of 1,087 corpus fields (Duolingo's two OPT questions, Databricks'
#: citizenship follow-up); rare, but the ones that exist are required.
_NOT_APPLICABLE = re.compile(r"^(n/?a|n\.a\.|not applicable|does not apply)\b", re.I)

_CONDITIONAL = re.compile(
    r"\bif (you |your )?(answered|selected|responded|you said)\b"
    r"|\bif (yes|so|the above|any of the above)\b"
    r"|\bif you (answered|selected) [\"'“]?yes",
    re.I,
)

#: Which answer to the preceding question makes the follow-up apply.
_CONDITION_IS_NO = re.compile(r"\bif (you )?(answered|selected)\s*[\"'“]?no\b", re.I)


#: Reason text set by the resolvers when a stored answer exists but matches no
#: option deterministically. That is the signal for the Jev pass below.
#: "None of these options work for me" — an opt-out that must never be selected
#: alongside the real choices.
_OPT_OUT = re.compile(r"\bnone of (these|the)\b|\bnot pursuing\b|\bnone apply\b", re.I)


def _resolve_option_mismatches(
    entries, form: FormSchema, binder,
    precomputed: Optional[Dict[str, Tuple[Optional[str], float]]] = None,
) -> None:
    """Ask the model which option expresses an answer the applicant already gave.

    The largest single cause of unfilled fields: 88 of 222 across 57 postings,
    and 143 of those 222 were selects. The stored answer is *right* — "Yes" — it
    simply is not the string this form uses, which writes "I am willing to
    relocate before starting employment."

    Barred from LEGAL fields. Translating a stored answer into a form's wording
    is still a model producing the value, and protected characteristics keep the
    deterministic path — the decline-variant matcher already covers them.
    """
    if binder is None or not hasattr(binder, "match_options"):
        return

    fields = {f.key: f for f in form.fields}
    requests = []
    results: Dict[str, Tuple[Optional[str], float]] = {}
    precomputed = precomputed or {}

    for index, entry in enumerate(entries):
        if entry.reason != OPTION_MISMATCH or not entry.value:
            continue
        field = fields.get(entry.field_key)
        if field is None or not field.options:
            continue
        if not field.allows_option_translation():
            continue
        # A multi-select is permitted here only because this gate is matching a
        # single stored answer to a single option — the same shape and the same
        # risk as a single-select. Choosing a *set* of options stays
        # deterministic-only, and disclosures are unreachable regardless:
        # `may_fill_from` above already bars every LEGAL field.
        request_id = "req_{}".format(index)
        if entry.field_key in precomputed:
            results[request_id] = precomputed[entry.field_key]
            requests.append((request_id, entry.value, None))
        else:
            requests.append((request_id, entry.value, field))

    fresh = [(rid, v, f) for rid, v, f in requests if f is not None]
    if fresh:
        results.update(binder.match_options(fresh))

    for request_id, _value, _field in requests:
        entry = entries[int(request_id.split("_")[1])]
        label, confidence = results.get(request_id, (None, 0.0))

        if label is None:
            entry.reason = "no option matches your stored answer"
            continue

        if confidence < AUTOFILL_CONFIDENCE:
            entry.value = label
            entry.confidence = confidence
            entry.reason = "best match for your stored answer, but only {:.0%} sure".format(
                confidence
            )
            continue

        entry.value = label
        entry.source = FillSource.MODEL_DECISION
        entry.confidence = confidence
        entry.needs_review = False
        entry.reason = "your stored answer, matched to this form's wording"


def _resolve_conditionals(entries: List[FillPlanEntry], form: FormSchema) -> None:
    """Mark a follow-up inapplicable when the question it depends on said no.

    Most of these hang off conflict-of-interest screens — "Do you have any
    familial relationships with a current employee?" followed by "If you
    answered Yes, please provide details." An applicant who answered No has
    nothing to write, so the follow-up is *answered* rather than outstanding.

    Deliberately one-directional: a preceding "Yes" leaves the follow-up for
    review, because the detail it wants is free prose that only the applicant
    can supply. Filling it would be inventing a disclosure, which is the worst
    precision failure available here.
    """
    order = [f.key for f in form.fields]
    position = {key: index for index, key in enumerate(order)}
    by_key = {e.field_key: e for e in entries}

    for entry in entries:
        if not _CONDITIONAL.search(entry.label or ""):
            continue

        index = position.get(entry.field_key)
        if not index:
            continue

        # The nearest preceding field that actually has an answer.
        previous = None
        for offset in range(index - 1, -1, -1):
            candidate = by_key.get(order[offset])
            if candidate is not None and candidate.value:
                previous = candidate
                break

        if previous is None:
            continue

        answer = str(previous.value).strip().lower()
        triggers_on_no = bool(_CONDITION_IS_NO.search(entry.label or ""))
        answered_yes = answer.startswith("y") or answer.startswith("i am")
        applies = answered_yes if not triggers_on_no else not answered_yes

        if applies:
            entry.needs_review = True
            entry.reason = "you answered '{}' above, so this needs your detail".format(
                str(previous.value)[:24]
            )
            continue

        # Inapplicable. If the form offers an option that says so, select it:
        # Duolingo's "If so, are you eligible for OPT?" is *required* and
        # offers Yes / No / NA, so leaving it blank blocks submission while
        # "NA" is the true answer. Without such an option, blank is right.
        field = next((f for f in form.fields if f.key == entry.field_key), None)
        not_applicable = next(
            (o for o in (field.options if field else []) if _NOT_APPLICABLE.match(o.label.strip())),
            None,
        )
        if not_applicable is not None:
            entry.value = not_applicable.label
            entry.values = []
            entry.needs_review = False
            entry.satisfied_by = None
            entry.source = FillSource.MEMORY
            entry.confidence = 1.0
            entry.reason = "not applicable — you answered '{}' above, and this form offers {!r}".format(
                str(previous.value)[:24], not_applicable.label
            )
            continue

        # Required, and no way to say "not applicable": the form will refuse a
        # blank. Xaira 5225658007 asks "If you answered 'No' ... will you
        # require sponsorship?" with only Yes / No and marks it required; a
        # citizen's truthful answer is the one the sponsorship theme already
        # gave ("No"), so a confident answer of the entry's own stands. With
        # no such answer, the field stays for the applicant rather than being
        # hidden as "answered by a sibling" and blocking submission unseen
        # (Compeer 5404994008: a required "If yes, please explain").
        if field is not None and field.required:
            if entry.value and not entry.needs_review and not entry.skipped:
                entry.reason = "not applicable after your '{}' above, but required — answered {!r} from your profile".format(
                    str(previous.value)[:24], str(entry.value)[:24]
                )
                continue
            entry.needs_review = True
            entry.satisfied_by = None
            entry.reason = "not applicable after your '{}' above, but this form requires an answer".format(
                str(previous.value)[:24]
            )
            continue

        entry.needs_review = False
        entry.satisfied_by = previous.field_key
        entry.reason = "not applicable — you answered '{}' to the question above".format(
            str(previous.value)[:24]
        )


#: Facts a screening question can turn on that are neither protected nor
#: contact details. Whitelisted by key so that a new fact never reaches the
#: model by accident. Counts are how often the 100-board run wanted them.
_BACKGROUND_FACTS = (
    "security_clearance",     # 4 boards: hold one? which level? eligible?
    "attended_career_fair",   # career fair / saw us on campus, 12 source variants
    "prior_internships",      # Klaviyo "how many prior internships", counts
    "gpa_scale",              # Radix "your university's GPA range"
    "current_job_title",
    "current_employer",
)

_STANDING = {
    1: "first-year (freshman)",
    2: "second-year (sophomore)",
    3: "third-year (junior)",
    4: "fourth-year (senior)",
}


def _class_standing(education: Dict[str, str], today: date) -> Optional[str]:
    """Where in a degree the applicant is, as a person would say it.

    Academic years start in August. From the stored start date when there is
    one, else from the graduation date assuming four years; "graduated" once
    the graduation month has passed. None when neither date parses.
    """
    from app.autofill.memory import _year_of, _month_of  # local: memory imports binder's schema

    start = _year_of(education.get("start_date") or "")
    grad = _year_of(education.get("graduation_date") or "")
    grad_month = _month_of(education.get("graduation_date") or "")
    academic_year = today.year if today.month >= 8 else today.year - 1

    if grad:
        grad_year = int(grad)
        months = ["January", "February", "March", "April", "May", "June", "July",
                  "August", "September", "October", "November", "December"]
        grad_month_index = months.index(grad_month) + 1 if grad_month in months else 6
        if (today.year, today.month) > (grad_year, grad_month_index):
            return "graduated ({} {})".format(grad_month or "", grad_year).replace("( ", "(")

    if start:
        index = academic_year - int(start) + 1
    elif grad:
        index = 4 - (int(grad) - academic_year - 1)
    else:
        return None

    if index < 1:
        return "not yet started (starts {})".format(start or grad)
    return _STANDING.get(index, "{}th-year".format(index))


def _applicant_state(memory: Memory, today: Optional[date] = None) -> Dict[str, object]:
    """What the model may know about the applicant when answering a question.

    Facts a screening question can turn on — legal status, education, stated
    preferences, location, employers. Never the protected section, never
    contact details: neither answers a screening question, and the first must
    not reach a model at all.
    """
    preferences = {
        k: v for k, v in (memory.preferences or {}).items()
        if v and v not in Memory.BLANK_ANSWERS and not v.startswith("'")
    }
    # Consequences a person draws instantly and a model should not have to
    # infer against the odds: citizenship settles every student/work-visa
    # question. Stated as derived notes, not as new facts.
    citizenship = str((memory.legal_status or {}).get("citizenship") or "").lower()
    notes = []
    if "citizen" in citizenship and "non" not in citizenship and "u.s" in citizenship.replace("us", "u.s"):
        notes.append("US citizen: student and work visa programmes (F-1, CPT, OPT, STEM OPT "
                     "extension, H-1B, TN, sponsorship) do not apply; questions about them are "
                     "answered 'not applicable' when offered, otherwise 'No'.")

    degree_type = memory.lookup("degree_type") or ""
    if degree_type:
        others = [d for d in ("Bachelor's", "Master's", "Doctorate / PhD", "MBA", "JD", "MD") if not degree_type.startswith(d.split(" ")[0])]
        notes.append("Currently pursuing a {} only; questions that presuppose another degree "
                     "level ({}) do not apply — answer 'not applicable' when offered, otherwise "
                     "'unsure'.".format(degree_type, ", ".join(others)))

    today = today or date.today()
    education = dict(memory.education or {})
    standing = _class_standing(education, today)
    if standing:
        notes.append("Today is {}. Class standing: {}; graduating {}. Questions about academic "
                     "status, year in school or highest degree completed follow from this "
                     "(an undergraduate has not yet obtained a bachelor's degree).".format(
                         today.isoformat(), standing, education.get("graduation_date") or "unknown"))

    term = (memory.preferences or {}).get("internship_term") or ""
    flexible = str((memory.preferences or {}).get("term_flexible") or "").lower().startswith("y")
    earliest = (memory.preferences or {}).get("earliest_start") or ""
    if term:
        # The calendar the model would otherwise have to guess at: a term is
        # available only if it starts on or after the earliest start date.
        # General Matter's "Fall 2026 / Spring 2027 / Summer 2027" sat at
        # p=0.50 on Spring until the note said when Spring begins.
        calendar = ("Term start months: Spring = January, Summer = May/June, Fall = August/September, "
                    "Winter = December/January.")
        if flexible:
            rule = ("Any term is acceptable provided it starts on or after the earliest start date{}; "
                    "a term that starts before it does not apply.".format(
                        " ({})".format(earliest) if earliest else ""))
        else:
            rule = ("{} terms only{}; options naming other terms or semesters do not apply.".format(
                term, " (earliest start {})".format(earliest) if earliest else ""))
        notes.append("Internship availability: preferred term {}. {} {}".format(term, rule, calendar))

    background = {
        key: (memory.facts or {}).get(key) or (memory.preferences or {}).get(key)
        for key in _BACKGROUND_FACTS
    }
    background = {k: v for k, v in background.items() if v and v not in Memory.BLANK_ANSWERS}

    return {
        "notes": notes,
        "today": today.isoformat(),
        "background": background,
        "legal_status": dict(memory.legal_status or {}),
        "work_authorization_by_country": dict((memory.per_country or {}).get("work_authorization", {})),
        "needs_sponsorship_by_country": dict((memory.per_country or {}).get("needs_sponsorship", {})),
        "education": dict(memory.education or {}),
        "preferences": preferences,
        "current_location": (memory.facts or {}).get("current_location"),
        "employers": [
            (e.get("name") if isinstance(e, dict) else str(e)) for e in (getattr(memory, "employers", None) or [])
        ][:12],
    }


def _model_may_answer(field: FormField) -> bool:
    """Whether the profile-answer gate may be asked about a field at all."""
    if not field.options:
        return False
    if field.kind == FieldKind.MULTI_SELECT:
        # "Check all that apply" — term availability, offices, clearances.
        # Each option is its own yes/no; the cap keeps a form's questions
        # bounded (Optiver lists 11 offices, General Matter 3 terms).
        if len(field.options) > MULTI_SELECT_OPTION_CAP:
            return False
    elif field.kind not in (FieldKind.SINGLE_SELECT, FieldKind.BOOLEAN):
        return False
    return field.allows_model_judgement()


def _answer_state(form: FormSchema, memory: Memory, earlier: List[Dict[str, object]]) -> Dict[str, object]:
    return {
        "applicant": _applicant_state(memory),
        "form": {"company": form.company, "title": form.title, "earlier_answers": earlier[:40]},
    }


def _speculative_answers(
    form: FormSchema, memory: Memory, binder, pending: List[FormField], resolved: Dict[str, "Resolution"]
) -> Tuple[
    Dict[str, Tuple[QuestionTheme, float]],
    Dict[str, Tuple[Optional[object], float]],
    Dict[str, Tuple[Optional[str], float]],
]:
    """Classify themes, ask the profile-answer gate, and translate the option
    mismatches memory already knows about — all in the same breath.

    Returns (themes, speculative answers by field key, option matches by
    field key). The answer gate's state carries the answers memory has
    already settled — the theme-resolved ones are not known yet, which is
    the price of the overlap; the confidence thresholds are unchanged.
    Option mismatches that only appear once a theme resolves are matched
    afterwards, as before.
    """
    labels = [f.label for f in pending]
    candidates = [f for f in pending if _model_may_answer(f)] if hasattr(binder, "answer_from_profile") else []
    mismatches = [
        (f, resolved[f.key].value) for f in form.fields
        if f.key in resolved and resolved[f.key].reason == OPTION_MISMATCH and resolved[f.key].value
        and f.options and f.allows_option_translation()
    ] if hasattr(binder, "match_options") else []

    if not SPECULATE_ANSWERS or not (candidates or mismatches):
        return binder.classify_themes(labels), {}, {}

    earlier = [
        {"question": (f.label or "")[:120], "answer": resolved[f.key].values or resolved[f.key].value}
        for f in form.fields
        if f.key in resolved
        and (resolved[f.key].value or resolved[f.key].values)
        and not resolved[f.key].needs_review
        and not resolved[f.key].skipped
    ]
    state = _answer_state(form, memory, earlier)
    requests = [("spec_{}".format(f.key), f) for f in candidates]
    match_requests = [("match_{}".format(f.key), value, f) for f, value in mismatches]

    with ThreadPoolExecutor(max_workers=2) as pool:
        answer_future = pool.submit(binder.answer_from_profile, state, requests) if requests else None
        match_future = pool.submit(binder.match_options, match_requests) if match_requests else None
        themes = binder.classify_themes(labels)
        answers, matches = {}, {}
        # A failed speculation costs nothing: the sequential gates still run.
        try:
            answers = answer_future.result() if answer_future else {}
        except Exception:  # noqa: BLE001
            pass
        try:
            matches = match_future.result() if match_future else {}
        except Exception:  # noqa: BLE001
            pass

    return (
        themes,
        {f.key: answers[rid] for rid, f in requests if rid in answers},
        {f.key: matches[rid] for rid, _v, f in match_requests if rid in matches},
    )


def _answer_from_profile(
    entries: List[FillPlanEntry], form: FormSchema, memory: Memory, binder,
    speculative: Optional[Dict[str, Tuple[Optional[object], float]]] = None,
) -> None:
    """Ask the model to answer, from the profile, what no lookup reached.

    Runs last, over single-choice screening questions still marked for review,
    with the applicant's non-protected profile and the answers already settled
    on this form as state. Accepted only at AUTOFILL_CONFIDENCE, as
    MODEL_DECISION, so it is labelled and measured like every other model
    output. LEGAL, CONSENT and free-text fields never come here.
    """
    if binder is None or not hasattr(binder, "answer_from_profile"):
        return

    fields = {f.key: f for f in form.fields}
    requests = []
    results: Dict[str, Tuple[Optional[object], float]] = {}
    speculative = speculative or {}

    for index, entry in enumerate(entries):
        if not entry.needs_review or entry.skipped or entry.satisfied_by:
            continue
        field = fields.get(entry.field_key)
        if field is None or not _model_may_answer(field):
            continue
        request_id = "ans_{}".format(index)
        if entry.field_key in speculative:
            results[request_id] = speculative[entry.field_key]
            requests.append((request_id, None))
        else:
            requests.append((request_id, field))

    fresh = [(rid, f) for rid, f in requests if f is not None]

    if fresh:
        earlier = [
            {"question": (e.label or "")[:120], "answer": e.values or e.value}
            for e in entries
            if (e.value or e.values) and not e.needs_review and not e.skipped and not e.satisfied_by
        ]
        results.update(binder.answer_from_profile(_answer_state(form, memory, earlier), fresh))

    for request_id, _field in requests:
        entry = entries[int(request_id.split("_")[1])]
        label, confidence = results.get(request_id, (None, 0.0))

        if label is None or label == []:
            continue

        # A multi-select comes back as the list of options that apply, with
        # the confidence of the least-decided option. Partial certainty is a
        # suggestion, never a fill: ticking two of three boxes silently is
        # worse than ticking none.
        picked = list(label) if isinstance(label, (list, tuple)) else None

        if confidence < AUTOFILL_CONFIDENCE:
            if confidence >= 0.5:
                if picked is not None:
                    entry.values = picked
                else:
                    entry.value = label
                entry.confidence = confidence
                entry.reason = "the model reads your profile as {!r}, but only {:.0%} sure".format(
                    picked if picked is not None else label, confidence
                )
            continue

        if picked is not None:
            entry.value = None
            entry.values = picked
        else:
            entry.value = label
            entry.values = []
        entry.source = FillSource.MODEL_DECISION
        entry.confidence = confidence
        entry.needs_review = False
        entry.reason = "decided from your profile and your earlier answers on this form"


def _mark_attachments(entries: List[FillPlanEntry], form: FormSchema) -> None:
    """A resolved file upload becomes an attachment instruction, not a fill."""
    kinds = {f.key: f.kind for f in form.fields}

    for entry in entries:
        if kinds.get(entry.field_key) != FieldKind.FILE:
            continue
        if entry.value is None or entry.needs_review or entry.skipped:
            continue
        entry.attach = entry.value
        entry.reason = "attach this file yourself — a browser will not let a script do it"


def _mark_satisfied_alternates(entries: List[FillPlanEntry], form: FormSchema) -> None:
    """Stop asking for a paste when the file is already attached.

    Greenhouse ships one "Resume/CV" question as two controls — `resume` (an
    upload) and `resume_text` (a textarea alternative) — and the applicant needs
    to supply only one. Scored independently, the unused alternate showed up as
    an unanswered field on every single posting: 57 resume fields and 36 cover
    letter fields across 57 postings, all of them already satisfied.

    Mutates `entries` in place, since it is a post-pass over the plan rather
    than a resolution rule.
    """
    by_label: Dict[str, List[FillPlanEntry]] = {}
    field_labels = {f.key: f.label for f in form.fields}

    for entry in entries:
        label = field_labels.get(entry.field_key, entry.label)
        # A blank label is the absence of a question, not a shared one. On a
        # real Ashby form the radio groups (no legend), a bare file input, two
        # unlabelled checkboxes and the reCAPTCHA textarea all have empty
        # labels; grouping them made a filled gender radio "answer" all of
        # them, which hid a pronoun group and credited a CAPTCHA as done.
        if not (label or "").strip():
            continue
        by_label.setdefault(label, []).append(entry)

    for label, group in by_label.items():
        if len(group) < 2:
            continue

        satisfied = next(
            (e for e in group
             if (e.value is not None or e.attach is not None) and not e.needs_review),
            None,
        )

        if satisfied is None:
            continue

        for entry in group:
            if entry is satisfied or (entry.value is not None and not entry.needs_review):
                continue
            entry.needs_review = False
            entry.satisfied_by = satisfied.field_key
            entry.reason = "already answered by '{}' on this question".format(
                satisfied.label or satisfied.field_key
            )


def resolve_form(
    form: FormSchema,
    memory: Memory,
    *,
    company: Optional[str] = None,
    binder: Optional[DeterministicBinder] = None,
) -> FillPlan:
    """Build a FillPlan for one application.

    Args:
        form: The extracted form
        memory: The applicant's stored answers
        company: The company being applied to, for the parameterised themes.
            Defaults to the form's own company.
        binder: Theme classifier. None means gates 2-4 are skipped entirely and
            anything memory cannot answer goes to review.

    Returns:
        A FillPlan. Never submitted; the applicant reviews and submits.
    """
    company = company or form.company
    entries: List[FillPlanEntry] = []

    # Gate 2 is batched: every unresolved label classified in one call, because
    # state is sent once and extra questions are close to free.
    pending: List[FormField] = []
    resolved: Dict[str, Resolution] = {}

    for field in form.fields:
        resolution = _within_options(memory.resolve(field), field)
        if resolution is not None:
            resolved[field.key] = resolution
        else:
            pending.append(field)

    themes: Dict[str, Tuple[QuestionTheme, float]] = {}
    speculative: Dict[str, Tuple[Optional[object], float]] = {}
    matched: Dict[str, Tuple[Optional[str], float]] = {}
    if binder is not None and pending:
        themes, speculative, matched = _speculative_answers(form, memory, binder, pending, resolved)

    for field in form.fields:
        resolution = resolved.get(field.key)
        theme_name = None

        if resolution is None and field.label in themes:
            theme, confidence = themes[field.label]
            # Only recorded when a classifier actually ran. "Classified and not
            # recognised" and "never classified" are different diagnostics, and
            # collapsing them hides which of the two happened.
            theme_name = theme.value

            if theme != QuestionTheme.UNKNOWN and confidence >= SUGGEST_CONFIDENCE:
                kind = THEME_RESOLVERS.get(theme, Resolver.ASK)

                if kind == Resolver.COMPUTED:
                    resolution = _within_options(_resolve_computed(
                        theme, field, memory, company, form.country, form.remote
                    ), field)
                elif kind == Resolver.STORED:
                    resolution = _resolve_stored(theme, field, memory)

                # A theme recognised but not confidently enough to act on is a
                # suggestion, not an answer.
                if resolution is not None and confidence < AUTOFILL_CONFIDENCE:
                    resolution.needs_review = True
                    resolution.confidence = confidence
                    resolution.reason = "low confidence that this asks about {}".format(
                        theme.value
                    )

        if resolution is None:
            entries.append(
                FillPlanEntry(
                    field_key=field.key,
                    label=field.label,
                    source=FillSource.HUMAN,
                    confidence=0.0,
                    needs_review=True,
                    reason="nothing stored answers this yet",
                    theme=theme_name,
                    required=field.required,
                )
            )
            continue

        entries.append(
            FillPlanEntry(
                field_key=field.key,
                label=field.label,
                value=resolution.value,
                values=resolution.values,
                source=resolution.source,
                confidence=resolution.confidence,
                needs_review=resolution.needs_review,
                reason=resolution.reason or resolution.skipped,
                skipped=resolution.skipped,
                theme=theme_name,
                required=field.required,
            )
        )

    _mark_attachments(entries, form)
    _resolve_option_mismatches(entries, form, binder, matched)
    _mark_satisfied_alternates(entries, form)
    _resolve_conditionals(entries, form)
    _answer_from_profile(entries, form, memory, binder, speculative)

    return FillPlan(
        posting_id=form.posting_id,
        company=form.company,
        title=form.title,
        entries=entries,
    )
