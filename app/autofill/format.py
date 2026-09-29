"""
Turning a stored answer into the exact value a form will accept.

This layer exists because what the applicant reads and what the form submits
are different strings. Greenhouse sends `{"label": "Yes", "value": 1}`, an
arbitration agreement uses `{"label": "I understand and agree...", "value":
44048486008}`, and EEOC options use string digits. So a decision is made over
*labels* and then submitted as the option's *value*.

Deliberately deterministic and deliberately strict. A near-miss here is a wrong
answer on an application, so anything short of an unambiguous match returns None
and the field goes to review instead.
"""
import re
from typing import Optional

from app.autofill.buckets import match_numeric_bucket, match_term_bucket
from app.autofill.schema import FieldOption, FormField

#: Set as a resolution's reason when a stored answer exists but matches no
#: option deterministically. The binder's model gate looks for exactly this.
#: Defined here rather than in binder.py because memory.py needs it too, and
#: memory cannot import binder — binder imports memory.
OPTION_MISMATCH = "stored answer does not clearly match one of the options"

_PUNCT = re.compile(r"[^\w\s+#&]+")
_WHITESPACE = re.compile(r"\s+")

#: Every way a form offers "I would rather not say". Measured on real forms:
#: Greenhouse writes "I don't wish to answer", Ashby writes "Decline to
#: self-identify", and neither matches the other — nor does "Prefer not to
#: say", which is what most people would actually type.
#:
#: Without this class, a protected answer matches one vendor and silently goes
#: to review on the other, which is the difference between answering these
#: questions once and answering them on every application.
_DECLINE = re.compile(
    # Matched against `normalize_value` output, which strips apostrophes to
    # spaces — so "I don't wish to answer" arrives as "i don t wish to answer"
    # and a pattern containing "don't" would never fire. Written against the
    # normalised form on purpose.
    r"\bdecline\b"
    r"|\bprefer not\b"
    r"|\brather not\b"
    r"|\bwish to (answer|disclose|say|self identify)\b"
    r"|\bnot (want|wish) to (answer|disclose|say|self identify)\b"
    r"|\bopt out\b"
    r"|\bno answer\b",
    re.I,
)

#: Spellings that mean yes or no. Forms use all of these.
_AFFIRMATIVE = {"yes", "y", "true", "1", "i am", "i do", "authorized", "agree"}
_NEGATIVE = {"no", "n", "false", "0", "i am not", "i do not", "not authorized"}


def normalize_value(text: Optional[str]) -> str:
    """Canonical form of an option label or a stored answer."""
    lowered = (text or "").strip().lower()
    lowered = _PUNCT.sub(" ", lowered)

    return _WHITESPACE.sub(" ", lowered).strip()


def _as_bool(text: str) -> Optional[bool]:
    normalized = normalize_value(text)

    if normalized in _AFFIRMATIVE:
        return True
    if normalized in _NEGATIVE:
        return False

    return None


#: Digits only, once punctuation and a country code are stripped.
_PHONE_DIGITS = re.compile(r"\d+")


def format_phone(value: Optional[str]) -> Optional[str]:
    """Normalise a phone number to the form US application forms expect.

    Stored however the applicant typed it — "3019063249", "(301) 906-3249",
    "+1 301 906 3249" — and emitted as "301-906-3249", which every US form
    accepts and a recruiter can read. This is why the stored format does not
    matter: the profile holds whatever was convenient to type and the resolver
    emits one canonical form.

    A number that is not a recognisable 10-digit US one is passed through
    unchanged rather than mangled: an international number has its own
    conventions and guessing at them would be worse than leaving it alone.

    Args:
        value: A phone number in any format

    Returns:
        "XXX-XXX-XXXX" for a US number, the original otherwise
    """
    if not value:
        return value

    digits = "".join(_PHONE_DIGITS.findall(value))

    # A leading 1 is the US country code, not an area code.
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    if len(digits) != 10:
        return value

    return "{}-{}-{}".format(digits[:3], digits[3:6], digits[6:])


#: Country spellings that mean the same place. Forms list "US", "USA",
#: "United States" and "United States of America" interchangeably, so a stored
#: "United States" matched nothing on 7 of the option lists measured.
_COUNTRY_ALIASES = {
    "us": "united states", "usa": "united states", "u s": "united states",
    "u s a": "united states", "united states of america": "united states",
    "america": "united states",
    "uk": "united kingdom", "u k": "united kingdom",
    "great britain": "united kingdom", "britain": "united kingdom",
    "england": "united kingdom",
    "uae": "united arab emirates",
    "south korea": "korea republic of", "republic of korea": "korea republic of",
}


def canonical_country(text: Optional[str]) -> Optional[str]:
    """Canonical country name, or None when the text names no known country."""
    normalized = normalize_value(text)

    if not normalized:
        return None

    return _COUNTRY_ALIASES.get(normalized, normalized)


def match_country(value: Optional[str], field: FormField) -> Optional[FieldOption]:
    """The one option naming the same country as `value`."""
    target = canonical_country(value)

    if not target:
        return None

    hits = [o for o in field.options if canonical_country(o.label) == target]

    return hits[0] if len(hits) == 1 else None


_OWN_SITE_ANSWERS = {"careers website", "career website", "careers page", "career page",
                     "company website", "careers site", "career site", "website"}
_OWN_SITE = re.compile(r"\b(website|web site|careers? page|careers? site|careers)\b", re.I)
_THIRD_PARTY = re.compile(r"linkedin|indeed|glassdoor|handshake|ripplematch|wayup|simplify|"
                          r"builtin|levels|blind|repvue|google|job board|university|school", re.I)

_SYNONYMS = {
    "male": ("man",), "man": ("male",),
    "female": ("woman",), "woman": ("female",),
}


def match_option(value: Optional[str], field: FormField) -> Optional[FieldOption]:
    """Find the one option a stored answer unambiguously means.

    Three passes, each requiring a *unique* winner. Ambiguity returns None,
    which routes the field to review rather than picking the first plausible
    option — the failure mode being avoided is a confident wrong selection on
    something like work authorisation.

    Args:
        value: The stored answer
        field: The field whose options are being matched

    Returns:
        The matching option, or None
    """
    if not value or not field.options:
        return None

    target = normalize_value(value)

    if not target:
        return None

    exact = [o for o in field.options if normalize_value(o.label) == target]
    if len(exact) == 1:
        return exact[0]

    # The one vocabulary gap that is unambiguous: the profile stores the EEOC
    # wording ("Male") and self-identification blocks ask in another ("Man").
    synonyms = _SYNONYMS.get(target, ())
    same = [o for o in field.options if normalize_value(o.label) in synonyms]
    if len(same) == 1:
        return same[0]

    # "Careers Website" against a source list: the employer's own site is
    # named after the employer ("Integra FEC Website", "Verkada Careers Page",
    # "DV Website"). 26 such fields in the large corpus, 25 required. Only when
    # exactly one option names a website / careers page and it is not a
    # third-party site.
    if target in _OWN_SITE_ANSWERS:
        own = [o for o in field.options
               if _OWN_SITE.search(o.label) and not _THIRD_PARTY.search(o.label)]
        if len(own) == 1:
            return own[0]

    # Yes/no, where the option text is longer than the answer: a stored "Yes"
    # against "Yes, I am authorized to work in the US".
    wanted = _as_bool(target)
    if wanted is not None:
        matches = [o for o in field.options if _as_bool(o.label) is wanted]
        if len(matches) == 1:
            return matches[0]

        prefixed = [
            o for o in field.options
            if normalize_value(o.label).startswith(target + " ")
        ]
        if len(prefixed) == 1:
            return prefixed[0]

    country = match_country(value, field)
    if country is not None:
        return country

    # Bucketed option lists, which is how intern forms ask for a GPA or a
    # graduation date. Tried before the loose prefix pass below, because "3.8"
    # is a prefix of nothing and "May 2028" is a prefix of nothing either —
    # these need arithmetic and a calendar, not string comparison.
    bucket = match_numeric_bucket(value, field.options)
    if bucket is not None:
        return bucket

    bucket = match_term_bucket(value, field.options)
    if bucket is not None:
        return bucket

    # "I would rather not say", however this form words it. Still requires a
    # unique winner: if a form somehow offers two decline options, that is a
    # choice the applicant has to make.
    if _DECLINE.search(target):
        declines = [o for o in field.options if _DECLINE.search(normalize_value(o.label))]
        if len(declines) == 1:
            return declines[0]

    # A stored answer that is a unique prefix of exactly one option label.
    prefix = [o for o in field.options if normalize_value(o.label).startswith(target)]
    if len(prefix) == 1:
        return prefix[0]

    # And the reverse: an option that is a unique prefix of the stored answer.
    # Pronouns need this in both directions — a profile saying "he/him/his"
    # meets forms offering "He / Him", and requiring an exact match left 13
    # fields unanswered across the corpus. Still requires a unique winner, so
    # "he him" cannot also match "he him hers" if a form offered both.
    contained = [
        o for o in field.options
        if normalize_value(o.label) and target.startswith(normalize_value(o.label))
    ]
    if len(contained) == 1:
        return contained[0]

    return None


def option_for_bool(answer: bool, field: FormField) -> Optional[FieldOption]:
    """The option meaning yes or no on a two-option question.

    Used by the computed themes — "have you worked here before?" produces a
    boolean from employment history, which still has to become whichever of
    this form's options means it.
    """
    matches = [o for o in field.options if _as_bool(o.label) is answer]

    return matches[0] if len(matches) == 1 else None
