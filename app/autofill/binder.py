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
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from app.autofill.classify import CORE_FIELD_NAMES, FOLLOW_UP_ON_OTHER, LABEL_SCAN_LIMIT, is_follow_up, memory_key_for, normalize_label
from app.autofill.format import OPTION_MISMATCH, _as_bool, match_option, option_for_bool
from app.autofill.memory import DERIVED_SOURCES, Memory, Resolution
from app.autofill.schema import FieldClass, FieldKind, FillSource, FormField, FormSchema
from app.autofill.themes import (
    QuestionTheme,
    Resolver,
    THEME_MEMORY_KEYS,
    THEME_RESOLVERS,
    country_in_label,
    unlisted_place,
)

logger = logging.getLogger(__name__)

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

    #: The field's class (core | screening | narrative | legal | consent |
    #: document | unknown), so a client can refuse to capture a LEGAL or
    #: CONSENT entry by class rather than by the wording of `reason` (audit
    #: 2026-10-12, F7). A plain string with a default, so older clients
    #: ignore it.
    field_class: str = FieldClass.UNKNOWN.value

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
    #: When a recognised profile key has no value, what to ask the applicant:
    #: {"section", "key", "prompt", "example", "note"}. The popup turns these
    #: into questions and saves the answers into the profile (Victor,
    #: 2026-10-12: "you also need to be able to edit this stuff and ask the
    #: user for this information").
    needs_fact: Optional[Dict[str, object]] = None
    #: For a file field: which document the form is asking for — resume,
    #: cover_letter, transcript, portfolio, or other — decided from the key and
    #: the label, whether or not that document is stored. The extension
    #: attaches the stored document of that kind or outlines the field with a
    #: note naming it; it never attaches the résumé to a slot that asks for
    #: something else (2026-10-12).
    document: Optional[str] = None

    #: Set when another control on the same question already answers it —
    #: Greenhouse offers "Resume/CV" as a file upload *and* a textarea, and
    #: supplying either satisfies the question. Such a field carries no value
    #: and needs no action, which is a third state distinct from both "filled"
    #: and "needs review"; without it, 93 already-answered controls across 57
    #: postings scored as coverage misses.
    satisfied_by: Optional[str] = None

    #: For a country select: the ISO 3166-1 alpha-2 code and dialling prefix
    #: of the stored country ("US", "+1"), so the extension can match a
    #: localised option by its dial-code suffix when the name does not match
    #: — Haven Interactive's French board rendered "États-Unis +1" (R50).
    #: `value` is still the stored name. None when the country is not in
    #: `format._COUNTRY_CODES`, and never set on anything but a country field.
    iso2: Optional[str] = None
    dial_code: Optional[str] = None


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
    # (May/June starts)" names a season but asks yes/no, not which. Not an
    # interview slot: "Interview availability: are you available for
    # interviews in the next two weeks?" was filled from term_flexible
    # (audit 2026-10-12, H4).
    (QuestionTheme.INTERNSHIP_AVAILABILITY,
     re.compile(r"^(?!.*\binterview)"
               r"(?=.*(\bi confirm my availability\b|\bare you available for\b"
               r"|\bconfirm .{0,20}availability\b|\bflexibility to consider\b))", re.I | re.S)),
    (QuestionTheme.INTERNSHIP_TERM,
     re.compile(r"\b(winter or summer|summer or winter|internship (term|season|cohort)"
               r"|cohort dates|prefer a (winter|summer)"
               # "Are you interested in a Spring Co-Op, Summer Internship, or
               # Both?" (Rugged Robotics, R50): two named seasons and an
               # umbrella, which is the term question in another coat.
               r"|(spring|summer|fall|winter)[^?]{0,40}\b(co-?op|internship)[^?]{0,40}\bor both)\b", re.I)),
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
    # "May we contact you via text message?" is an SMS consent, not a
    # reference check; it was filled from contact_current_employer (audit
    # 2026-10-12, H4). The theme is about an employer, so the object of
    # "contact" must not be the applicant and the channel must not be named.
    (QuestionTheme.CONTACT_CURRENT_EMPLOYER,
     re.compile(r"^(?!.*\b(text|texts|sms|whatsapp|messag\w*|phone call)\b)"
               r"(?=.*(\bmay we contact (?!you\b)|\bcontact your (current|previous|present|former)\b"
               r"|\bpermission to contact (?!you\b)))", re.I | re.S)),
    # "Do you currently require employer-sponsored work authorization" is a
    # sponsorship question, not "who is your current employer".
    (QuestionTheme.CURRENT_EMPLOYER,
     re.compile(r"^(?!.*\bemployer[- ]sponsor)(?=.*\b(who is your )?(current|previous).{0,12}\bemployer\b)",
                re.I | re.S)),
    # "Are you legally authorized to work in the US without (requiring)
    # sponsorship?" is an authorisation question qualified by sponsorship:
    # yes only when the applicant is authorised *and* needs no sponsorship.
    # 24 corpus instances were filled with the sponsorship answer "No" for a
    # citizen whose true answer is "Yes" (audit 2026-10-12, F15a). Two shapes
    # outrank the bare sponsorship pattern below: the qualifier "without …
    # sponsor", and an authorisation question whose own sentence (up to its
    # "?") mentions neither sponsorship nor requiring anything — "Are you
    # authorized to work in the US? We DO assist with visa sponsorship".
    # `_resolve_computed` reads the qualifier and combines both stored
    # answers.
    (QuestionTheme.WORK_AUTHORIZATION,
     re.compile(r"\b(authoriz\w*|eligible|entitled|right)\b[^?]{0,40}\bwork\b[^?]{0,80}"
                r"\bwithout\b[^?]{0,60}\bsponsor", re.I)),
    (QuestionTheme.WORK_AUTHORIZATION,
     re.compile(r"^(?![^?]*\b(sponsor\w*|requir\w*|need\w*|stem|opt|cpt|extension|extended)\b)"
                r"[^?]*\b(authoriz\w*|eligible|entitled)\b[^?]{0,40}\bwork\b[^?]*\?", re.I | re.S)),
    (QuestionTheme.VISA_SPONSORSHIP, re.compile(r"\bsponsor", re.I)),
    # Not a visa-programme question: "Do you have a STEM degree that would
    # make you eligible for extended work authorization?" was answered "Yes"
    # from the stored authorisation (audit 2026-10-12, H4). OPT / CPT / STEM
    # extension questions are left for the model gate, whose applicant state
    # says whether those programmes apply at all.
    (QuestionTheme.WORK_AUTHORIZATION,
     re.compile(r"^(?!.*\b(stem|opt|cpt|extension|extended)\b)"
               r"(?=.*\b(authoriz\w*|entitled|eligible)\b[^?]{0,40}\bwork\b)", re.I | re.S)),
    # "Interview availability" is not "interviewed here before": the theme
    # needs a past-tense marker, else an interview-scheduling question lands
    # on an ASK theme and its wording is lost (audit 2026-10-12, H4).
    (QuestionTheme.INTERVIEWED_HERE_BEFORE,
     re.compile(r"(?=.*\binterview)(?=.*\b(before|previously|prior|past|ever|already)\b)", re.I | re.S)),
    # Lookaheads rather than a sequence: real forms write both "worked for X
    # before" and "have you previously worked for X", and an ordered pattern
    # caught only the first — 56 instances across 149 postings went to review
    # for word order alone. "worked" is past tense deliberately, so "open to
    # working in-person" does not match.
    (QuestionTheme.WORKED_HERE_BEFORE,
     re.compile(r"(?=.*\b(?:worked|employee|employed|contractor|intern(?:ed)?)\b)"
               r"(?=.*\b(?:before|previously|previous)\b)", re.I)),
    (QuestionTheme.CURRENT_STATE, re.compile(r"\b(state or province|which state)\b", re.I)),
    # The theme reads the stored street address, so it is gated on what a
    # street address is called — street / mailing / home / residential /
    # current / legal / permanent / postal address, "Address 1", "Address
    # Line 1", a bare "Address", or the address one would work from — not on
    # the word "address" alone. An exclusion list ("university email
    # address", M9) left "Employer Address", "School Address", "GitHub
    # address", "Reference 1 Address" and "Billing address" filling with the
    # applicant's home street at confidence 1.0 (review of 2026-10-12,
    # finding 3). A second line ("Street Address (Optional Second Line)",
    # x5) and a yes/no about having an address somewhere ("Do you currently
    # have a permanent or school address in the DC metro area?", x5) are
    # still excluded.
    (QuestionTheme.WORK_ADDRESS,
     re.compile(r"^(?!.*\b(second line|line 2|address 2)\b)"
               r"(?!\s*(do|are|have|will|would|can|could|is) you\b)"
               r"(?=.*(\b(street|mailing|home|residential|current|legal|permanent|physical|postal)\s+address\b"
               r"|\baddress\s*(line\s*)?1\b"
               r"|\baddress\b.{0,15}\bwork from\b"
               r"|^\W*(your\s+|full\s+)?address\W*$))", re.I | re.S)),
    # A general tolerance answers a general question. "Are you able to work
    # in-person in San Jose this summer?" names a place, and the stored
    # in_office_tolerance says nothing about San Jose; it was filled at 1.0
    # (audit 2026-10-12, H4). A capitalised word right after "in-person in /
    # at / from" is a place name; "in one of our offices" is not.
    (QuestionTheme.IN_OFFICE_TOLERANCE,
     re.compile(r"^(?!.*\b(in[-\s]?person|in[-\s]?office|on[-\s]?site|hybrid)\b[^?]{0,30}?"
               r"\b(in|at|from|to)\s+(the\s+|our\s+)?(?-i:[A-Z]))"
               r"(?=.*\b(in[-\s]?person|in[-\s]?office|days?\s*/\s*week|hybrid)\b)", re.I | re.S)),
    # Willingness to move, not the cost of moving: "Do you require relocation
    # assistance?" was filled from open_to_relocation (audit 2026-10-12, H4).
    (QuestionTheme.WILLING_TO_RELOCATE,
     re.compile(r"\brelocat(?!\w*\s+(assistance|package|support|bonus|benefits?|expenses?|costs?"
               r"|reimbursement|stipend|allowance))", re.I)),
    (QuestionTheme.CURRENT_LOCATION,
     re.compile(r"\b(currently (located|based|live)|where .{0,30}"
               r"(based|work from|intend to work))", re.I)),
    (QuestionTheme.HEARD_ABOUT, re.compile(r"\bhear about\b", re.I)),
    # R48-2: "What is your desired salary?" x6, "What are your salary
    # expectations?" x5+3, "Desired Salary" x3, "annual base salary
    # expectations" — all went unanswered with desired_salary stored.
    (QuestionTheme.OFFICE_PREFERENCE,
     re.compile(r"\b(which|select|choose|pick|rank)\b.{0,50}\b(office|location|city|hub)s?\b.{0,60}\b(prefer|interested|most|rank|first choice)"
                r"|\b(preferred|first.choice|top) (office|work location|location|city)\b|\boffice (preference|location preference)\b", re.I)),
    (QuestionTheme.DESIRED_SALARY,
     re.compile(r"\b(desired|expected|target|anticipated)\s+(annual\s+)?(base\s+)?(salary|compensation|pay|rate)\b"
                r"|\b(salary|compensation|pay)\s+(expectation|requirement)s?\b|\bexpected base\b", re.I)),
    (QuestionTheme.INTERNSHIP_END,
     re.compile(r"\b(ideal |anticipated |expected )?end date\b|\bending your internship\b|\binternship end\b", re.I)),
    # A bare "Start Date" is some other date on an education or employment
    # block; it was filled with the earliest-start month (audit 2026-10-12,
    # H4). The theme needs "earliest", a notice period, "when … start", or a
    # preference / availability word on the start ("Preferred Internship/
    # Co-Op Start Date" x9, "When are you available to start?" x7 in the
    # corpora). "Are you available to start on <date>?" asks yes/no about
    # the employer's date, not for the applicant's, and is excluded.
    (QuestionTheme.EARLIEST_START,
     re.compile(r"^(?!.*\bstart and end\b)"
               r"(?=.*\b(earliest|notice period|when .{0,20}start\b"
               r"|(available|availability|preferred|desired|ideal|target|possible|potential|anticipated|expected)\b"
               r".{0,25}\bstart( date)?\b(?! (on|the|in|by|for)\b)"
               r"|start date (that |which )?(you )?(could|can|would|availab|prefer)))", re.I | re.S)),
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
            # The two history patterns are double lookaheads and quadratic in
            # the label's length (a 50 KB label cost seconds, audit F10); no
            # corpus label exceeds 1,941 chars, so the scan stops at 2,000.
            text = (label or "")[:LABEL_SCAN_LIMIT]
            for theme, pattern in _THEME_PATTERNS:
                if pattern.search(text):
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
    # A numeric control takes a number. A phone number becomes its digits
    # (TELUS asks "Phone" as a Number field); "8 weeks +" into "notice period
    # (in months)" is not an answer and goes back for review.
    if getattr(field, "numeric", False) and resolution.value is not None and not resolution.values:
        text = str(resolution.value).strip()
        digits = re.sub(r"\D", "", text)
        if re.fullmatch(r"-?\d+([.,]\d+)?", text):
            pass
        elif len(digits) >= 7 and re.fullmatch(r"[\d\s().+-]+", text):
            resolution.value = digits
        else:
            return Resolution(
                field_key=field.key, value=None, source=FillSource.HUMAN, confidence=0.0,
                needs_review=True,
                reason="this field takes a number; {!r} is not one".format(text[:30]),
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
        # A menu that offers "Both" / "Either" / "All of the above" says it in
        # one option; ticking it beside the cohorts it stands for is
        # incoherent (Rugged Robotics, R50 2026-10-12: Spring, Summer and
        # Both were all selected). That option alone is the answer then.
        umbrella = [o for o in workable if _ANY_TERM.match(o.strip())]
        if umbrella and len(workable) > 1:
            return Resolution(
                field_key=field.key,
                values=[umbrella[0]],
                source=FillSource.MEMORY,
                reason="you are flexible about terms, and this menu says that in one option",
            )
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
        # A country the table does not know is different from no country:
        # "authorized to work in Japan?" must not take the general answer
        # (audit 2026-10-12, F37).
        country = country_in_label(field.label)
        if country is None:
            place = unlisted_place(field.label, company)
            if place:
                return Resolution(
                    field_key=field.key,
                    source=FillSource.MEMORY,
                    needs_review=True,
                    reason="asks about working in {}, which your profile has no answer for".format(place),
                )
            country = default_country

        if theme == QuestionTheme.WORK_AUTHORIZATION and _WITHOUT_SPONSORSHIP.search(field.label or ""):
            return _authorised_without_sponsorship(field, memory, country)

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


#: "… authorized to work in the US without (requiring | the need for | employer)
#: sponsorship": the sponsorship clause qualifies the authorisation question.
_WITHOUT_SPONSORSHIP = re.compile(r"\bwithout\b[^?]{0,60}\bsponsor", re.I)


def _is_us_citizen(memory: Memory) -> bool:
    citizenship = str((memory.legal_status or {}).get("citizenship") or "").lower()
    return ("citizen" in citizenship and "non" not in citizenship
            and "u.s" in citizenship.replace("us", "u.s"))


def _authorised_without_sponsorship(field: FormField, memory: Memory, country: Optional[str]) -> Resolution:
    """Yes only when authorised to work there *and* needing no sponsorship.

    Either stored answer alone misreads the question: the sponsorship answer
    "No" was written for a citizen (true answer "Yes"), and the authorisation
    answer alone would say "Yes" for a student on OPT who needs sponsorship
    later. A label that also demands citizenship ("requires candidates be
    U.S. citizens who are legally authorized …") is answered only for a US
    citizen; anyone else sees it.
    """
    auth_stored = (memory.authorization_for(country) if country else None) or memory.legal_status.get("work_authorization")
    sponsor_stored = (memory.sponsorship_for(country) if country else None) or memory.legal_status.get("needs_sponsorship")
    auth = _as_bool(auth_stored) if auth_stored else None
    sponsor = _as_bool(sponsor_stored) if sponsor_stored else None
    where = country or "this country"

    if re.search(r"\bcitizen", field.label or "", re.I) and country in (None, "US") and not _is_us_citizen(memory):
        return Resolution(field_key=field.key, source=FillSource.MEMORY, needs_review=True,
                          reason="asks for US citizenship as well as work authorisation; your profile does not settle it")
    if auth is False or sponsor is True:
        answer = False
    elif auth is True and sponsor is False:
        answer = True
    else:
        return Resolution(field_key=field.key, source=FillSource.MEMORY, needs_review=True,
                          reason="needs both your work authorisation and your sponsorship answer for {}".format(where))
    option = option_for_bool(answer, field) if field.options else None
    return Resolution(
        field_key=field.key,
        value=option.label if option else ("Yes" if answer else "No"),
        source=FillSource.MEMORY,
        reason="computed from your work authorisation and sponsorship answers for {}".format(where),
    )


#: A STORED theme whose key is derived from another fact: when it is unset,
#: the fact to ask for is the one it derives from.
_ASK_FOR = {"state_of_residence": "current_location", "city_of_residence": "current_location"}


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
            reason="recognised as {} but you have not set it".format(_ASK_FOR.get(key, key)),
        )

    # A stored "None" / "N/A" is a decision to leave a text control blank,
    # exactly as memory.resolve treats it: timeline_notes "None" was typed
    # verbatim into 54 corpus fields (audit 2026-10-12, §4). Never applied to
    # a field with options, where "No" is a real answer to "Do you have any
    # deadlines?"; a required blank surfaces rather than blocking submission.
    if memory.is_blank(stored) and not field.options:
        if field.required:
            return Resolution(field_key=field.key, source=FillSource.MEMORY, needs_review=True,
                              reason="you recorded no {}, but this form requires it".format(key))
        return Resolution(field_key=field.key, source=FillSource.MEMORY,
                          skipped="you recorded no {}".format(key))

    option = match_option(stored, field) if field.options else None

    # "How did you hear about us?" where the menu has no careers-site option
    # (x46 required across 578 Greenhouse forms, x27 on Ashby): the applicant's
    # ordered second choices, each tried as an exact match against the menu.
    # Offices in the applicant's order: the first one this form offers.
    if field.options and theme == QuestionTheme.OFFICE_PREFERENCE:
        for choice in (stored or "").split("|"):
            option = match_option(choice.strip(), field) if choice.strip() else None
            if option is not None:
                break
        if option is None:
            return Resolution(field_key=field.key, value=stored, source=FillSource.MEMORY, needs_review=True,
                              reason="none of your preferred offices is offered here")

    if option is None and field.options and theme == QuestionTheme.HEARD_ABOUT:
        for fallback in (memory.preferences.get("heard_about_fallback") or "").split("|"):
            option = match_option(fallback.strip(), field) if fallback.strip() else None
            if option is not None:
                key = "heard_about_fallback"
                break

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

#: A parent answer that says the question did not apply: no follow-up applies.
_NO_ANSWER_APPLIES = re.compile(r"^(n/?a|n\.a\.|not applicable|does not apply|none)\b", re.I)

#: "If you selected <options>, …": the clause that names the triggering
#: option(s), cut where the instruction begins.
_CONDITION_CLAUSE = re.compile(
    r"\bif (?:you )?(?:have )?(?:selected|answered|chose|responded|said|indicated|marked|checked)\s+(.+)",
    re.I | re.S)
_CLAUSE_END = re.compile(
    r"(:|;|\?|,?\s+please\b|\s+above\b|\s+below\b|\s+as\b|\s+to\b|\s+for\b|\s+in\b|\s+on\b|\s+at\b"
    r"|,?\s+(what|which|how|where|who|when)\b)", re.I)
_CLAUSE_SPLIT = re.compile(r",|/|\bor\b|\band\b", re.I)
_QUOTES = re.compile(r"[\"'“”‘’]")
#: Named options that carry no identity of their own; the polarity path
#: handles these.
_NOT_A_NAMED_OPTION = frozenset({"yes", "no", "so", "the above", "any of the above", "any", "either", ""})


def _plain(text: str) -> str:
    return normalize_label(_QUOTES.sub("", text or ""))


def _condition_options(label: str) -> List[str]:
    """The option names a condition cites, normalised; empty for a yes/no condition."""
    match = _CONDITION_CLAUSE.search(label or "")
    if not match:
        return []
    clause = match.group(1)
    end = _CLAUSE_END.search(clause)
    if end:
        clause = clause[:end.start()]
    out = []
    for part in _CLAUSE_SPLIT.split(clause):
        plain = _plain(part)
        if plain not in _NOT_A_NAMED_OPTION and len(plain) >= 3:
            out.append(plain)
    return out

_CONDITIONAL = re.compile(
    r"\bif (you |your )?(answered|selected|responded|you said)\b"
    r"|\bif (yes|so|the above|any of the above)\b"
    r"|\bif you (answered|selected) [\"'“]?yes",
    re.I,
)

#: Which answer to the preceding question makes the follow-up apply.
_CONDITION_IS_NO = re.compile(r"\bif (you )?(answered|selected|responded|chose)\s*[\"'“]?no\b", re.I)
#: "If you selected 'Other' above, please specify": the trigger is the option
#: named "Other", not a yes. Read from the condition itself, not from "please
#: specify", which also follows "If you answered Yes, please specify your visa".
_CONDITION_IS_OTHER = re.compile(r"\bif (you )?(answered|selected|responded|chose|said)\s*[\"'“]?other\b|\bif other\b", re.I)


#: Reason text set by the resolvers when a stored answer exists but matches no
#: option deterministically. That is the signal for the Jev pass below.
#: "None of these options work for me" — an opt-out that must never be selected
#: alongside the real choices.
#: "Both", "Either", "All of the above", "Any", "No preference": one option
#: that stands for every cohort on the menu.
_ANY_TERM = re.compile(
    r"^(both|either|all( of (the above|these))?|any( of (the above|these))?|no preference|flexible|open to (both|either|any))"
    r"\b(?!.*\b(other|not|neither|none)\b).{0,40}$", re.I)

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


def _skip_untriggered_follow_up(entry, order, position, by_key, fields=None) -> bool:
    """Leave an optional "If other, please specify" blank when nothing triggered it.

    x24 "If other, please specify", x20 "Please specify", x18 "Please provide
    additional detail if appropriate.", x14 "If other, please explain" across
    578 Greenhouse forms — almost all optional, all sent to review. The field
    applies only when the answer before it was "Other" (or, for an "if yes"
    wording, yes). With that preceding answer settled and not a trigger, blank
    is the answer; with it unsettled, the follow-up stays for review alongside
    it. Required follow-ups are never skipped here.
    """
    if entry.required or not entry.needs_review or entry.skipped or entry.satisfied_by:
        return False
    if not is_follow_up(entry.label or ""):
        return False
    # "If yes, …" / "If you answered …" wordings belong to _resolve_conditionals,
    # which also knows how to pick a form's own "N/A" option and how to treat a
    # required follow-up. This rule takes only the labels that rule cannot read.
    if _CONDITIONAL.search(entry.label or ""):
        return False
    index = position.get(entry.field_key)
    if not index:
        return False
    previous = None
    for offset in range(index - 1, -1, -1):
        candidate = by_key.get(order[offset])
        if candidate is None or candidate.skipped or candidate.satisfied_by:
            continue
        previous = candidate
        break
    if previous is None or previous.needs_review or not (previous.value or previous.values):
        return False
    answers = [str(v) for v in (previous.values or [previous.value])]
    wants_other = bool(FOLLOW_UP_ON_OTHER.search(normalize_label(entry.label or "")))
    # A follow-up hangs off a *choice*. Crusoe's "Additional information or a
    # note you want to share" sits after a links field; a URL is not an answer
    # anything can follow up on, so that field is not a follow-up at all.
    parent = (fields or {}).get(previous.field_key)
    if parent is not None and not parent.options and parent.kind != FieldKind.BOOLEAN:
        if not any(_as_bool(a) is not None or normalize_label(a).startswith("other") for a in answers):
            return False
    if wants_other:
        triggered = any(normalize_label(a).startswith("other") for a in answers)
    else:
        # The shared polarity: "I am not a protected veteran" is a no (F14).
        triggered = any(_as_bool(a) is True for a in answers)
    if triggered:
        return False
    # A protected answer is never quoted into another field's reason.
    legal_parent = parent is not None and parent.field_class == FieldClass.LEGAL
    entry.skipped = "follow-up to {!r}, which you answered {}".format(
        (previous.label or "")[:40], "already" if legal_parent else repr(answers[0][:24]))
    entry.reason = entry.skipped
    entry.needs_review = False
    entry.value = None
    entry.values = []
    entry.source = FillSource.MEMORY
    return True


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
    fields_by_key = {f.key: f for f in form.fields}

    for entry in entries:
        if _skip_untriggered_follow_up(entry, order, position, by_key, fields_by_key):
            continue

        condition = _CONDITIONAL.search(entry.label or "")
        if not condition:
            continue
        # "Do you have a Duolingo account? If yes, what is your username?" is
        # one question with its own condition, not a follow-up to the field
        # before it; hanging it on an unrelated "No" hid it as answered.
        if "?" in (entry.label or "")[:condition.start()]:
            continue

        index = position.get(entry.field_key)
        if not index:
            continue

        # The parent is the question immediately before this one — not the
        # nearest one that happens to hold a value, which walked past an
        # unanswered or multi-select parent to an unrelated earlier "No"
        # (audit 2026-10-12, F13). Skipped and satisfied entries are not
        # questions the applicant answered here, and a follow-up before this
        # one ("If yes, please explain" then "If yes, provide details")
        # chains to the same parent; everything else is the parent.
        previous = None
        for offset in range(index - 1, -1, -1):
            candidate = by_key.get(order[offset])
            if candidate is None or candidate.skipped or candidate.satisfied_by:
                continue
            if _CONDITIONAL.search(candidate.label or "") and not (candidate.value or candidate.values):
                continue
            previous = candidate
            break

        if previous is None:
            continue

        parent = fields_by_key.get(previous.field_key)
        answers = [str(v) for v in (previous.values or ([previous.value] if previous.value else []))]

        if previous.needs_review or not answers:
            # The parent is unsettled, so the follow-up stays with it. An
            # answer of the entry's own (a required follow-up the sponsorship
            # theme answered) is left as it is.
            if not entry.value and not entry.values:
                entry.needs_review = True
                entry.reason = "follows {!r}, which is still unanswered".format((previous.label or "")[:40])
            continue

        # A protected answer is never quoted into another field's reason (F14).
        legal_parent = parent is not None and parent.field_class == FieldClass.LEGAL
        said = "you answered the question above" if legal_parent else "you answered '{}' above".format(answers[0][:24])
        after = "your answer above" if legal_parent else "your '{}' above".format(answers[0][:24])

        named = _condition_options(entry.label or "")
        if _CONDITION_IS_OTHER.search(entry.label or ""):
            applies = any(normalize_label(a).startswith("other") for a in answers)
        elif named:
            # "If you selected 'A friend or relative', …" names the option
            # that triggers it; the parent's answer either is that option
            # or is not.
            given = [_plain(a) for a in answers]
            applies = any(alt in g or g in alt for g in given for alt in named)
        else:
            # The shared polarity (format._as_bool): "I am not a protected
            # veteran" is a no, and "Decline to self-identify" is neither.
            polarity = [_as_bool(a) for a in answers]
            if any(p is not None for p in polarity):
                answered_yes = any(p is True for p in polarity)
                triggers_on_no = bool(_CONDITION_IS_NO.search(entry.label or ""))
                applies = answered_yes if not triggers_on_no else not answered_yes
            elif any(_NO_ANSWER_APPLIES.match(a.strip()) for a in answers):
                # "NA" / "None" above is neither yes nor no: no follow-up
                # applies in either direction.
                applies = False
            else:
                # "Decline to self-identify", a URL, a name: no polarity to
                # hang a condition on. Left with the applicant.
                if not entry.value and not entry.values:
                    entry.needs_review = True
                    entry.reason = "follows the question above; could not tell from {} whether it applies".format(
                        "your answer" if legal_parent else "'{}'".format(answers[0][:24]))
                continue

        if applies:
            entry.needs_review = True
            entry.reason = "{}, so this needs your detail".format(said)
            continue

        # Inapplicable. If the form offers an option that says so, select it:
        # Duolingo's "If so, are you eligible for OPT?" is *required* and
        # offers Yes / No / NA, so leaving it blank blocks submission while
        # "NA" is the true answer. Without such an option, blank is right.
        field = fields_by_key.get(entry.field_key)
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
            entry.reason = "not applicable — {}, and this form offers {!r}".format(said, not_applicable.label)
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
                entry.reason = "not applicable after {}, but required — answered {!r} from your profile".format(
                    after, str(entry.value)[:24]
                )
                continue
            entry.needs_review = True
            entry.satisfied_by = None
            entry.reason = "not applicable after {}, but this form requires an answer".format(after)
            continue

        entry.needs_review = False
        entry.satisfied_by = previous.field_key
        entry.reason = ("not applicable — you answered the question above" if legal_parent
                        else "not applicable — you answered '{}' to the question above".format(answers[0][:24]))


#: Facts a screening question can turn on that are neither protected nor
#: contact details. Whitelisted by key so that a new fact never reaches the
#: model by accident. Counts are how often the 100-board run wanted them.
_BACKGROUND_FACTS = (
    "security_clearance",     # 4 boards: hold one? which level? eligible?
    "attended_career_fair",   # career fair / saw us on campus, 12 source variants
    "prior_internships",      # Klaviyo "how many prior internships", counts
    "gpa_scale",              # Radix "your university's GPA range"
    "years_experience",       # Wealth.com "professional experience writing backend code?", x6 "years of industry experience"
    "current_job_title",
    "current_employer",
)

_STANDING = {
    1: "first-year (freshman)",
    2: "second-year (sophomore)",
    3: "third-year (junior)",
    4: "fourth-year (senior)",
}


_CLASS_YEARS = re.compile(
    r"\b(freshman|freshmen|sophomore|junior|senior|first[- ]year|second[- ]year|third[- ]year|"
    r"fourth[- ]year|final[- ]year)\b", re.I)
_CLASS_EXCLUDE = re.compile(r"\b(senior (software|engineer|developer|level|role|position)|years? of)\b", re.I)


def _class_standing_answer(field: FormField, memory: Memory) -> Optional[Resolution]:
    """Answer "Are you currently a Freshman or Sophomore?" from the calendar.

    Base Power (Ashby, x5) asks it as a checkbox; the enrollment theme handed
    it "Currently Enrolled" and the yes/no guard refused. Class standing is
    arithmetic on the stored start or graduation date, so the answer is
    whether the standing the dates give is one of the years the label names.
    """
    two_way = (field.kind == FieldKind.SINGLE_SELECT and len(field.options) == 2
               and all(_as_bool(o.label) is not None for o in field.options))
    if field.kind != FieldKind.BOOLEAN and not two_way:
        return None
    label = field.label or ""
    if not _CLASS_YEARS.search(label) or _CLASS_EXCLUDE.search(label):
        return None
    if not re.search(r"\b(are you|currently|student|undergraduate|program)\b", label, re.I):
        return None
    standing = _class_standing(memory.education, date.today())
    if not standing or standing.startswith(("graduated", "not yet")):
        return None
    ordinal = {"first year": "freshman", "second year": "sophomore", "third year": "junior",
               "fourth year": "senior", "final year": "senior", "freshmen": "freshman"}
    named = {ordinal.get(m.lower().replace("-", " "), m.lower()) for m in _CLASS_YEARS.findall(label)}
    found = re.search(r"\((freshman|sophomore|junior|senior)\)", standing)
    if not found:
        return None
    mine = found.group(1)
    answer = mine in named
    option = option_for_bool(answer, field) if field.options else None
    value = option.label if option else ("Yes" if answer else "No")
    return Resolution(field_key=field.key, value=value, source=FillSource.MEMORY,
                      reason="computed: you are a {} by your stored dates".format(mine))


_GRADUATES = re.compile(r"\bgraduat\w*\b", re.I)
_GRAD_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_GRAD_MONTH_YEAR = re.compile(r"\b" + _GRAD_MONTH + r",?\s+((?:19|20)\d{2})\b", re.I)
_GRAD_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
#: The question's date is a bound, not a set: "by June 2027", "before 2028",
#: "after May 2027", "no earlier than 2028", "between X and Y".
_GRAD_AT_MOST = re.compile(r"\b(by|before|prior to|no later than|on or before|until|up to|or earlier|or sooner)\b", re.I)
_GRAD_AT_LEAST = re.compile(r"\b(after|later than|no earlier than|on or after|following|or later|at the earliest)\b", re.I)
_GRAD_STRICT = re.compile(r"\b(before|prior to|after|later than)\b", re.I)
_GRAD_BETWEEN = re.compile(r"\bbetween\b", re.I)
#: A date the question refers to but does not state: "before the start
#: date", "by the time the internship begins", "prior to your start".
_GRAD_UNSTATED = re.compile(r"\b(start date|start of|begins?|beginning|commence|prior to (your|the)|before (your|the)"
                            r"|by the time|at the time|when (you|the)|end of (the|your|this))\b", re.I)
_GRAD_NOT_THIS = re.compile(r"\bhigh school\b|\bsecondary school\b|\bbootcamp\b|\b(ceremony|commencement|gift|party)\b", re.I)
_GRAD_OTHER_LEVEL = re.compile(r"\b(master'?s?|phd|ph\.d|doctor(al|ate)|mba|graduate (program|programme|degree|school|studies|student))\b", re.I)
_GRAD_BACHELORS = re.compile(r"\b(bachelor'?s?|undergrad\w*|b\.?s\.?|b\.?a\.?)\b", re.I)
_GRAD_UNDERGRAD_MARKERS = ("bachelor", "b.s", "bs ", "b.a", "ba ", "undergrad", "bsc")


def _graduation_by_date_answer(field: FormField, memory: Memory) -> Optional[Resolution]:
    """Answer "Will you be graduating in 2028?" from the stored graduation date.

    R50 live round: Lendable (Ashby) asked it as Yes/No, another board asked
    "Are you graduating in December 2027 or May 2028?", Enova asked "If you
    are in a Bachelor's program, do you graduate by June 2027?". All three
    went to the model gate for a comparison the calendar settles. The
    question names a year, a month-year or a set of them, possibly as a
    bound ("by", "before", "after", "between"); the stored date is compared
    at the granularity the question uses.

    Review, not a guess, when the question refers to a date it does not
    state ("will you have graduated before the start date?"), when the
    stored date is too coarse for the comparison (a bare year against "by
    June 2027" when the years are equal), when the question is conditional
    on a degree level the applicant is not in, or when the dates in the
    question do not compose into one test. None — the existing paths — when
    the question names no date at all ("Have you graduated?") or asks about
    another graduation (high school, a bootcamp, a graduate programme for a
    bachelor's student).
    """
    two_way = (field.kind == FieldKind.SINGLE_SELECT and len(field.options) == 2
               and all(_as_bool(o.label) is not None for o in field.options))
    if field.kind != FieldKind.BOOLEAN and not two_way:
        return None
    label = field.label or ""
    # "graduate program" is a degree level, not the act of graduating: "Are
    # you enrolled in a graduate program through 2028?" is the
    # graduate-programme theme's question and it answers it.
    if not _GRADUATES.search(_GRAD_OTHER_LEVEL.sub(" ", label)) or _GRAD_NOT_THIS.search(label):
        return None

    from app.autofill.memory import _month_of, _year_of  # local: memory imports binder's schema

    stored = memory.education.get("graduation_date") or ""
    grad_year = _year_of(stored)
    if not grad_year:
        return None
    grad_year = int(grad_year)
    grad_month = _month_of(stored)
    grad_month_index = (_MONTH_INDEX[grad_month] if grad_month else None)

    # A conditional on a degree level: "If you are in a Bachelor's program…"
    # for a master's student, or a graduate-programme date for an
    # undergraduate, is not settled by the stored date.
    degree = (memory.education.get("degree") or "").lower()
    names_bachelors = bool(_GRAD_BACHELORS.search(label))
    names_graduate = bool(_GRAD_OTHER_LEVEL.search(label))
    if degree and (names_bachelors or names_graduate):
        is_undergrad = any(m in degree for m in _GRAD_UNDERGRAD_MARKERS)
        is_graduate = any(m in degree for m in _GRAD_MARKERS)
        if (names_bachelors and not names_graduate and is_graduate and not is_undergrad) or \
                (names_graduate and not names_bachelors and is_undergrad and not is_graduate):
            return Resolution(field_key=field.key, source=FillSource.HUMAN, needs_review=True,
                              reason="asks about graduating from a {} programme; you are pursuing {}".format(
                                  "bachelor's" if names_bachelors else "graduate", memory.education.get("degree")))
    elif names_graduate and not names_bachelors:
        return None

    # The dates the question names: month-years first, then the years not
    # already inside one.
    month_years = [(_MONTH_INDEX[m[:3].lower()], int(y)) for m, y in _GRAD_MONTH_YEAR.findall(label)]
    rest = _GRAD_MONTH_YEAR.sub(" ", label)
    years = [int(y) for y in _GRAD_YEAR.findall(rest)]
    named = [(None, y) for y in years] + month_years
    if not named:
        if _GRAD_UNSTATED.search(label) or _GRAD_AT_MOST.search(label) or _GRAD_AT_LEAST.search(label):
            return Resolution(field_key=field.key, source=FillSource.HUMAN, needs_review=True,
                              reason="asks whether you graduate relative to a date the question does not state")
        return None

    def compare(target):
        """-1, 0, 1 for stored before / same / after `target`, None if unsettled."""
        month, year = target
        if grad_year != year:
            return -1 if grad_year < year else 1
        if month is None:
            return 0
        if grad_month_index is None:
            return None
        return (grad_month_index > month) - (grad_month_index < month)

    at_most, at_least, between = _GRAD_AT_MOST.search(label), _GRAD_AT_LEAST.search(label), _GRAD_BETWEEN.search(label)
    answer: Optional[bool]
    if between and len(named) == 2:
        low, high = sorted(named, key=lambda t: (t[1], t[0] or 0))
        lo, hi = compare(low), compare(high)
        answer = None if lo is None or hi is None else (lo >= 0 and hi <= 0)
    elif (at_most or at_least) and not between and len(named) == 1 and not (at_most and at_least):
        c = compare(named[0])
        if c is None:
            answer = None
        elif at_most:
            answer = c < 0 if _GRAD_STRICT.search(at_most.group(0)) else c <= 0
        else:
            answer = c > 0 if _GRAD_STRICT.search(at_least.group(0)) else c >= 0
    elif not at_most and not at_least and not between:
        results = [compare(t) for t in named]
        answer = None if any(r is None for r in results) else any(r == 0 for r in results)
    else:
        answer = None

    if answer is None:
        return Resolution(field_key=field.key, source=FillSource.HUMAN, needs_review=True,
                          reason="your stored graduation date ({}) does not settle this question as asked".format(stored))
    option = option_for_bool(answer, field) if field.options else None
    value = option.label if option else ("Yes" if answer else "No")
    return Resolution(field_key=field.key, value=value, source=FillSource.MEMORY,
                      reason="computed: you graduate {}".format(stored))


_MONTH_INDEX = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_MONTH_INDEX.update({m: i + 1 for i, m in enumerate(("January", "February", "March", "April", "May", "June", "July",
                                                      "August", "September", "October", "November", "December"))})

_OTHER_LEVEL = re.compile(r"\b(master'?s?|phd|ph\.d|doctor(al|ate)|graduate (gpa|degree|studies|program|programme))\b", re.I)
_GRAD_MARKERS = ("master", "m.s", "ms ", "phd", "ph.d", "doctor", "msc", "meng")


def _other_degree_level_answer(field: FormField, memory: Memory) -> Optional[Resolution]:
    """A question about a degree level the applicant is not in is "not applicable".

    Rocket Lab (audit 2026-10-12): "Select your anticipated master's degree
    graduation date" was answered 05/2028 for a bachelor's student — the gate
    carried the bachelor's date across. When the form offers a not-applicable
    option it is chosen; otherwise the field stays with the applicant rather
    than taking a value from the other degree.
    """
    label = field.label or ""
    if not _OTHER_LEVEL.search(label) or re.search(r"\b(bachelor|undergrad)", label, re.I):
        return None
    # Only a question that wants a *value* from that degree (a date, a GPA, a
    # school, a field). "Are you enrolled in a Master's or PhD programme?" is a
    # yes/no the graduate-programme theme answers, correctly, with No.
    if not re.search(r"\b(date|gpa|grade|transcript|year|major|field of study|school|university|institution)\b", label, re.I):
        return None
    if field.kind == FieldKind.BOOLEAN or (field.options and all(_as_bool(o.label) is not None for o in field.options)):
        return None
    degree = (memory.education.get("degree") or "").lower()
    if not degree or any(m in degree for m in _GRAD_MARKERS):
        return None
    if field.options:
        na = next((o for o in field.options if _NOT_APPLICABLE.match(o.label.strip())
                   or re.match(r"^(other\s*/\s*)?(not applicable|n/?a|none|do not recall)", o.label.strip(), re.I)), None)
        if na is not None:
            return Resolution(field_key=field.key, value=na.label, source=FillSource.MEMORY,
                              reason="not applicable: you are pursuing a bachelor's")
    if field.kind == FieldKind.TEXT and not field.required:
        return Resolution(field_key=field.key, source=FillSource.MEMORY,
                          skipped="asks about a graduate degree; you are pursuing a bachelor's")
    return Resolution(field_key=field.key, source=FillSource.HUMAN, needs_review=True,
                      reason="asks about a graduate degree; you are pursuing a bachelor's and the form offers no 'not applicable'")


_BASED_IN = re.compile(r"\b(based|located|living|live|reside|residing|physically located)\s+(in|within)\b", re.I)
_COUNTRY_NAMES = {"US": "united states", "CA": "canada", "UK": "united kingdom", "IE": "ireland", "DE": "germany", "IN": "india"}


def _based_in_country_answer(field: FormField, memory: Memory) -> Optional[Resolution]:
    """"Are you currently based in the United States?" from the stored country.

    R48-2: two Ashby forms asked it as a checkbox; the location theme handed
    them "Ithaca, NY" and the yes/no guard refused. The country named in the
    label against the applicant's country of residence is arithmetic, not
    judgement. Only a *country* — "based in the Bay Area" stays with the gate.
    """
    two_way = (field.kind == FieldKind.SINGLE_SELECT and len(field.options) == 2
               and all(_as_bool(o.label) is not None for o in field.options))
    if field.kind != FieldKind.BOOLEAN and not two_way:
        return None
    label = field.label or ""
    code = country_in_label(label)
    if not code or not _BASED_IN.search(label):
        return None
    mine = (memory.lookup("country_of_residence") or "").strip().lower()
    if not mine:
        return None
    mine_code = next((c for c, name in _COUNTRY_NAMES.items() if name in mine or mine in (c.lower(), "usa", "u.s.", "u.s.a.") and c == "US"), None)
    if mine_code is None:
        return None
    answer = mine_code == code
    option = option_for_bool(answer, field) if field.options else None
    value = option.label if option else ("Yes" if answer else "No")
    return Resolution(field_key=field.key, value=value, source=FillSource.MEMORY,
                      reason="computed: you live in {}".format(memory.lookup("country_of_residence")))


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
        "learned": {k: v for k, v in (getattr(memory, "learned", None) or {}).items() if v},
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


#: The second pass (§49): a free-text question is filled from the profile only
#: when the model is this sure the profile covers it, this sure which key
#: answers it, and this sure the value is a direct answer. Three gates, because
#: a wrong free-text answer is written under the applicant's name.
SECOND_PASS_COVERED = 0.90
SECOND_PASS_KEY = 0.85
SECOND_PASS_VERIFY = 0.90
SECOND_PASS = True

#: Profile keys the second pass may write. Files never; protected and consent
#: sections never; nothing a model composed. Values come from memory.lookup so
#: a phone number is formatted the way the first pass formats it.
_SECOND_PASS_SECTIONS = ("facts", "education", "preferences", "legal_status", "learned")
_SECOND_PASS_EXCLUDE = {"resume_file", "cover_letter", "transcript_file", "personal_preferences"}


#: Contact details the second pass must know *exist*, and under which key,
#: without seeing the value: the model's Choice returns a key, and the binder
#: writes the stored value itself. A "university email" field is answered from
#: `email` either way; the address itself never reaches the provider (audit
#: 2026-10-12, F8). Non-contact facts are still sent as values, because "Do
#: you hold a clearance?" is answered by what the value says.
_CONTACT_SHAPES: Dict[str, str] = {
    "first_name": "the applicant's first name",
    "last_name": "the applicant's last name",
    "full_name": "the applicant's full name",
    "preferred_first_name": "the applicant's preferred first name",
    "preferred_last_name": "the applicant's preferred last name",
    "email": "an email address",
    "alternate_email": "a second email address",
    "phone": "a phone number",
    "linkedin": "a LinkedIn profile URL",
    "github": "a GitHub profile URL",
    "website": "a personal website URL",
    "website_other": "another profile or portfolio URL",
    "links_combined": "the applicant's profile links",
    "street_address": "a street address",
    "street_address_2": "a second street-address line",
    "postal_code": "a postal code",
}

#: Memory keys that are contact details: never in `earlier_answers`, and in
#: the second-pass catalog only as the shapes above. City, location and
#: country are not contact details; a screening question can turn on them.
_CONTACT_KEYS = frozenset(_CONTACT_SHAPES) | frozenset({"twitter", "scholar", "google_scholar", "portfolio"})


def _catalog_value(memory: Memory, key: str) -> Optional[str]:
    """The value the second pass writes for a catalog key, formatted as the
    first pass would format it."""
    value = memory.lookup(key)
    if key == "phone":
        from app.autofill.format import format_phone

        value = format_phone(value)
    return str(value) if value else None


def _profile_catalog(memory: Memory) -> Dict[str, str]:
    """What the second pass may choose from: key -> value, or key -> shape
    for a contact detail. Every key here has a stored, non-blank value."""
    catalog: Dict[str, str] = {}
    for section in _SECOND_PASS_SECTIONS:
        for key, value in (getattr(memory, section, None) or {}).items():
            if key in _SECOND_PASS_EXCLUDE or not value or memory.is_blank(str(value)):
                continue
            catalog[key] = _CONTACT_SHAPES.get(key) or str(value)[:120]
    for key in ("full_name", "country_of_residence", "state_of_residence", "city_of_residence"):
        value = memory.lookup(key)
        if value:
            catalog[key] = _CONTACT_SHAPES.get(key) or str(value)[:120]
    return catalog


def _second_pass_eligible(entry: FillPlanEntry, field: FormField) -> bool:
    if not entry.needs_review or entry.skipped or entry.satisfied_by or entry.attach:
        return False
    if field.kind != FieldKind.TEXT or field.options:
        return False
    if field.field_class != FieldClass.SCREENING or not field.allows_model_judgement():
        return False
    reason = entry.reason or ""
    if "needs your detail" in reason or "form requires it" in reason:
        return False
    if is_follow_up(field.label or ""):
        return False
    return True


def _second_pass(entries: List[FillPlanEntry], form: FormSchema, memory: Memory, binder) -> None:
    """Answer the free-text remainder from the profile, or say plainly it is not there.

    Runs after every other gate over SCREENING text fields still marked for
    review. The model decides whether the profile covers the question and
    which stored value answers it; a second call verifies the value against
    the question; only then is it written, as MODEL_DECISION with the key
    named in the reason. A question the profile does not cover keeps its
    review state and gets the reason "not in your profile", which is the
    yes/no the applicant asked for.
    """
    if not SECOND_PASS or binder is None or not hasattr(binder, "second_pass"):
        return
    fields = {f.key: f for f in form.fields}
    candidates = [(index, entry) for index, entry in enumerate(entries)
                  if fields.get(entry.field_key) is not None and _second_pass_eligible(entry, fields[entry.field_key])]
    if not candidates:
        return
    catalog = _profile_catalog(memory)
    if not catalog:
        return
    state = _answer_state(form, memory, _earlier_answers(form, {e.field_key: e for e in entries}))
    state["profile"] = catalog
    requests = [("sp_{}".format(index), fields[entry.field_key], catalog) for index, entry in candidates]
    decided = binder.second_pass(state, requests)

    to_verify = []
    for index, entry in candidates:
        key, p_cov, p_key = decided.get("sp_{}".format(index), (None, 0.0, 0.0))
        if key and p_cov >= SECOND_PASS_COVERED and p_key >= SECOND_PASS_KEY:
            to_verify.append((index, entry, key, p_cov, p_key))
        else:
            entry.reason = "not in your profile ({:.0%} that it is covered)".format(p_cov) if p_cov < 0.5 else                 "your profile may cover this ({:.0%}), but no single stored fact answers it".format(p_cov)
    if not to_verify:
        return
    verified = binder.verify_values(state, [("sp_{}".format(i), fields[e.field_key].label, catalog[k]) for i, e, k, _c, _k in to_verify])
    for index, entry, key, p_cov, p_key in to_verify:
        p_ok = verified.get("sp_{}".format(index), 0.0)
        if p_ok < SECOND_PASS_VERIFY:
            entry.reason = "second pass: {} looked like the answer but did not verify ({:.0%})".format(key, p_ok)
            continue
        # The stored value, never the catalog text: for a contact key the
        # catalog holds a shape description, not the value.
        written = _catalog_value(memory, key)
        if not written:
            entry.reason = "second pass: {} was chosen but nothing is stored under it".format(key)
            continue
        entry.value = written
        entry.values = []
        entry.source = FillSource.MODEL_DECISION
        entry.confidence = min(p_cov, p_key, p_ok)
        entry.needs_review = False
        entry.theme = "profile.{}".format(key)
        entry.reason = "second pass: your {} answers this ({:.0%} covered, {:.0%} verified)".format(key, p_cov, p_ok)


#: The only classes whose settled answers may be shown to the model as
#: "earlier answers on this form". LEGAL (EEOC, veteran, disability) and
#: CONSENT answers are the applicant's own and never leave the deterministic
#: path; DOCUMENT values are file paths; NARRATIVE is prose. Before this
#: filter every Greenhouse form with a compliance block sent the race, gender,
#: veteran and disability answers to the answer gate and the second pass
#: (audit 2026-10-12, F1).
_EARLIER_ANSWER_CLASSES = frozenset({FieldClass.CORE, FieldClass.SCREENING})


def _earlier_answers(form: FormSchema, answered: Dict[str, object]) -> List[Dict[str, object]]:
    """The answers already settled on this form, as the model may see them.

    `answered` maps a field key to a Resolution or FillPlanEntry. An answer
    counts only when it is a confident fill (not review, not skipped, not
    satisfied by a sibling, not an attachment) on a CORE or SCREENING field
    that is not a file. One builder for the three gates, so the class filter
    cannot drift between them.
    """
    earlier: List[Dict[str, object]] = []
    for field in form.fields:
        if field.field_class not in _EARLIER_ANSWER_CLASSES or field.kind == FieldKind.FILE:
            continue
        item = answered.get(field.key)
        if item is None:
            continue
        value = getattr(item, "values", None) or getattr(item, "value", None)
        if not value:
            continue
        if (getattr(item, "needs_review", False) or getattr(item, "skipped", None)
                or getattr(item, "satisfied_by", None) or getattr(item, "attach", None)):
            continue
        # A contact detail is not an earlier answer the gate can use: a name,
        # email, phone, address or profile link answers no screening
        # question, and the core fields are answered on every form, so the
        # address travelled every time (F8). Dropped by memory key, and by
        # shape for anything that is itself an address or a link.
        key = memory_key_for(field.key, field.label, field.kind)
        if key in _CONTACT_KEYS or any(_CONTACT_SHAPED.search(str(v)) for v in (value if isinstance(value, list) else [value])):
            continue
        earlier.append({"question": (field.label or "")[:120], "answer": value})
    return earlier


#: An answer that is itself an address or a link, whatever key produced it.
_CONTACT_SHAPED = re.compile(r"@|https?://|\bwww\.", re.I)


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

    state = _answer_state(form, memory, _earlier_answers(form, resolved))
    requests = [("spec_{}".format(f.key), f) for f in candidates]
    match_requests = [("match_{}".format(f.key), value, f) for f, value in mismatches]

    with ThreadPoolExecutor(max_workers=2) as pool:
        answer_future = pool.submit(binder.answer_from_profile, state, requests) if requests else None
        match_future = pool.submit(binder.match_options, match_requests) if match_requests else None
        themes = binder.classify_themes(labels)
        answers, matches = {}, {}
        # A failed speculation costs nothing: the sequential gates still run.
        # Logged by type only (F21): a model error's message can echo the
        # request, and the request carries the applicant's answers.
        try:
            answers = answer_future.result() if answer_future else {}
        except Exception as e:  # noqa: BLE001
            logger.warning("speculative profile-answer gate failed (%s)", type(e).__name__)
        try:
            matches = match_future.result() if match_future else {}
        except Exception as e:  # noqa: BLE001
            logger.warning("speculative option-match gate failed (%s)", type(e).__name__)

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
        earlier = _earlier_answers(form, {e.field_key: e for e in entries})
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


_DOCUMENT_KINDS = (
    ("transcript", re.compile(r"\btranscript|academic record\b", re.I)),
    ("cover_letter", re.compile(r"\bcover letter\b|\bcover_letter\b", re.I)),
    ("portfolio", re.compile(r"\bportfolio|work sample|writing sample|design sample|code sample\b", re.I)),
    ("resume", re.compile(r"\b(resume|résumé|cv|curriculum vitae)\b|_systemfield_resume", re.I)),
)


def document_kind(field: FormField) -> str:
    """What a file field asks for, from its key and label."""
    text = "{} {}".format(field.key or "", field.label or "")
    for kind, pattern in _DOCUMENT_KINDS:
        if pattern.search(text):
            return kind
    return "other"


_UNSET = re.compile(r"recognised as ([a-z_]+) but you have not set it|set consents\.([a-z_]+) once|you have not set this( for ([A-Z]{2}))?$")


def _mark_country_codes(entries: List[FillPlanEntry], form: FormSchema) -> None:
    """Attach iso2 / dial_code to a country select's entry (see FillPlanEntry).

    Before the model gates, so the codes come from the stored country and
    not from whatever option label a gate translates it to. Only a field
    that *is* the country of residence: Greenhouse's synthetic `country`,
    or a custom question whose key is country_of_residence. A country named
    inside another question (citizenship, work authorisation) is not one.
    """
    from app.autofill.format import country_codes

    fields = {f.key: f for f in form.fields}
    for entry in entries:
        if not entry.value or entry.skipped or entry.satisfied_by:
            continue
        field = fields.get(entry.field_key)
        if field is None:
            continue
        if not (field.key == "country" or memory_key_for(field.key, field.label, field.kind) == "country_of_residence"
                or entry.theme == QuestionTheme.COUNTRY_OF_RESIDENCE.value):
            continue
        codes = country_codes(entry.value)
        if codes is not None:
            entry.iso2, entry.dial_code = codes


def _mark_missing_facts(entries: List[FillPlanEntry], form: Optional[FormSchema] = None) -> None:
    """Turn "recognised as X but you have not set it" into a question to ask."""
    from app.autofill.memory import profile_schema
    schema = {row["key"]: row for row in profile_schema()}
    fields = {f.key: f for f in form.fields} if form is not None else {}
    for entry in entries:
        if not entry.needs_review or entry.skipped or entry.satisfied_by:
            continue
        # A standing consent is never typed into a text control (M1), so
        # asking the applicant to set it would not fill this field: "If you
        # wish to receive information via text/SMS, please indicate the cell
        # number below" sent them to set consents.sms_messages, which then
        # did nothing here (review of 2026-10-12, finding 7).
        field = fields.get(entry.field_key)
        if (field is not None and field.field_class == FieldClass.CONSENT
                and field.kind in (FieldKind.TEXT, FieldKind.LONG_TEXT)):
            continue
        m = _UNSET.search(entry.reason or "")
        if not m:
            continue
        key = m.group(1) or m.group(2)
        if not key:
            continue
        row = schema.get(key)
        if row is None and key in DERIVED_SOURCES:
            # A derived key (education_start_month) is read from a stored
            # fact (start_date): ask for the fact, once, in its own shape.
            _section, key = DERIVED_SOURCES[key]
            row = schema.get(key)
        if row is None:
            continue
        entry.needs_fact = {
            "section": row["section"], "key": key,
            "prompt": "{} asks: {}".format("This form", (entry.label or key)[:120]),
            "example": row["example"], "note": row["note"],
        }


def _mark_attachments(entries: List[FillPlanEntry], form: FormSchema) -> None:
    """Every file field gets the document kind it asks for; a resolved one
    becomes an attachment instruction, an unresolved one a note naming the
    kind — and a required one stays visibly required."""
    fields = {f.key: f for f in form.fields}

    for entry in entries:
        field = fields.get(entry.field_key)
        if field is None or field.kind != FieldKind.FILE:
            continue
        entry.document = document_kind(field)
        if entry.skipped:
            continue
        if entry.value is None or entry.needs_review:
            if entry.document == "other":
                entry.reason = "asks for a file this profile does not hold ({}){}".format(
                    (field.label or "")[:40], "; required" if field.required else "")
            else:
                entry.reason = "attach your {}{}".format(entry.document.replace("_", " "),
                                                        " — required" if field.required else "")
            entry.needs_review = True
            continue
        entry.attach = entry.value
        entry.reason = "attach this file yourself — a browser will not let a script do it"


#: One question shipped as two controls, by the vendor's own keys.
_VENDOR_ALTERNATES = (frozenset({"resume", "resume_text"}), frozenset({"cover_letter", "cover_letter_text"}))


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
    by_label: Dict[object, List[FillPlanEntry]] = {}
    fields = {f.key: f for f in form.fields}

    for entry in entries:
        field = fields.get(entry.field_key)
        label = field.label if field is not None else entry.label
        # A blank label is the absence of a question, not a shared one. On a
        # real Ashby form the radio groups (no legend), a bare file input, two
        # unlabelled checkboxes and the reCAPTCHA textarea all have empty
        # labels; grouping them made a filled gender radio "answer" all of
        # them, which hid a pronoun group and credited a CAPTCHA as done.
        if not (label or "").strip():
            continue
        # Same label is not the same question. The vendor's own alternate
        # pairs are grouped by key; anything else only with the same class
        # and kind, and never a follow-up ("If yes, please specify" recurs
        # under several questions on one form). A reference's "Email" is a
        # SCREENING text field; the applicant's is CORE — not one question
        # (audit 2026-10-12, F20).
        pair = next((p for p in _VENDOR_ALTERNATES if entry.field_key in p), None)
        if pair is not None:
            group_key: object = pair
        elif field is not None:
            if is_follow_up(label) or _CONDITIONAL.search(label):
                continue
            group_key = (label, field.field_class, field.kind)
        else:
            continue
        by_label.setdefault(group_key, []).append(entry)

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


#: Where a STORED or COMPUTED theme may write: a short answer on a core or
#: screening control. A theme reads a label, and a label is not the whole
#: question — "Please explain any gaps in your résumé or a low GPA"
#: (textarea) was filled with the GPA, and "Upload a zip of your code
#: sample" (file) became an instruction to attach the postal code (audit
#: 2026-10-12, F33). Prose and files take no theme value; a file field's
#: document comes from memory's document keys in gate 1.
_THEME_CLASSES = frozenset({FieldClass.CORE, FieldClass.SCREENING})
_THEME_KINDS = frozenset({FieldKind.TEXT, FieldKind.SINGLE_SELECT, FieldKind.MULTI_SELECT, FieldKind.BOOLEAN})


def _theme_may_fill(field: FormField) -> bool:
    return field.field_class in _THEME_CLASSES and field.kind in _THEME_KINDS


#: A bare core alias on a custom key: "Email", "Phone", "Name", "Company".
#: On a reference or employment block these are someone else's details.
_BARE_CORE_ALIASES = frozenset({
    "email", "e mail", "email address", "phone", "phone number", "telephone", "mobile", "mobile phone",
    "mobile number", "cell phone", "name", "full name", "first name", "last name", "company", "company name",
    "employer",
})
_STORED_AS = re.compile(r"^stored as ([a-z_]+)$")


def _second_core_alias_guard(field: FormField, resolution: Resolution, vendor_answered: Dict[str, str]) -> Resolution:
    """Send a second bare "Email" / "Phone" / "Name" on one form to review.

    A vendor-keyed core field (`email`, `_systemfield_email`, …) answers the
    applicant's own detail; a later custom-keyed field with the same bare
    label and the same memory key is a reference's or an employer's, and
    was filled with the applicant's at confidence 1.0 (audit 2026-10-12,
    F18, probe p10). "Legal First Name" is not bare and still fills.
    Records what vendor keys answered as a side effect, in form order.
    """
    if resolution.needs_review or resolution.skipped or not (resolution.value or resolution.values):
        return resolution
    match = _STORED_AS.match(resolution.reason or "")
    if not match:
        return resolution
    key = match.group(1)
    if field.key in CORE_FIELD_NAMES:
        vendor_answered.setdefault(key, field.key)
        return resolution
    if normalize_label(field.label) not in _BARE_CORE_ALIASES:
        return resolution
    already = key in vendor_answered or (
        key == "full_name" and ("first_name" in vendor_answered or "last_name" in vendor_answered))
    if not already:
        return resolution
    return Resolution(
        field_key=field.key,
        source=FillSource.HUMAN,
        confidence=0.0,
        needs_review=True,
        reason="a second {} on this form — a reference's or an employer's?".format((field.label or "").strip()),
    )


def _model_gate(name: str, gate, *args) -> None:
    """Run one model-backed pass; a model failure leaves its entries for review.

    Only the exception type is logged, never its message: an upstream error
    body can echo the request, and the request carries the applicant's
    answers (audit 2026-10-12, F9/F21).
    """
    from app.exceptions import JevServiceError

    try:
        gate(*args)
    except JevServiceError as e:
        logger.warning("%s gate skipped after a model error (%s); the plan was finished deterministically",
                       name, type(e).__name__)


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
    # Memory keys a vendor-keyed field has already answered on this form,
    # for the bare-alias guard below (F18).
    vendor_answered: Dict[str, str] = {}

    for field in form.fields:
        resolution = _within_options(memory.resolve(field), field)
        if resolution is None:
            resolution = (_class_standing_answer(field, memory) or _based_in_country_answer(field, memory)
                          or _graduation_by_date_answer(field, memory) or _other_degree_level_answer(field, memory))
        if resolution is not None:
            resolution = _second_core_alias_guard(field, resolution, vendor_answered)
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

            if theme != QuestionTheme.UNKNOWN and confidence >= SUGGEST_CONFIDENCE and _theme_may_fill(field):
                kind = THEME_RESOLVERS.get(theme, Resolver.ASK)

                if kind == Resolver.COMPUTED:
                    resolution = _within_options(_resolve_computed(
                        theme, field, memory, company, form.country, form.remote
                    ), field)
                elif kind == Resolver.STORED:
                    # Through the same guard as computed themes: Siftstack
                    # asked "Do you have any offers…?" as a checkbox and the
                    # timeline theme handed it the stored "None".
                    resolution = _within_options(_resolve_stored(theme, field, memory), field)

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
                    field_class=field.field_class.value,
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
                field_class=field.field_class.value,
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
    _mark_missing_facts(entries, form)
    _mark_country_codes(entries, form)
    # The model gates run behind the Jev binder's circuit breaker: once a
    # call has timed out, every later call raises at once. A tripped breaker
    # must finish the plan deterministically — the entries it would have
    # improved stay for review — rather than fail the whole plan; the
    # speculative pool already swallows it, the sequential gates did not.
    _model_gate("option translation", _resolve_option_mismatches, entries, form, binder, matched)
    _mark_satisfied_alternates(entries, form)
    _resolve_conditionals(entries, form)
    _model_gate("profile answer", _answer_from_profile, entries, form, memory, binder, speculative)
    _model_gate("second pass", _second_pass, entries, form, memory, binder)

    return FillPlan(
        posting_id=form.posting_id,
        company=form.company,
        title=form.title,
        entries=entries,
    )
