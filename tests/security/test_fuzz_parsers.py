"""Fuzz the parsers that read untrusted scraped job-page content.

The browser extension scrapes a live application page (anyone can serve one) and
posts the result to the backend. These parsers run over that fully
attacker-influenceable input and must degrade safely: return, or raise a
declared exception, never crash the worker with an unexpected error type. See
../../../security-agents/fuzzing/ for the method.

Targets:
  * app/autofill/buckets.py  -- pure string parsers over a stored answer / an
    option label (parse_number, parse_month_year, parse_term,
    parse_compound_term).
  * app/autofill/ashby.py       -- parse_ashby_dom over a scraped DOM extract.
  * app/autofill/ashby_api.py   -- parse_ashby_posting over a scraped GraphQL
    posting payload.

FINDING (FIXED 2026-11-25): parse_ashby_dom did not validate node types and
raised an undeclared AttributeError/TypeError (-> HTTP 500) on a malformed DOM
extract, whereas its sibling parse_ashby_posting degrades to MalformedPayload
(a ValueError -> HTTP 400, audit F11). Fixed by routing the DOM nodes through
greenhouse._as_dict/_as_list and str()-coercing control type/name. The last test
now asserts the clean rejection.
"""
import pytest
from hypothesis import strategies as st

from fuzzing.fuzz_lib import assert_robust, text_like

from app.autofill import ashby, ashby_api, buckets
from app.autofill.greenhouse import MalformedPayload


# --- buckets: pure string parsers over a stored answer or an option label ----

def test_parse_number_safe_on_hostile_text():
    assert_robust(buckets.parse_number, allowed=(ValueError, TypeError))()


def test_parse_month_year_safe_on_hostile_text():
    assert_robust(buckets.parse_month_year, allowed=(ValueError, TypeError))()


def test_parse_term_safe_on_hostile_text():
    assert_robust(buckets.parse_term, allowed=(ValueError, TypeError))()


def test_parse_compound_term_safe_on_hostile_text():
    assert_robust(buckets.parse_compound_term, allowed=(ValueError, TypeError))()


# --- ashby DOM: hostile *text* inside a well-formed extract -------------------
# A hostile page controls the scraped strings (labels, option text, control
# types). With the structure well-formed, the parser must degrade safely.

def _dom_with_hostile_text(s: str):
    payload = {
        "posting_id": s,
        "company": s,
        "title": s,
        "apply_url": s,
        "controls": [
            {"tag": s, "type": s, "name": s, "id": s, "required": True, "label": s},
            {"tag": "input", "type": "radio", "name": s, "id": s, "label": s,
             "options": [{"label": s, "value": s}]},
            {"tag": "select", "type": "select-one", "name": s, "label": s,
             "options": [{"label": s, "value": s}, {"label": s, "value": s}]},
        ],
    }
    return ashby.parse_ashby_dom(payload)


_dom_with_hostile_text.__name__ = "parse_ashby_dom"


def test_parse_ashby_dom_safe_on_hostile_text():
    assert_robust(_dom_with_hostile_text, allowed=(ValueError, TypeError))()


# --- ashby API posting: hostile structure AND text ---------------------------
# parse_ashby_posting was hardened (audit F11) to answer malformed nodes with
# MalformedPayload (a ValueError) rather than a 500, so it should survive a
# fuzzed nested structure. Fuzz the whole payload shape, not just one string.

_scalar = st.one_of(text_like, st.none(), st.booleans(), st.integers(),
                    st.floats(allow_nan=True))
_field = st.dictionaries(st.sampled_from(
    ["path", "id", "title", "humanReadablePath", "type", "isDeactivated",
     "selectableValues"]), st.one_of(_scalar, st.lists(_scalar, max_size=3)),
    max_size=6)
_entry = st.dictionaries(st.sampled_from(["field", "isHidden", "isRequired"]),
                         st.one_of(_scalar, _field), max_size=3)
_section = st.dictionaries(st.sampled_from(["isHidden", "fieldEntries"]),
                           st.one_of(_scalar, st.lists(_entry, max_size=3)),
                           max_size=2)
_posting = st.dictionaries(
    st.sampled_from(["id", "title", "workplaceType", "organizationName",
                     "applicationForm"]),
    st.one_of(_scalar, st.dictionaries(st.sampled_from(["sections"]),
              st.one_of(_scalar, st.lists(_section, max_size=3)), max_size=1)),
    max_size=5)


def test_parse_ashby_posting_safe_on_hostile_payload():
    def parse(payload):
        return ashby_api.parse_ashby_posting(payload)

    parse.__name__ = "parse_ashby_posting"
    # MalformedPayload subclasses ValueError; declare the safe set.
    assert_robust(parse, allowed=(ValueError, TypeError, KeyError),
                  strategy=_posting, max_examples=400)()


# --- regression: parse_ashby_dom now shape-checks its nodes (fix verified) ----

def test_parse_ashby_dom_rejects_malformed_structure_cleanly():
    """A malformed DOM structure must degrade to MalformedPayload (a ValueError
    -> HTTP 400), never an undeclared AttributeError/TypeError (-> 500). This is
    the fix for the gap between parse_ashby_dom and its F11-hardened sibling."""
    structurally_malformed = [
        {"controls": "not-a-list"},   # controls is not a list
        "not-a-dict",                 # the whole payload is not a dict
        {"controls": [42]},           # a control node is a scalar, not an object
        {"controls": [{"options": "x"}]},  # a control's options is not a list
    ]
    for payload in structurally_malformed:
        with pytest.raises(MalformedPayload):
            ashby.parse_ashby_dom(payload)


def test_parse_ashby_dom_tolerates_nonstring_scalars():
    """A well-formed control whose fields are the wrong *scalar* type, and a null
    control node, must degrade safely (str()-coerced / treated as empty), never a
    500."""
    # non-string type/name/label scalars
    schema = ashby.parse_ashby_dom({"controls": [{"type": 1, "name": 2, "label": 3}]})
    assert schema.ats == "ashby" and schema.source == "ashby_dom"
    # a null control node is treated as an empty control, not a crash
    assert ashby.parse_ashby_dom({"controls": [None]}).ats == "ashby"
