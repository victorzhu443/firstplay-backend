"""
Matching a stored answer to a bucketed option list.

Real intern forms rarely offer free text for a GPA or a graduation date. They
offer buckets, and the buckets are worded per company:

    GPA          '2.4 or below' | '2.5 - 2.7 (out of 4.0)' | '3.6 or above (out of 4.0)'
                 '80% or above (Canadian Institutions)' | '70-79% (Canadian Institutions)'
    Graduation   'Earlier than Fall 2027' | 'Fall 2027' | 'Spring 2028'
                 'Later than Summer 2028'

A stored "3.8" matches none of those as a string, and neither does "May 2028".
Both are solved here rather than by a model, deliberately: placing a number in a
range and mapping a month to an academic term are arithmetic and calendar work,
which are two of Jev's documented failure modes. Code gets them exactly right
every time and costs nothing.

What is *not* here is semantic option wording — "Yes" against "I am willing to
relocate before starting employment." That is judgement, not arithmetic, and it
belongs to the model.
"""
import re
from typing import List, Optional, Tuple

from app.autofill.schema import FieldOption

#: "3.6 or above", "80% or above", "3.4 - 3.5", "70-79%", "2.4 or below".
_NUMBER = r"(\d+(?:\.\d+)?)"
_RANGE = re.compile(_NUMBER + r"\s*(?:-|–|to)\s*" + _NUMBER)
_AT_LEAST = re.compile(_NUMBER + r"\s*%?\s*(?:or (?:above|higher|more)|\+|and above)")
_AT_MOST = re.compile(_NUMBER + r"\s*%?\s*(?:or (?:below|less|lower)|and below)")

#: A percentage bucket is a different scale from a 4.0 GPA and the two must not
#: be mixed — "80% or above" and "3.6 or above" can appear in the same list.
_PERCENT = re.compile(r"%|percent", re.I)

_SEASONS = {
    "winter": 1, "spring": 2, "summer": 3, "fall": 4, "autumn": 4,
}
_MONTH_SEASON = {
    1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer", 9: "fall", 10: "fall",
    11: "fall", 12: "winter",
}
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_TERM = re.compile(r"\b(winter|spring|summer|fall|autumn)\b\s*(\d{4})", re.I)
_EARLIER = re.compile(r"\b(earlier|before|prior to)\b", re.I)
_LATER = re.compile(r"\b(later|after)\b", re.I)


def parse_number(text: str) -> Optional[float]:
    """The first number in a string, or None."""
    match = re.search(_NUMBER, text or "")

    return float(match.group(1)) if match else None


def match_numeric_bucket(value: str, options: List[FieldOption]) -> Optional[FieldOption]:
    """The one option whose range contains `value`.

    Args:
        value: A stored answer such as "3.8" or "85%"
        options: The form's options, worded however this company words them

    Returns:
        The containing bucket, or None when nothing contains it or more than
        one does
    """
    number = parse_number(value)

    if number is None:
        return None

    # Stay on one scale. A GPA must not land in "80% or above".
    wants_percent = bool(_PERCENT.search(value)) or number > 5.0

    hits = []

    for option in options:
        label = option.label

        if bool(_PERCENT.search(label)) != wants_percent:
            continue

        span = _RANGE.search(label)
        if span:
            low, high = float(span.group(1)), float(span.group(2))
            if low <= number <= high:
                hits.append(option)
            continue

        at_least = _AT_LEAST.search(label)
        if at_least and number >= float(at_least.group(1)):
            hits.append(option)
            continue

        at_most = _AT_MOST.search(label)
        if at_most and number <= float(at_most.group(1)):
            hits.append(option)

    return hits[0] if len(hits) == 1 else None


def parse_term(text: str) -> Optional[Tuple[int, int]]:
    """(year, season_rank) for a date or academic term, or None.

    Accepts "May 2028", "Spring 2028" and "2028" alike, because applicants write
    all three and forms offer the middle one.
    """
    if not text:
        return None

    term = _TERM.search(text)
    if term:
        return int(term.group(2)), _SEASONS[term.group(1).lower()]

    year = re.search(r"\b(20\d{2})\b", text)
    if not year:
        return None

    month_match = re.search(r"\b([a-z]{3})[a-z]*\b", text.lower())
    if month_match and month_match.group(1) in _MONTHS:
        season = _MONTH_SEASON[_MONTHS[month_match.group(1)]]
        return int(year.group(1)), _SEASONS[season]

    # A bare year sorts before every term within it, so an exact term option
    # still wins and an "earlier/later than" bound still resolves.
    return int(year.group(1)), 0


#: "Spring/Summer 2028", "Fall/Winter 2027" — a single option covering two
#: consecutive terms. Real intern forms use these constantly, and treating them
#: as one term meant a May 2028 graduation matched nothing and fell through to
#: the model: 9 of 43 model-decided fills were this one shape.
_COMPOUND_TERM = re.compile(
    r"\b(winter|spring|summer|fall|autumn)\s*[/&+]\s*(winter|spring|summer|fall|autumn)"
    r"\s*(\d{4})", re.I)


def parse_compound_term(text: str) -> Optional[Tuple[int, List[int]]]:
    """(year, [season ranks]) for an option naming two terms, or None."""
    match = _COMPOUND_TERM.search(text or "")

    if not match:
        return None

    return int(match.group(3)), [
        _SEASONS[match.group(1).lower()], _SEASONS[match.group(2).lower()]
    ]


def match_term_bucket(value: str, options: List[FieldOption]) -> Optional[FieldOption]:
    """The one option covering the term `value` falls in.

    Handles the three shapes real forms use together: an exact term
    ("Spring 2028"), a lower bound ("Earlier than Fall 2027") and an upper bound
    ("Later than Summer 2028").
    """
    target = parse_term(value)

    if target is None:
        return None

    exact = []
    earlier = []
    later = []
    compound = []

    for option in options:
        # Checked first: "Spring/Summer 2028" would otherwise parse as the
        # single term "Summer 2028" and miss a spring graduation.
        pair = parse_compound_term(option.label)
        if pair is not None:
            compound.append((pair, option))
            continue

        parsed = parse_term(option.label)
        if parsed is None:
            continue

        if _EARLIER.search(option.label):
            earlier.append((parsed, option))
        elif _LATER.search(option.label):
            later.append((parsed, option))
        else:
            exact.append((parsed, option))

    for parsed, option in exact:
        if parsed == target:
            return option

    # A compound option matches when the target term is either of its two.
    year, season = target
    covering = [o for (pair_year, seasons), o in compound
                if pair_year == year and season in seasons]
    if len(covering) == 1:
        return covering[0]

    # Before the earliest named term, or after the latest.
    for parsed, option in earlier:
        if target < parsed:
            return option

    for parsed, option in later:
        if target > parsed:
            return option

    return None
