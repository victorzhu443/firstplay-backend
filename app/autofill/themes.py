"""
Question themes: the canonical set of things employers actually ask.

Measured across 150 live Greenhouse postings, the 1,010 screening fields used
only **117 distinct labels**, and those collapse into the handful of themes
below. That collapse is the central design decision of the binding layer, and
it is worth being explicit about why:

  - **A theme is an atomic question.** Jev's own guidance is that each question
    must be atomic or the answer comes back at low confidence. "Which of these
    themes is this label?" is atomic. "Which of my 40 stored facts goes here?"
    is not, and its option set would differ per applicant.

  - **Themes are cacheable; fact bindings are not.** The same label recurs 8.5
    times on average across postings and up to 145 times for the most common.
    A label -> theme mapping is therefore worth caching once and reusing
    forever, which drives the marginal cost of the binding layer toward zero.
    A label -> "Ada's phone number" mapping is useful to nobody else.

  - **Some themes are answered by computing, not by choosing.** Two of the
    largest are parameterised by the posting: "have you worked at {company}
    before?" (~109 occurrences, four phrasings) is a lookup against the
    applicant's own employment history, and work authorisation (~136
    occurrences) is parameterised by country — both US and Canada appear. A
    model asked these would guess; code answers them exactly.

The `criteria` strings are written the way Jev's guidance requires: each
describes an observable situation rather than a degree, so the model is
matching a question against a description rather than judging a vague scale.
"""
import re
from enum import Enum
from typing import Dict, Optional


class Resolver(str, Enum):
    """How a theme's answer is produced once the theme is known."""

    #: Computed from the posting plus the applicant's own history. Exact.
    COMPUTED = "computed"
    #: Read from a stored fact or preference.
    STORED = "stored"
    #: Only the applicant can answer; the answer is then remembered.
    ASK = "ask"


class QuestionTheme(str, Enum):
    # --- academic. The largest intern-specific cluster: ~70 instances across
    # 57 SWE-intern postings, and absent from the general corpus entirely.
    # ResumeParsed.education already carries institution/degree/graduation_date/
    # gpa, so these resolve from data the pipeline has already parsed.
    GRADUATION_DATE = "graduation_date"
    CURRENT_UNIVERSITY = "current_university"
    DEGREE_PROGRAM = "degree_program"
    GPA = "gpa"
    ENROLLMENT_STATUS = "enrollment_status"
    STUDYING_IN_COUNTRY = "studying_in_country"
    GRADUATE_PROGRAM = "graduate_program"

    # --- internship logistics
    INTERNSHIP_TERM = "internship_term"
    INTERNSHIP_AVAILABILITY = "internship_availability"
    ENGINEERING_TRACK = "engineering_track"

    # --- short employment facts, distinct from "worked here before"
    CURRENT_EMPLOYER = "current_employer"
    CONTACT_CURRENT_EMPLOYER = "contact_current_employer"
    CURRENT_JOB_TITLE = "current_job_title"

    # --- location, in the shapes intern forms use
    COUNTRY_OF_RESIDENCE = "country_of_residence"
    POSTAL_CODE = "postal_code"

    AGE_ELIGIBILITY = "age_eligibility"

    WORK_AUTHORIZATION = "work_authorization"
    VISA_SPONSORSHIP = "visa_sponsorship"
    WORKED_HERE_BEFORE = "worked_here_before"
    INTERVIEWED_HERE_BEFORE = "interviewed_here_before"
    CURRENT_LOCATION = "current_location"
    CURRENT_STATE = "current_state"
    WORK_ADDRESS = "work_address"
    WILLING_TO_RELOCATE = "willing_to_relocate"
    IN_OFFICE_TOLERANCE = "in_office_tolerance"
    EARLIEST_START = "earliest_start"
    INTERNSHIP_END = "internship_end"
    DESIRED_SALARY = "desired_salary"
    TIMELINE_NOTES = "timeline_notes"
    HEARD_ABOUT = "heard_about"
    PERSONAL_PREFERENCES = "personal_preferences"
    UNKNOWN = "unknown"


#: Situation descriptions, sent to Jev as Choice criteria. `unknown` is present
#: deliberately: Jev's guidance is to always offer a fallback option rather than
#: force a pick, and an unmatched question should surface as low confidence
#: instead of being crammed into the nearest theme.
THEME_CRITERIA: Dict[str, str] = {
    QuestionTheme.GRADUATION_DATE.value:
        "Asks when the candidate graduates or expects to graduate, as a month, "
        "year, term or date.",
    QuestionTheme.CURRENT_UNIVERSITY.value:
        "Asks which university, college or school the candidate attends.",
    QuestionTheme.DEGREE_PROGRAM.value:
        "Asks what degree, major, programme or field of study the candidate is "
        "enrolled in.",
    QuestionTheme.GPA.value:
        "Asks for the candidate's GPA, grade average or academic marks.",
    QuestionTheme.ENROLLMENT_STATUS.value:
        "Asks whether the candidate is enrolled as a student at all, or what "
        "year of study they are in. Not specific to any degree level.",
    QuestionTheme.STUDYING_IN_COUNTRY.value:
        "Asks whether the candidate attends a university in one specific named "
        "country, such as Canada or Mexico.",
    QuestionTheme.GRADUATE_PROGRAM.value:
        "Asks specifically whether the candidate is in a graduate programme — a "
        "Master's, PhD or doctoral degree — as opposed to an undergraduate one.",
    QuestionTheme.INTERNSHIP_TERM.value:
        "Asks the candidate to choose between internship seasons, terms or "
        "cohorts — for example winter versus summer.",
    QuestionTheme.INTERNSHIP_AVAILABILITY.value:
        "Asks the candidate to confirm yes or no whether they are available for "
        "one specific named term, rather than to choose between terms.",
    QuestionTheme.ENGINEERING_TRACK.value:
        "Asks which kind of engineering work, team or technical track the "
        "candidate is most interested in.",
    QuestionTheme.CURRENT_EMPLOYER.value:
        "Asks the name of the candidate's current or most recent employer.",
    QuestionTheme.CONTACT_CURRENT_EMPLOYER.value:
        "Asks permission to contact the candidate's current or previous "
        "employer as a reference.",
    QuestionTheme.CURRENT_JOB_TITLE.value:
        "Asks the candidate's current or most recent job title.",
    QuestionTheme.COUNTRY_OF_RESIDENCE.value:
        "Asks which country the candidate currently lives in.",
    QuestionTheme.POSTAL_CODE.value:
        "Asks for the candidate's zip or postal code.",
    QuestionTheme.AGE_ELIGIBILITY.value:
        "Asks whether the candidate is above a minimum age, such as 18.",
    QuestionTheme.WORK_AUTHORIZATION.value:
        "Asks whether the candidate is legally authorised or entitled to work "
        "in a particular country, without needing sponsorship.",
    QuestionTheme.VISA_SPONSORSHIP.value:
        "Asks whether the candidate now or in future needs visa, immigration "
        "or employment sponsorship.",
    QuestionTheme.WORKED_HERE_BEFORE.value:
        "Asks whether the candidate has previously been employed by, or "
        "contracted to, this same company.",
    QuestionTheme.INTERVIEWED_HERE_BEFORE.value:
        "Asks whether the candidate has previously interviewed or applied to "
        "this same company.",
    QuestionTheme.CURRENT_LOCATION.value:
        "Asks where the candidate currently lives or is currently based, as a "
        "city, country or yes/no about a region.",
    QuestionTheme.CURRENT_STATE.value:
        "Asks which state, province or region the candidate lives in, usually "
        "as a long list of options.",
    QuestionTheme.WORK_ADDRESS.value:
        "Asks for the street address the candidate would work from.",
    QuestionTheme.WILLING_TO_RELOCATE.value:
        "Asks whether the candidate is willing to move to a different city or "
        "region for the role.",
    QuestionTheme.IN_OFFICE_TOLERANCE.value:
        "Asks whether the candidate is willing to work in an office some or "
        "all days of the week.",
    QuestionTheme.EARLIEST_START.value:
        "Asks the earliest date the candidate could start, or their notice "
        "period.",
    QuestionTheme.INTERNSHIP_END.value:
        "Asks when the candidate's internship or placement would end, or their "
        "ideal or latest end date.",
    QuestionTheme.DESIRED_SALARY.value:
        "Asks what salary, compensation, pay or rate the candidate expects, "
        "desires or requires.",
    QuestionTheme.TIMELINE_NOTES.value:
        "Asks about deadlines, competing offers or timing constraints the "
        "employer should know about.",
    QuestionTheme.HEARD_ABOUT.value:
        "Asks how the candidate found out about this job or company.",
    QuestionTheme.PERSONAL_PREFERENCES.value:
        "Asks an optional personal detail such as name pronunciation or "
        "preferred form of address.",
    QuestionTheme.UNKNOWN.value:
        "None of the other descriptions clearly fits this question.",
}

#: How each theme is answered. Only the STORED and COMPUTED ones can be filled
#: without interrupting the applicant.
THEME_RESOLVERS: Dict[QuestionTheme, Resolver] = {
    QuestionTheme.GRADUATION_DATE: Resolver.STORED,
    QuestionTheme.CURRENT_UNIVERSITY: Resolver.STORED,
    QuestionTheme.DEGREE_PROGRAM: Resolver.STORED,
    QuestionTheme.GPA: Resolver.STORED,
    QuestionTheme.ENROLLMENT_STATUS: Resolver.STORED,
    # Computed by comparing the country named in the question with where the
    # applicant actually studies. Answering it from a generic "Currently
    # Enrolled" is the same generic-onto-specific error that produced a wrong
    # "Yes" for a Masters/PhD question.
    QuestionTheme.STUDYING_IN_COUNTRY: Resolver.COMPUTED,
    # Computed from the stored degree, not stored separately: "BS Computer
    # Science" answers this exactly, and a generic "Currently Enrolled" does
    # not. Matching the generic answer onto this question produced a confident
    # wrong "Yes" for an undergraduate — the first precision failure found.
    QuestionTheme.GRADUATE_PROGRAM: Resolver.COMPUTED,
    # Which cohort: a preference, and conditional on remoteness.
    QuestionTheme.INTERNSHIP_TERM: Resolver.COMPUTED,
    # Whether a named cohort works: a different question, answered from whether
    # the applicant is flexible about terms at all.
    QuestionTheme.INTERNSHIP_AVAILABILITY: Resolver.STORED,
    QuestionTheme.ENGINEERING_TRACK: Resolver.STORED,
    QuestionTheme.CURRENT_EMPLOYER: Resolver.STORED,
    QuestionTheme.CONTACT_CURRENT_EMPLOYER: Resolver.STORED,
    QuestionTheme.CURRENT_JOB_TITLE: Resolver.STORED,
    QuestionTheme.COUNTRY_OF_RESIDENCE: Resolver.STORED,
    QuestionTheme.POSTAL_CODE: Resolver.STORED,
    QuestionTheme.AGE_ELIGIBILITY: Resolver.STORED,
    QuestionTheme.WORK_AUTHORIZATION: Resolver.COMPUTED,
    QuestionTheme.VISA_SPONSORSHIP: Resolver.COMPUTED,
    QuestionTheme.WORKED_HERE_BEFORE: Resolver.COMPUTED,
    QuestionTheme.INTERVIEWED_HERE_BEFORE: Resolver.ASK,
    QuestionTheme.CURRENT_LOCATION: Resolver.STORED,
    QuestionTheme.CURRENT_STATE: Resolver.STORED,
    QuestionTheme.WORK_ADDRESS: Resolver.STORED,
    QuestionTheme.WILLING_TO_RELOCATE: Resolver.STORED,
    QuestionTheme.IN_OFFICE_TOLERANCE: Resolver.STORED,
    QuestionTheme.EARLIEST_START: Resolver.STORED,
    QuestionTheme.INTERNSHIP_END: Resolver.STORED,
    QuestionTheme.DESIRED_SALARY: Resolver.STORED,
    QuestionTheme.TIMELINE_NOTES: Resolver.STORED,
    QuestionTheme.HEARD_ABOUT: Resolver.STORED,
    QuestionTheme.PERSONAL_PREFERENCES: Resolver.STORED,
    QuestionTheme.UNKNOWN: Resolver.ASK,
}

#: Memory key each STORED theme reads.
THEME_MEMORY_KEYS: Dict[QuestionTheme, str] = {
    QuestionTheme.GRADUATION_DATE: "graduation_date",
    QuestionTheme.CURRENT_UNIVERSITY: "university",
    QuestionTheme.DEGREE_PROGRAM: "degree",
    QuestionTheme.GPA: "gpa",
    QuestionTheme.ENROLLMENT_STATUS: "enrollment_status",
    QuestionTheme.INTERNSHIP_AVAILABILITY: "term_flexible",
    QuestionTheme.ENGINEERING_TRACK: "engineering_track",
    QuestionTheme.CURRENT_EMPLOYER: "current_employer",
    QuestionTheme.CONTACT_CURRENT_EMPLOYER: "contact_current_employer",
    QuestionTheme.CURRENT_JOB_TITLE: "current_job_title",
    QuestionTheme.COUNTRY_OF_RESIDENCE: "country_of_residence",
    QuestionTheme.POSTAL_CODE: "postal_code",
    QuestionTheme.AGE_ELIGIBILITY: "age_18_plus",
    QuestionTheme.CURRENT_LOCATION: "current_location",
    QuestionTheme.CURRENT_STATE: "current_state",
    QuestionTheme.WORK_ADDRESS: "work_address",
    QuestionTheme.WILLING_TO_RELOCATE: "open_to_relocation",
    QuestionTheme.IN_OFFICE_TOLERANCE: "in_office_tolerance",
    QuestionTheme.EARLIEST_START: "earliest_start",
    QuestionTheme.INTERNSHIP_END: "internship_end",
    QuestionTheme.DESIRED_SALARY: "desired_salary",
    QuestionTheme.TIMELINE_NOTES: "timeline_notes",
    QuestionTheme.HEARD_ABOUT: "heard_about",
    QuestionTheme.PERSONAL_PREFERENCES: "personal_preferences",
}

#: Countries named in authorisation questions, mapped to the key memory stores
#: them under. Extracted from the label by regex rather than by a model: the
#: country is stated literally, so there is nothing to judge.
_COUNTRY_PATTERNS = (
    ("US", re.compile(r"\b(united states|u\.?s\.?a?|america)\b", re.I)),
    ("CA", re.compile(r"\bcanada\b", re.I)),
    ("UK", re.compile(r"\b(united kingdom|u\.?k\.?|britain)\b", re.I)),
    ("IE", re.compile(r"\bireland\b", re.I)),
    ("DE", re.compile(r"\bgermany\b", re.I)),
    ("IN", re.compile(r"\bindia\b", re.I)),
)


#: US state and territory codes. Greenhouse location strings routinely name a
#: state and no country — "San Francisco, CA", "Menlo Park, CA; New York, NY" —
#: while Canada, Mexico and Singapore postings name the country outright. So a
#: state code is a reliable US signal, but only *after* an explicit country name
#: has been ruled out: "CA" is also the ISO code for Canada.
_US_STATES = frozenset("""
AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT
NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR
""".split())

_STATE_CODE = re.compile(r",\s*([A-Z]{2})\b")


def country_in_text(text: str) -> Optional[str]:
    """Country code named anywhere in a string, or None.

    Used for both a question's wording and a posting's location, which are the
    two places the relevant country is ever stated.
    """
    for code, pattern in _COUNTRY_PATTERNS:
        if pattern.search(text or ""):
            return code

    # Only once no country is named outright, so "Toronto, Canada" is never read
    # as a US state.
    for match in _STATE_CODE.finditer(text or ""):
        if match.group(1) in _US_STATES:
            return "US"

    return None


def country_in_label(label: str) -> Optional[str]:
    """Country code named in an authorisation question, if one is named.

    "Are you legally entitled to work in Canada?" and "...in the United States"
    are different questions with different answers, and both appear in the
    corpus. A single stored boolean would answer one of them wrongly.

    Args:
        label: The question text

    Returns:
        A two-letter code, or None when the question names no country
    """
    for code, pattern in _COUNTRY_PATTERNS:
        if pattern.search(label or ""):
            return code

    return None
