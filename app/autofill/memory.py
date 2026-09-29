"""
The applicant's stored answers — the thing that makes autofill worth having.

The product's whole value is here rather than in any model: you know you are
not a veteran, you know your own email. Measured across 150 live Greenhouse
postings, **67.4% of fields need no model at all**, and they are answered from
this store. The model's job is only the residual.

Three design points, each forced by real form data:

  1. **Name parts are stored, never the composite.** Ashby collects one `Name`
     field where Greenhouse splits `first_name`/`last_name`. Composing
     "Ada Lovelace" from parts is exact; splitting a full name back into parts
     breaks on the first double surname. So parts are the truth and the
     composite is derived.

  2. **Some answers are parameterised, not stored.** "Have you worked for
     Databricks before?" recurs ~109 times across postings in four phrasings,
     and the answer depends on the company being applied to — it is a lookup
     against the applicant's own employment history, which is exact where a
     model would guess. Work authorisation is parameterised by country: both
     US and Canada variants appear.

  3. **Protected characteristics live in their own section.** Not because they
     are unfillable — replaying an answer the applicant gave is the entire
     point — but because a structural separation means no code path can treat
     one as an ordinary inferable fact by accident. It mirrors the
     `ALLOWED_FILL_SOURCES` discipline in schema.py: policy expressed as
     structure rather than as a rule someone has to remember.
"""
import hashlib
import json
import os
import re
from typing import ClassVar, Dict, List, Optional

from pydantic import BaseModel, Field

from app.autofill.classify import memory_key_for, normalize_label
from app.autofill.format import OPTION_MISMATCH
from app.autofill.schema import FieldClass, FieldKind, FillSource, FormField

#: Legal-entity suffixes, dropped when comparing company names. "Databricks"
#: in a resume and "Databricks, Inc." on a form are the same employer.
_COMPANY_SUFFIX = re.compile(
    r"[\s,]+(?:inc|inc\.|llc|l\.l\.c|ltd|ltd\.|limited|corp|corp\.|corporation|"
    r"co|co\.|company|gmbh|plc|sa|ag|bv|pty|holdings|group|labs|technologies)$",
    re.I,
)
_COMPANY_PUNCT = re.compile(r"[^\w\s&+]+")
_WHITESPACE = re.compile(r"\s+")


def normalize_company(name: str) -> str:
    """Canonical form of a company name for comparison.

    Mirrors `app.analysis.gap_analysis.normalize_skill`: comparison against a
    literal string means every spelling variant is a miss, and a miss here
    answers "have you worked here before?" wrongly.

    Args:
        name: A company name from a resume or a form

    Returns:
        Lowercased, suffix-stripped, punctuation-free form
    """
    text = (name or "").strip().lower()
    text = _COMPANY_PUNCT.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()

    # Applied repeatedly: "Acme Labs, Inc." carries two.
    previous = None
    while previous != text:
        previous = text
        text = _COMPANY_SUFFIX.sub("", text).strip()

    return text


class Story(BaseModel):
    """A grounded narrative span, usable as evidence in an essay.

    Sourced from the existing pipeline's `improve_resume` output, whose bullets
    are already action-verb + technical-context + metric. That node therefore
    produces the essay layer's evidence pool for free.
    """

    key: str
    text: str
    themes: List[str] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    source: str = Field(default="", description="Provenance, e.g. improved_resume:3")


class Resolution(BaseModel):
    """One field's proposed value, and where it came from.

    `source` is load-bearing rather than informational: it is what the review
    UI shows and what the safety invariant is asserted against.
    """

    field_key: str
    value: Optional[str] = None

    #: Several option labels, for a multi-select. Kept separate from `value`
    #: rather than overloading it, because a consumer ticking checkboxes needs
    #: a list and one joining a string needs a scalar; conflating them makes
    #: every consumer handle both.
    values: List[str] = Field(default_factory=list)

    source: FillSource
    confidence: float = 1.0
    needs_review: bool = False
    reason: Optional[str] = None

    #: Set when a standing decision says to leave this blank. Distinct from
    #: needs_review (nothing to do) and from a value (nothing goes in).
    skipped: Optional[str] = None


_DEGREE_TYPES = (
    # Greenhouse's own vocabulary (board /education/degrees, 10 entries,
    # measured 2026-09-27), so an exact option match is possible.
    (re.compile(r"^(m\.?\s?b\.?\s?a\b|master of business)", re.I), "Master of Business Administration (M.B.A.)"),
    (re.compile(r"^(j\.?\s?d\b|juris doctor)", re.I), "Juris Doctor (J.D.)"),
    (re.compile(r"^(m\.?\s?d\b|doctor of medicine)", re.I), "Doctor of Medicine (M.D.)"),
    (re.compile(r"^(ph\.?\s?d|doctor of philosophy|d\.?phil)", re.I), "Doctor of Philosophy (Ph.D.)"),
    (re.compile(r"^(m\.?\s?(s|a|eng|sc|ba)\b|master)", re.I), "Master's Degree"),
    (re.compile(r"^(b\.?\s?(s|a|sc|eng|se)\b|bachelor)", re.I), "Bachelor's Degree"),
    (re.compile(r"^(a\.?\s?(a|s)\b|associate)", re.I), "Associate's Degree"),
)

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _degree_type(education: Dict[str, str]) -> Optional[str]:
    """"BS Computer Science" -> "Bachelor's Degree", in Greenhouse's own words."""
    degree = (education.get("degree") or "").strip()
    for pattern, label in _DEGREE_TYPES:
        if pattern.search(degree):
            return label
    return None


def _discipline(education: Dict[str, str]) -> Optional[str]:
    """"BS Computer Science" / "Bachelor of Science in Computer Science" -> "Computer Science"."""
    degree = (education.get("degree") or "").strip()
    if not degree:
        return None
    if " in " in degree:
        return degree.split(" in ", 1)[1].strip() or None
    rest = re.sub(r"^(ph\.?\s?d\.?|[bma]\.?\s?(s|a|sc|eng|se|ba)\.?|bachelor'?s?|master'?s?|associate'?s?)\s*(of\s+\w+\s*)?",
                  "", degree, flags=re.I).strip(" ,-")
    return rest or None


def _month_of(text: str) -> Optional[str]:
    for month in _MONTHS:
        if re.search(r"\b" + month[:3], text or "", re.I):
            return month
    return None


def _year_of(text: str) -> Optional[str]:
    match = re.search(r"\b(20\d\d|19\d\d)\b", text or "")
    return match.group(1) if match else None


_US_STATE_NAMES = {
    "al": "Alabama", "ak": "Alaska", "az": "Arizona", "ar": "Arkansas", "ca": "California", "co": "Colorado",
    "ct": "Connecticut", "de": "Delaware", "fl": "Florida", "ga": "Georgia", "hi": "Hawaii", "id": "Idaho",
    "il": "Illinois", "in": "Indiana", "ia": "Iowa", "ks": "Kansas", "ky": "Kentucky", "la": "Louisiana",
    "me": "Maine", "md": "Maryland", "ma": "Massachusetts", "mi": "Michigan", "mn": "Minnesota",
    "ms": "Mississippi", "mo": "Missouri", "mt": "Montana", "ne": "Nebraska", "nv": "Nevada",
    "nh": "New Hampshire", "nj": "New Jersey", "nm": "New Mexico", "ny": "New York", "nc": "North Carolina",
    "nd": "North Dakota", "oh": "Ohio", "ok": "Oklahoma", "or": "Oregon", "pa": "Pennsylvania",
    "ri": "Rhode Island", "sc": "South Carolina", "sd": "South Dakota", "tn": "Tennessee", "tx": "Texas",
    "ut": "Utah", "vt": "Vermont", "va": "Virginia", "wa": "Washington", "wv": "West Virginia",
    "wi": "Wisconsin", "wy": "Wyoming", "dc": "District of Columbia",
}


def _split_location(facts: Dict[str, str]):
    """"Ithaca, NY" -> ("Ithaca", "New York"); explicit city/state facts win."""
    city = (facts.get("city") or "").strip()
    state = (facts.get("state") or "").strip()
    location = (facts.get("current_location") or "").strip()
    parts = [x.strip() for x in location.split(",") if x.strip()]
    if not city and parts:
        city = parts[0]
    if not state and len(parts) >= 2:
        tail = parts[1].split()[0].lower() if parts[1] else ""
        state = _US_STATE_NAMES.get(tail, parts[1])
    return city or None, state or None


_US_STATE_ABBR = {"al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia","ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj","nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt","va","wa","wv","wi","wy","dc"}


def _country_of_residence(facts: Dict[str, str]) -> Optional[str]:
    """"Ithaca, NY" -> "United States": the country the applicant lives in.

    Explicit `country` wins; otherwise a US state abbreviation or name at the
    end of `current_location` means the United States. Anything else stays
    None and the form's country select goes to review.
    """
    explicit = (facts.get("country") or "").strip()
    if explicit:
        return explicit
    location = (facts.get("current_location") or "").strip()
    if not location:
        return None
    tail = re.split(r"[,\s]+", location.lower())[-1] if location else ""
    if tail in _US_STATE_ABBR or tail == "usa" or "united states" in location.lower():
        return "United States"
    return None


#: Values the education block asks for that the profile stores in another
#: shape. Derived here so `graduation_date: "May 2028"` answers both the month
#: select and the year field, and one `degree` string answers degree type and
#: discipline. Anything that does not parse stays None and goes to review.
_DERIVED_EDUCATION = {
    "degree_type": _degree_type,
    "discipline": _discipline,
    "graduation_month": lambda e: _month_of(e.get("graduation_date") or ""),
    "graduation_year": lambda e: _year_of(e.get("graduation_date") or ""),
    "education_start_month": lambda e: _month_of(e.get("start_date") or ""),
    "education_start_year": lambda e: _year_of(e.get("start_date") or ""),
}


class Memory(BaseModel):
    """Everything the applicant has told us once.

    Sections are separate so that policy is structural. `protected` is not a
    dumping ground for sensitive strings — it is the section no inference path
    is allowed to write to or read from.
    """

    #: Canonical key -> value. Keys come from classify.py's tables, which are
    #: the single source of truth for what a key is called.
    facts: Dict[str, str] = Field(default_factory=dict)

    #: Stated preferences: heard_about, open_to_relocation, earliest_start.
    preferences: Dict[str, str] = Field(default_factory=dict)

    #: e.g. {"work_authorization": {"US": "Yes", "CA": "No"}}
    per_country: Dict[str, Dict[str, str]] = Field(default_factory=dict)

    #: veteran_status, disability_status, race_ethnicity, gender. MEMORY-only:
    #: replayed verbatim, never inferred. "I decline to self-identify" is a
    #: perfectly good stored answer and the most common one people choose.
    protected: Dict[str, str] = Field(default_factory=dict)

    #: Academic facts: university, degree, graduation_date, gpa,
    #: enrollment_status. Its own section because for an intern application this
    #: is the single largest answerable cluster — ~70 field instances across 57
    #: SWE-intern postings — and because ResumeParsed.education already supplies
    #: every one of them.
    education: Dict[str, str] = Field(default_factory=dict)

    #: Employers from ResumeParsed.experience; powers "worked here before".
    employers: List[str] = Field(default_factory=list)

    stories: List[Story] = Field(default_factory=list)

    #: Legal status facts, asked in several shapes on intern forms:
    #: "Work Authorization" (x13), "unrestricted right to work" (x4),
    #: "Are you legally authorized to work in the US" (x6). Citizenship is kept
    #: separate from authorisation because they are different questions — a
    #: citizen needs no sponsorship, but an authorised non-citizen may.
    #:
    #: Never inferred. `ALLOWED_FILL_SOURCES` bars every model from these; they
    #: are replayed exactly as stated here.
    legal_status: Dict[str, str] = Field(default_factory=dict)

    #: Memory keys the applicant never supplies — a standing decision, recorded
    #: once and applied thereafter. "These applications do not require a cover
    #: letter, so I always skip it" is an answer to the question, not an absence
    #: of one, and a field handled by a standing decision is not a coverage miss.
    #:
    #: Applies only to fields the form marks optional. A *required* field on the
    #: skip list is surfaced for review instead: silently leaving it blank would
    #: block submission, which is worse than one prompt.
    skip: List[str] = Field(default_factory=list)

    #: Fingerprint -> the value the applicant approved. This is what makes the
    #: tool stop asking: a question answered once, or a value corrected in
    #: review, is replayed verbatim next time.
    answers: Dict[str, str] = Field(default_factory=dict)

    # --- derived facts ------------------------------------------------------

    def full_name(self) -> Optional[str]:
        """Composite name, built from parts. See design point 1."""
        parts = [self.facts.get("first_name"), self.facts.get("last_name")]
        joined = " ".join(p for p in parts if p)

        return joined or self.facts.get("full_name")

    def has_worked_at(self, company: Optional[str]) -> Optional[bool]:
        """Whether the applicant's history includes `company`.

        Returns None when the company is unknown, which is a different answer
        from False and must not be collapsed into it: "no employment history
        recorded" is not "no, I have not worked there".
        """
        known = self.employers or [
            v for v in [self.facts.get("current_employer")] if v
        ]

        if not company or not known:
            return None

        target = normalize_company(company)

        if not target:
            return None

        return any(normalize_company(e) == target for e in known)

    def authorization_for(self, country: str) -> Optional[str]:
        return self.per_country.get("work_authorization", {}).get(country)

    def sponsorship_for(self, country: str) -> Optional[str]:
        return self.per_country.get("needs_sponsorship", {}).get(country)

    # --- the answer log -----------------------------------------------------

    @staticmethod
    def fingerprint(label: str) -> str:
        """Stable id for a question, so the same question is never asked twice.

        Keyed on the normalised label rather than the ATS field name, because
        the field name is allocated per posting: Greenhouse's
        `question_8581808008` differs on every job for the same question.
        """
        return hashlib.sha256(normalize_label(label).encode()).hexdigest()[:16]

    def recall_answer(self, label: str) -> Optional[str]:
        return self.answers.get(self.fingerprint(label))

    def remember_answer(self, label: str, value: str) -> None:
        """Record an approved answer. Also the label-collection mechanism: a
        correction made in review is a labelled example."""
        self.answers[self.fingerprint(label)] = value

    # --- resolution ---------------------------------------------------------

    #: Values people type to mean "I don't have one". Writing the literal word
    #: "None" into a website field is worse than leaving it blank, and the
    #: setup prompt invites exactly this — 8 fields across the corpus were
    #: about to be filled with the string "None".
    #: ClassVar, not a field: Pydantic treats an unannotated class attribute on
    #: a BaseModel as an error, and a plain annotation would make it a field
    #: every profile has to carry.
    BLANK_ANSWERS: ClassVar[frozenset] = frozenset(
        {"none", "n/a", "na", "-", "--", "nil", "nothing", "not applicable", ""}
    )

    def is_blank(self, value: Optional[str]) -> bool:
        """Whether a stored value means "I have nothing for this"."""
        return (value or "").strip().lower() in self.BLANK_ANSWERS

    def lookup(self, key: str) -> Optional[str]:
        """A stored value for a canonical key, searching every section."""
        if key == "full_name":
            return self.full_name()

        if key in _DERIVED_EDUCATION:
            return _DERIVED_EDUCATION[key](self.education)

        if key == "country_of_residence":
            return _country_of_residence(self.facts)
        if key == "state_of_residence":
            return _split_location(self.facts)[1]
        if key == "city_of_residence":
            return _split_location(self.facts)[0]

        for section in (self.facts, self.education, self.legal_status,
                        self.preferences, self.protected):
            if section.get(key):
                return section[key]

        return None

    @staticmethod
    def _as_option(field: FormField, value: str, reason: str) -> Resolution:
        """Express a stored value in the form's own vocabulary.

        Without this, a stored "he/him/his" was written verbatim into a select
        offering "He / Him". Figma happens to word it identically so it worked
        there and nowhere else — the kind of bug that hides behind one lucky
        fixture. A select needs the option's own label, and a multi-select needs
        it as a list.

        An unmatched value keeps the stored text and is flagged with
        OPTION_MISMATCH, which is the signal for the binder's model gate to try
        a semantic match before anyone is asked.
        """
        from app.autofill.format import match_option

        if not field.options:
            return Resolution(field_key=field.key, value=value,
                              source=FillSource.MEMORY, reason=reason)

        option = match_option(value, field)

        if option is None:
            return Resolution(field_key=field.key, value=value,
                              source=FillSource.MEMORY, needs_review=True,
                              reason=OPTION_MISMATCH)

        if field.kind == FieldKind.MULTI_SELECT:
            return Resolution(field_key=field.key, values=[option.label],
                              source=FillSource.MEMORY, reason=reason)

        return Resolution(field_key=field.key, value=option.label,
                          source=FillSource.MEMORY, reason=reason)

    def resolve(self, field: FormField) -> Optional[Resolution]:
        """Answer a field from memory alone, or return None.

        Deliberately does no inference and makes no model call. Returning None
        is the signal to route the field onward to the theme router — it is not
        a failure.

        Args:
            field: The form field to answer

        Returns:
            A Resolution sourced from MEMORY, or None
        """
        # Policy first. A field memory is not permitted to fill is not filled,
        # whatever happens to be stored.
        if not field.may_fill_from(FillSource.MEMORY):
            return Resolution(
                field_key=field.key,
                source=FillSource.HUMAN,
                needs_review=True,
                reason="this question is only ever answered by you",
            )

        # An answer previously given or corrected wins over everything: it is
        # the applicant's own words about this exact question.
        remembered = self.recall_answer(field.label)
        if remembered:
            return self._as_option(field, remembered, "you answered this before")

        key = memory_key_for(field.key, field.label, field.kind)

        if key and key in self.skip:
            if field.required:
                return Resolution(
                    field_key=field.key,
                    source=FillSource.MEMORY,
                    needs_review=True,
                    reason="you normally skip {}, but this form requires it".format(key),
                )
            return Resolution(
                field_key=field.key,
                source=FillSource.MEMORY,
                skipped="you always skip {}".format(key),
            )

        if key:
            value = self.lookup(key)
            if key == "phone":
                # Normalised at fill time, so the profile can hold whatever was
                # convenient to type.
                from app.autofill.format import format_phone

                value = format_phone(value)
            # A "None" answer on an optional field is a decision to leave it
            # blank, and is treated as one. On a *required* field it surfaces,
            # since a blank there would block submission.
            #
            # Never applied to a field with options: "No" is a real answer to a
            # yes/no question, and collapsing it into "blank" would silently
            # drop a legitimate negative.
            if value and self.is_blank(value) and not field.options:
                if field.required:
                    return Resolution(
                        field_key=field.key, source=FillSource.MEMORY,
                        needs_review=True,
                        reason="you recorded no {}, but this form requires it".format(key),
                    )
                return Resolution(
                    field_key=field.key, source=FillSource.MEMORY,
                    skipped="you recorded no {}".format(key),
                )

            if value:
                return self._as_option(field, value, "stored as {}".format(key))
            # A recognised field with nothing stored is a gap in onboarding,
            # not a question for a model.
            return Resolution(
                field_key=field.key,
                source=FillSource.MEMORY,
                needs_review=True,
                reason="recognised as {} but you have not set it".format(key),
            )

        if field.field_class == FieldClass.LEGAL:
            # Recognised as protected but unmatched by key: ask once, then it
            # becomes an `answers` entry and is never asked again.
            return Resolution(
                field_key=field.key,
                source=FillSource.MEMORY,
                needs_review=True,
                reason="answer once during onboarding, then reused",
            )

        return None


#: Default location, outside the repository on purpose. This file holds a real
#: name, phone number, address and protected characteristics; a path inside the
#: working tree is one `git add .` away from being published. jev-apply takes the
#: same approach with ~/.config/jev-apply.
DEFAULT_PROFILE_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "firstplay", "profile.json"
)


def load_memory(path: Optional[str] = None) -> Memory:
    """Read a profile from disk, or return an empty Memory if absent.

    Args:
        path: Profile location; defaults to DEFAULT_PROFILE_PATH

    Returns:
        The stored Memory
    """
    target = path or DEFAULT_PROFILE_PATH

    if not os.path.exists(target):
        return Memory()

    with open(target) as handle:
        return Memory(**json.load(handle))


def save_memory(memory: Memory, path: Optional[str] = None) -> str:
    """Write a profile to disk with owner-only permissions.

    0600 because this file contains protected characteristics and contact
    details. Written to a temporary file and moved into place, so an interrupted
    write cannot leave a half-serialised profile behind.

    Args:
        memory: The profile to store
        path: Destination; defaults to DEFAULT_PROFILE_PATH

    Returns:
        The path written
    """
    target = path or DEFAULT_PROFILE_PATH
    os.makedirs(os.path.dirname(target), exist_ok=True)

    temporary = target + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(memory.model_dump(), handle, indent=2, sort_keys=True)

    os.replace(temporary, target)
    os.chmod(target, 0o600)

    return target


#: Every key the resolver knows how to use, grouped as the profile stores them,
#: with the number of field instances each answers across 57 live SWE-intern
#: postings. Ordered by that count, so filling the file top-down buys the most
#: coverage first.
PROFILE_FIELDS = {
    "facts": [
        ("first_name", "Ada", 57),
        ("last_name", "Lovelace", 57),
        ("email", "ada@example.com", 57),
        ("phone", "555-0100", 57),
        ("linkedin", "https://linkedin.com/in/ada", 49),
        ("preferred_first_name", "Ada", 28),
        ("preferred_last_name", "Lovelace", 2),
        ("pronouns", "she/her", 14),
        ("website", "https://ada.dev", 6),
        ("website_other", "https://github.com/ada", 6),
        ("github", "https://github.com/ada", 8),
        ("resume_file", "~/Documents/resume.pdf", 57),
        ("cover_letter", "~/Documents/cover_letter.pdf", 36),
        ("links_combined", "github.com/you | linkedin.com/in/you", 8),
        ("transcript_file", "~/Documents/transcript.pdf", 8),
        ("alternate_email", "you@gmail.com", 2),
        ("current_employer", "Analytical Engines", 8),
        ("current_job_title", "Software Engineering Intern", 8),
        ("current_location", "San Francisco, CA", 5),
        ("street_address", "", 2),
        ("postal_code", "94105", 13),
        ("country_of_residence", "United States", 8),
    ],
    "education": [
        ("university", "Cornell University", 14),
        ("start_date", "August 2024", 1),
        ("graduation_date", "May 2028", 20),
        ("degree", "BS Computer Science", 5),
        ("gpa", "3.8", 12),
        ("enrollment_status", "Currently enrolled", 9),
        ("university_country", "United States", 7),
    ],
    "legal_status": [
        ("citizenship", "US citizen", 0),
        ("work_authorization", "Yes", 13),
        ("needs_sponsorship", "No", 19),
        ("age_18_plus", "Yes", 5),
    ],
    "preferences": [
        ("heard_about", "LinkedIn", 12),
        ("internship_term", "Summer", 8),
        ("internship_term_if_remote", "Winter", 0),
        ("term_flexible", "Yes", 6),
        ("engineering_track", "Backend", 9),
        ("open_to_relocation", "Yes", 3),
        ("in_office_tolerance", "Yes", 7),
        ("earliest_start", "June 2027", 4),
        ("timeline_notes", "None", 8),
        ("contact_current_employer", "Yes", 12),
        ("personal_preferences", ""),
    ],
    "protected": [
        ("accommodation_needs", "None", 13),
        ("government_official", "No", 10),
        ("close_relative_official", "No", 10),
        ("familial_relationship", "No", 10),
        ("essential_functions", "Yes", 6),
        ("veteran_status", "I am not a protected veteran", 27),
        ("race_ethnicity", "Decline to self-identify", 27),
        ("gender", "Decline to self-identify", 27),
        ("disability_status", "I do not want to answer", 6),
    ],
}


def profile_template() -> Dict[str, object]:
    """A profile skeleton with every known key present and empty.

    Every key is included rather than only the common ones, because a key that
    is absent looks identical to a key the resolver does not know about — and
    the second is a bug while the first is just an unanswered question.
    """
    template: Dict[str, object] = {}

    for section, entries in PROFILE_FIELDS.items():
        template[section] = {name: "" for name, _example, *_rest in entries}

    template["per_country"] = {
        "work_authorization": {"US": "", "CA": ""},
        "needs_sponsorship": {"US": "", "CA": ""},
    }
    template["employers"] = []
    template["answers"] = {}

    return template


def memory_from_resume(resume, improved=None) -> Memory:
    """Seed a Memory from the existing pipeline's output.

    Reuses `app.schemas.ResumeParsed` and `ImprovedResumeParsed` rather than
    asking the applicant to retype what the resume already says. The improved
    resume's bullets become the essay evidence pool, already shaped as action
    verb + technical context + metric by the `improve_resume` node.

    Args:
        resume: A ResumeParsed
        improved: An optional ImprovedResumeParsed

    Returns:
        A Memory with facts, employers and stories populated
    """
    facts: Dict[str, str] = {}

    if resume.name:
        parts = resume.name.split()
        if len(parts) >= 2:
            facts["first_name"] = parts[0]
            facts["last_name"] = " ".join(parts[1:])
        facts["full_name"] = resume.name

    if resume.email:
        facts["email"] = resume.email
    if resume.phone:
        facts["phone"] = resume.phone

    employers = [e.company for e in resume.experience if e.company]

    # The education bridge. ResumeParsed.education carries exactly the fields
    # intern forms ask for, so nothing here has to be retyped.
    education: Dict[str, str] = {}
    if resume.education:
        first = resume.education[0]
        # Left unset rather than guessed: an institution name does not reliably
        # name its country, and a wrong answer here is a false statement about
        # where the applicant studies.
        if first.institution:
            education["university"] = first.institution
        if first.degree:
            education["degree"] = first.degree
        if first.graduation_date:
            education["graduation_date"] = first.graduation_date
        if first.gpa:
            education["gpa"] = first.gpa

    # Current employer/title, asked as short facts on ~28 intern fields.
    if resume.experience:
        latest = resume.experience[0]
        if latest.company:
            facts["current_employer"] = latest.company
        if latest.title:
            facts["current_job_title"] = latest.title

    stories: List[Story] = []
    if improved is not None:
        for index, experience in enumerate(improved.experience):
            for bullet_index, bullet in enumerate(experience.bullets):
                stories.append(
                    Story(
                        key="exp-{}-{}".format(index, bullet_index),
                        text=bullet,
                        themes=["experience"],
                        source="improved_resume:experience:{}".format(index),
                    )
                )
        for index, project in enumerate(improved.projects):
            for bullet_index, bullet in enumerate(project.bullets):
                stories.append(
                    Story(
                        key="proj-{}-{}".format(index, bullet_index),
                        text=bullet,
                        themes=["project"],
                        skills=list(project.technologies),
                        source="improved_resume:projects:{}".format(index),
                    )
                )

    return Memory(facts=facts, education=education, employers=employers, stories=stories)


#: Memory keys that are *derived* from another, and so are not separate
#: onboarding questions. Greenhouse's single "Resume/CV" question ships both
#: `resume` (an upload) and `resume_text` (a paste alternative): two different
#: fill values from one document, so two memory keys, but only one thing to ask
#: the applicant for.
ONBOARDING_GROUPS = {"resume_text": "resume_file"}


def onboarding_questions(forms, min_occurrences: int = 2):
    """The questions worth asking once, ranked by how often they recur.

    This is what makes the onboarding screen derivable from data rather than
    guessed: run it over a corpus of real postings and it reports exactly which
    questions to ask on day one, most valuable first.

    Args:
        forms: An iterable of FormSchema
        min_occurrences: Ignore questions seen fewer times than this

    Returns:
        List of (normalised_label, count, field_class) sorted by count desc
    """
    counts: Dict[str, int] = {}
    classes: Dict[str, str] = {}
    display: Dict[str, str] = {}

    for form in forms:
        for field in form.fields:
            if not field.needs_onboarding_answer():
                continue
            label = normalize_label(field.label)
            if not label:
                continue
            # Grouped by memory key where one exists, so the screen asks for a
            # website once rather than once per spelling: "Website", "Website(s)"
            # and "Websites" are one fact. "Other Website" has its own key and
            # so stays a separate question, correctly.
            group = memory_key_for(field.key, field.label) or label
            group = ONBOARDING_GROUPS.get(group, group)
            counts[group] = counts.get(group, 0) + 1
            classes[group] = field.field_class.value
            # Show the most frequent spelling, not whichever was seen first.
            if group not in display or counts[group] > counts.get(display[group], 0):
                display[group] = label

    ranked = [
        (display.get(group, group), count, classes[group])
        for group, count in counts.items()
        if count >= min_occurrences
    ]

    return sorted(ranked, key=lambda row: (-row[1], row[0]))
