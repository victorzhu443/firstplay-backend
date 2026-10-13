"""
Option matching: the strict rules in `app.autofill.format.match_option`.

Each test names the wrong selection it prevents. The failure mode this file
guards against is a confident wrong option on an application: a value that
is not an answer acquiring a polarity through string comparison.
"""
import pytest

from app.autofill.format import match_option
from app.autofill.schema import FieldKind, FieldOption, FormField


def _select(*labels):
    return FormField(key="q", label="q", kind=FieldKind.SINGLE_SELECT,
                     options=[FieldOption(label=o, value=o) for o in labels])


# --- code audit of 2026-10-12, F2: a blank is not a prefix of "No" ------------

@pytest.mark.parametrize("value", ["None", "none", "N/A", "n/a", "Not applicable", "Unknown", "Not sure"])
def test_a_value_that_is_not_an_answer_never_selects_an_option_by_reverse_prefix(value):
    """"None" starts with "no", so the reverse-prefix pass selected "No" for
    the ADA ability question (28 corpus fields, p18). No polarity, no option."""
    assert match_option(value, _select("Yes", "No")) is None
    assert match_option(value, _select("Yes, without accommodations", "Yes, with accommodations", "No")) is None
    assert match_option(value, _select("Nope", "Yep")) is None
    assert match_option(value, _select("N", "Y")) is None


@pytest.mark.parametrize("value,expected", [
    ("None", "None"),                     # an exact "None" option is the answer
    ("Not applicable", "Not Applicable"),
    ("N/A", "N/A"),
    ("Prefer not to say", "Prefer not to say"),
])
def test_a_blank_still_matches_an_option_that_says_the_same(value, expected):
    assert match_option(value, _select("Yes", "No", expected)).label == expected


def test_the_reverse_prefix_pass_still_serves_pronouns_and_real_answers():
    """What the rule must not cost: "he/him/his" against "He / Him" (13 corpus
    fields), and a stored sentence that opens with the option."""
    assert match_option("he/him/his", _select("He / Him", "She / Her", "They / Them")).label == "He / Him"
    assert match_option("No, I am not", _select("Yes", "No")).label == "No"
    assert match_option("Bachelor's degree in Computer Science", _select("Bachelor's", "Master's")).label == "Bachelor's"
