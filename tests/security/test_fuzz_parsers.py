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
import warnings

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


# --- registry additions (2026-11-26) -------------------------------------------
# The parsers above were fuzzed with hostile strings in a well-formed structure,
# which cannot find a node of the wrong *type* (an option given as a bare string,
# a company name given as a number). Each adapter now also gets a structural
# strategy, and the two parsers that were missing from this file entirely —
# the Greenhouse adapter (the primary ATS) and the HTML text extractor on the
# fetched job page — are registered. The allowed set for the structural runs is
# MalformedPayload alone: anything else that escapes is a 500.

from app.autofill import greenhouse
from app.routers import job as job_router

_option_node = st.one_of(
    _scalar,
    st.dictionaries(st.sampled_from(["label", "value", "id", "text"]),
                    _scalar, max_size=3),
)
_dom_control = st.dictionaries(
    st.sampled_from(["tag", "type", "name", "id", "required", "label", "options"]),
    st.one_of(_scalar, st.lists(_option_node, max_size=3)),
    max_size=7,
)
_dom_payload = st.one_of(
    _scalar,
    st.dictionaries(
        st.sampled_from(["posting_id", "company", "title", "apply_url", "controls"]),
        st.one_of(_scalar, st.lists(st.one_of(_scalar, _dom_control), max_size=4)),
        max_size=5,
    ),
)


def test_parse_ashby_dom_safe_on_hostile_structure():
    def parse(payload):
        return ashby.parse_ashby_dom(payload)

    parse.__name__ = "parse_ashby_dom[structure]"
    assert_robust(parse, allowed=(MalformedPayload,), strategy=_dom_payload,
                  max_examples=400)()


_gh_field = st.dictionaries(
    st.sampled_from(["name", "type", "values", "required"]),
    st.one_of(_scalar, st.lists(_option_node, max_size=3)),
    max_size=4,
)
_gh_question = st.one_of(
    _scalar,
    st.dictionaries(
        st.sampled_from(["label", "required", "description", "fields", "id", "type",
                         "answer_options"]),
        st.one_of(_scalar, st.lists(st.one_of(_scalar, _gh_field), max_size=3)),
        max_size=5,
    ),
)
_gh_block = st.one_of(
    _scalar,
    st.dictionaries(
        st.sampled_from(["type", "questions", "description", "requires_processing_consent",
                         "requires_consent", "demographic_data_consent_applies", "header"]),
        st.one_of(_scalar, st.lists(_gh_question, max_size=3)),
        max_size=4,
    ),
)
_gh_payload = st.one_of(
    _scalar,
    st.dictionaries(
        st.sampled_from(["id", "title", "company_name", "absolute_url", "location", "offices",
                         "questions", "location_questions", "compliance", "data_compliance",
                         "demographic_questions", "education", "education_options"]),
        st.one_of(
            _scalar,
            st.lists(st.one_of(_scalar, _gh_question, _gh_block), max_size=3),
            st.dictionaries(st.sampled_from(["name", "questions", "degrees", "disciplines", "schools"]),
                            st.one_of(_scalar, st.lists(st.one_of(_scalar, _gh_question), max_size=3)),
                            max_size=3),
        ),
        max_size=8,
    ),
)


def test_parse_greenhouse_job_safe_on_hostile_structure():
    def parse(payload):
        return greenhouse.parse_greenhouse_job(payload, board="fuzz")

    parse.__name__ = "parse_greenhouse_job[structure]"
    assert_robust(parse, allowed=(MalformedPayload,), strategy=_gh_payload,
                  max_examples=400)()


def test_parse_greenhouse_job_safe_on_hostile_text():
    def parse(s):
        payload = {
            "id": s, "title": s, "company_name": s, "absolute_url": s,
            "location": {"name": s}, "offices": [{"name": s}],
            "questions": [{"label": s, "required": True, "fields": [
                {"name": s, "type": "input_text"},
                {"name": s, "type": "multi_value_single_select",
                 "values": [{"label": s, "value": s}]},
            ]}],
            "compliance": [{"type": s, "questions": [{"label": s, "fields": [
                {"name": s, "type": "multi_value_single_select", "values": [{"label": s, "value": 1}]}]}]}],
            "data_compliance": [{"type": "gdpr", "requires_processing_consent": True}],
        }
        return greenhouse.parse_greenhouse_job(payload, board=s)

    parse.__name__ = "parse_greenhouse_job[text]"
    assert_robust(parse, allowed=(MalformedPayload,))()


def test_extract_job_text_safe_on_hostile_html():
    """BeautifulSoup over whatever the fetched page returned."""
    def extract(html):
        # bs4 warns when a short input looks like a filename or URL; that is
        # the fuzzer's doing, not a finding.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return job_router.extract_job_text(html)

    extract.__name__ = "extract_job_text"
    assert_robust(extract, allowed=(), max_examples=300)()


def test_ashby_dom_option_given_as_a_bare_string_is_a_400_not_a_500():
    """The case the structural strategy was added for: an option node that is
    not an object used to reach `.get` and raise AttributeError."""
    with pytest.raises(MalformedPayload):
        ashby.parse_ashby_dom({"controls": [
            {"tag": "select", "type": "select-one", "name": "x", "options": ["a"]}]})


def test_greenhouse_non_string_company_name_degrades():
    schema = greenhouse.parse_greenhouse_job(
        {"company_name": 1, "data_compliance": [{"type": "gdpr", "requires_processing_consent": True}]},
        board="b")
    assert schema.company == "1"
