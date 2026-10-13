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


# --- polarity of Greenhouse's veteran options (code audit of 2026-10-12) ------

GREENHOUSE_VETERAN = _select("I identify as one or more of the classifications of a protected veteran",
                             "I am not a protected veteran", "I don't wish to answer")


def test_i_identify_as_opens_affirmative_and_i_do_not_identify_opens_negative():
    from app.autofill.format import _as_bool
    assert _as_bool("I identify as one or more of the classifications of a protected veteran") is True
    assert _as_bool("I do not identify as a protected veteran") is False
    assert _as_bool("I don't identify as a protected veteran") is False
    assert _as_bool("I don't wish to answer") is None
    assert match_option("Yes", GREENHOUSE_VETERAN).label.startswith("I identify")
    assert match_option("No", GREENHOUSE_VETERAN).label == "I am not a protected veteran"
    assert match_option("I identify as a protected veteran", GREENHOUSE_VETERAN).label.startswith("I identify")
    assert match_option("Prefer not to say", GREENHOUSE_VETERAN).label == "I don't wish to answer"


def test_a_stored_none_on_a_yes_no_question_is_review_not_no():
    """Expected from the F2 rule: "Do you have any deadlines?" with
    timeline_notes "None" was a lucky "No"; now the matcher declines and the
    field is reviewed with the value intact (OPTION_MISMATCH)."""
    from app.autofill.binder import DeterministicBinder, resolve_form
    from app.autofill.classify import classify
    from app.autofill.format import OPTION_MISMATCH
    from app.autofill.memory import Memory
    from app.autofill.schema import FormSchema

    label = "Do you have any deadlines we should be aware of?"
    field = FormField(key="q1", label=label, kind=FieldKind.SINGLE_SELECT,
                      options=[FieldOption(label=o, value=o) for o in ("Yes", "No")],
                      field_class=classify("q1", label, FieldKind.SINGLE_SELECT, option_count=2))
    entry = resolve_form(FormSchema(source="t", ats="greenhouse", posting_id="1", fields=[field]),
                         Memory(preferences={"timeline_notes": "None"}), binder=DeterministicBinder()).entries[0]
    assert entry.value != "No"
    assert entry.needs_review and entry.reason == OPTION_MISMATCH
