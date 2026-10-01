"""
JevBinder tests: batching, the cache, and the accounting the live measurement
depends on.

The cache assertions carry the most weight. Measured over 150 postings the same
117 labels recur 8.5 times each, so if the cache is wrong the cost model is
wrong by an order of magnitude — and the cost model is the argument for using a
hosted model at all.
"""
import json
import os

import pytest

from app.autofill.jev_binder import JevBinder, ThemeCache
from app.autofill.themes import QuestionTheme
from app.jev_client import Answer, Decision


class FakeClient:
    """Records calls and returns whatever themes the test asks for."""

    def __init__(self, themes, confidence=0.95, cost=0.00002):
        self._themes = themes
        self._confidence = confidence
        self._cost = cost
        self.calls = []

    def decide(self, state, questions, description=""):
        self.calls.append({"state": state, "questions": questions})

        answers = {}
        for question_id, question in questions.items():
            label = question["instructions"]
            theme = QuestionTheme.UNKNOWN.value
            for needle, value in self._themes.items():
                if needle in label:
                    theme = value.value
                    break
            answers[question_id] = Answer(
                type="choice", choice=theme, confidence=self._confidence
            )

        return Decision(answers=answers, cost_usd=self._cost, input_tokens=100)


def _binder(themes=None, **kw):
    themes = themes if themes is not None else {
        "sponsorship": QuestionTheme.VISA_SPONSORSHIP,
        "hear about": QuestionTheme.HEARD_ABOUT,
        "relocat": QuestionTheme.WILLING_TO_RELOCATE,
    }
    client = FakeClient(themes)
    return JevBinder(client=client, **kw), client


# --- batching ---------------------------------------------------------------

def test_many_labels_cost_one_call():
    """State is sent once, so a tenth question costs tokens but almost no time.
    One call per field would multiply latency by the field count."""
    binder, client = _binder()

    binder.classify_themes([
        "Do you require visa sponsorship?",
        "How did you hear about this job?",
        "Are you open to relocation for this role?",
    ])

    assert len(client.calls) == 1
    assert len(client.calls[0]["questions"]) == 3
    assert binder.calls == 1
    assert binder.questions_asked == 3


def test_duplicate_labels_are_asked_once():
    binder, client = _binder()

    result = binder.classify_themes(["Do you require visa sponsorship?"] * 4)

    assert len(client.calls[0]["questions"]) == 1
    assert result["Do you require visa sponsorship?"][0] == QuestionTheme.VISA_SPONSORSHIP


def test_a_blank_label_is_never_sent():
    binder, client = _binder()

    result = binder.classify_themes(["", "   ", None])

    assert client.calls == []
    assert all(v[0] == QuestionTheme.UNKNOWN for v in result.values())


def test_batches_are_split_at_the_question_cap():
    from app.autofill.jev_binder import MAX_QUESTIONS_PER_CALL

    binder, client = _binder(themes={})
    labels = ["question number {}".format(i) for i in range(MAX_QUESTIONS_PER_CALL + 5)]

    binder.classify_themes(labels)

    assert len(client.calls) == 2
    assert len(client.calls[0]["questions"]) == MAX_QUESTIONS_PER_CALL


def test_state_carries_the_form_not_the_whole_form_content():
    """Accuracy falls as state fills with material the question does not need,
    and each question already carries its own label."""
    binder, client = _binder(form_context={"company": "Figma"})

    binder.classify_themes(["Do you require visa sponsorship?"])

    assert client.calls[0]["state"] == {"form": {"company": "Figma"}}


# --- the cache --------------------------------------------------------------

def test_a_cached_label_costs_nothing():
    """147 recurrences for the most common label. Without the cache that is 147
    calls; with it, one."""
    binder, client = _binder()
    label = "Do you require visa sponsorship?"

    binder.classify_themes([label])
    assert len(client.calls) == 1

    binder.classify_themes([label])
    assert len(client.calls) == 1
    assert binder.cache.hits == 1


def test_a_fully_cached_form_needs_no_client_at_all():
    """So a warm cache works without credentials, and the client is built lazily
    rather than at construction."""
    cache = ThemeCache()
    cache.put("Do you require visa sponsorship?", QuestionTheme.VISA_SPONSORSHIP, 0.95)

    binder = JevBinder(client=None, cache=cache)
    result = binder.classify_themes(["Do you require visa sponsorship?"])

    assert result["Do you require visa sponsorship?"][0] == QuestionTheme.VISA_SPONSORSHIP
    assert binder.calls == 0


def test_the_cache_keys_on_the_normalised_label():
    """"LinkedIn Profile:" and "LinkedIn Profile" are the same question."""
    cache = ThemeCache()
    cache.put("How did you hear about this job?", QuestionTheme.HEARD_ABOUT, 0.9)

    assert cache.get("how did you hear about this job") is not None
    assert cache.get("How did you hear about this job?:") is not None


def test_confidence_is_stored_rather_than_collapsed_to_certain():
    """So that raising the threshold later tightens historical entries too,
    instead of grandfathering them in."""
    binder, _client = _binder()
    binder._client._confidence = 0.55

    theme, confidence = binder.classify_themes(["Do you require visa sponsorship?"])[
        "Do you require visa sponsorship?"
    ]

    assert confidence == pytest.approx(0.55)
    assert binder.cache.get("Do you require visa sponsorship?")[1] == pytest.approx(0.55)


def test_the_cache_round_trips_through_disk(tmp_path):
    path = str(tmp_path / "themes.json")
    cache = ThemeCache(path)
    cache.put("Do you require visa sponsorship?", QuestionTheme.VISA_SPONSORSHIP, 0.91)
    cache.save()

    reloaded = ThemeCache(path)

    assert reloaded.size == 1
    theme, confidence = reloaded.get("Do you require visa sponsorship?")
    assert theme == QuestionTheme.VISA_SPONSORSHIP
    assert confidence == pytest.approx(0.91)


def test_a_corrupt_cache_starts_empty_rather_than_failing(tmp_path):
    """Derived data. A bad file rebuilds itself; it must not stop a run."""
    path = str(tmp_path / "themes.json")
    with open(path, "w") as handle:
        handle.write("{not json")

    assert ThemeCache(path).size == 0


def test_a_retired_theme_name_is_treated_as_a_miss(tmp_path):
    """A renamed theme must not resurrect stale classifications."""
    path = str(tmp_path / "themes.json")
    with open(path, "w") as handle:
        json.dump({"some label": ["theme_that_no_longer_exists", 0.9]}, handle)

    assert ThemeCache(path).get("some label") is None


def test_hit_rate_is_none_before_any_lookup():
    assert ThemeCache().hit_rate() is None


# --- robustness --------------------------------------------------------------

def test_an_off_menu_theme_becomes_unknown():
    """Structurally this should be impossible, since answers are constrained to
    the criteria supplied — so it is handled rather than trusted."""
    binder, _client = _binder(themes={"sponsorship": QuestionTheme.VISA_SPONSORSHIP})
    binder._client._themes = {}

    class OffMenu(FakeClient):
        def decide(self, state, questions, description=""):
            return Decision(
                answers={q: Answer(type="choice", choice="not_a_theme", confidence=0.99)
                         for q in questions},
                cost_usd=0.0,
            )

    binder._client = OffMenu({})
    theme, confidence = binder.classify_themes(["anything"])["anything"]

    assert theme == QuestionTheme.UNKNOWN
    assert confidence == 0.0


def test_cost_and_calls_are_accounted_for_measurement():
    binder, _client = _binder()

    binder.classify_themes(["Do you require visa sponsorship?",
                            "How did you hear about this job?"])

    assert binder.calls == 1
    assert binder.cost_usd == pytest.approx(0.00002)


def test_every_theme_has_criteria():
    """A Choice option with no situation description comes back at low
    confidence, so a theme added to the enum without criteria is a silent
    accuracy regression."""
    from app.autofill.themes import THEME_CRITERIA

    for theme in QuestionTheme:
        assert theme.value in THEME_CRITERIA, theme
        assert len(THEME_CRITERIA[theme.value]) > 20, theme


def test_gates_skip_fields_with_more_options_than_a_choice_allows():
    """Rothesay (corpus-large) lists 552 universities; Jev's Choice takes 255."""
    from app.autofill.jev_binder import JevBinder
    from app.autofill.schema import FieldClass, FieldKind, FieldOption, FormField

    field = FormField(key="uni", label="Which university?", kind=FieldKind.SINGLE_SELECT,
                      field_class=FieldClass.SCREENING, required=True,
                      options=[FieldOption(label=f"University {i}", value=str(i)) for i in range(552)])
    binder = JevBinder(client=None)

    assert binder.answer_from_profile({"applicant": {}}, [("a", field)]) == {"a": (None, 0.0)}
    assert binder.match_options([("m", "Cornell University", field)]) == {"m": (None, 0.0)}


class NoulAwareClient(FakeClient):
    """Answers a Noul by the option named in its instructions, a Choice as FakeClient does."""

    def __init__(self, beliefs, **kw):
        super().__init__({}, **kw)
        self._beliefs = beliefs

    def decide(self, state, questions, description=""):
        self.calls.append({"state": state, "questions": questions})
        answers = {}
        for question_id, question in questions.items():
            if question["type"] == "noul":
                text = question["instructions"]
                p = next((v for needle, v in self._beliefs.items() if needle in text), 0.5)
                answers[question_id] = Answer(type="noul", noul=p)
            else:
                answers[question_id] = Answer(type="choice", choice="unsure", confidence=0.3)
        return Decision(answers=answers, cost_usd=self._cost, input_tokens=100)


def _multi(label, *options):
    from app.autofill.schema import FieldClass, FieldKind, FieldOption, FormField
    return FormField(key=label[:8], label=label, kind=FieldKind.MULTI_SELECT, field_class=FieldClass.SCREENING,
                     required=True, options=[FieldOption(label=o, value=o) for o in options])


def test_a_multi_select_is_one_noul_per_option_and_returns_the_decided_set():
    from app.autofill.jev_binder import JevBinder

    client = NoulAwareClient({"Summer 2027": 0.97, "Spring 2027": 0.03, "Fall 2026": 0.04})
    binder = JevBinder(client=client)
    field = _multi("When are you available? Check all that apply", "Fall 2026", "Spring 2027", "Summer 2027")

    out = binder.answer_from_profile({"applicant": {"notes": ["Summer only"]}}, [("a", field)])

    assert len(client.calls) == 1
    asked = client.calls[0]["questions"]
    assert len(asked) == 3 and all(q["type"] == "noul" for q in asked.values())
    assert all("Option being judged" in q["instructions"] for q in asked.values())
    labels, confidence = out["a"]
    assert labels == ["Summer 2027"] and confidence >= 0.96


def test_an_undecided_option_lowers_the_whole_field_and_an_empty_set_is_no_answer():
    from app.autofill.jev_binder import JevBinder

    client = NoulAwareClient({"Chicago": 0.95, "New York": 0.55, "Amsterdam": 0.02})
    binder = JevBinder(client=client)
    field = _multi("Which offices are you open to?", "Chicago", "New York", "Amsterdam")
    labels, confidence = binder.answer_from_profile({"applicant": {}}, [("a", field)])["a"]
    assert labels == ["Chicago"] and abs(confidence - 0.55) < 1e-9   # New York undecided → suggestion only

    nothing = NoulAwareClient({"Chicago": 0.05, "New York": 0.02, "Amsterdam": 0.02})
    labels, confidence = JevBinder(client=nothing).answer_from_profile({"applicant": {}}, [("b", field)])["b"]
    assert labels is None and confidence == 0.0


def test_multi_select_batches_are_split_by_question_count_not_request_count():
    from app.autofill.jev_binder import JevBinder, MAX_QUESTIONS_PER_CALL

    client = NoulAwareClient({})
    binder = JevBinder(client=client)
    # Each request is 12 nouls; four of them exceed one 40-question call.
    requests = [("r{}".format(i), _multi("Q{} check all".format(i), *["o{}".format(k) for k in range(12)]))
                for i in range(4)]
    binder.answer_from_profile({"applicant": {}}, requests)

    assert len(client.calls) == 2
    assert all(len(c["questions"]) <= MAX_QUESTIONS_PER_CALL for c in client.calls)
    assert sum(len(c["questions"]) for c in client.calls) == 48


def test_a_lists_none_option_is_chosen_by_elimination_when_every_level_is_ruled_out():
    """Rocket Lab 7990043003: clearance levels all at p<=0.05, "Not Applicable" at 0.15."""
    from app.autofill.jev_binder import JevBinder

    client = NoulAwareClient({"Top Secret": 0.03, "Secret": 0.03, "Expired": 0.05, "Not Applicable": 0.15})
    field = _multi("Active Security Clearance(s)", "Top Secret", "Secret", "Expired Clearance", "Not Applicable")
    labels, confidence = JevBinder(client=client).answer_from_profile({"applicant": {}}, [("a", field)])["a"]
    assert labels == ["Not Applicable"] and abs(confidence - 0.95) < 1e-9

    # One level merely undecided: no elimination, the applicant decides.
    unsure = NoulAwareClient({"Top Secret": 0.03, "Secret": 0.40, "Expired": 0.05, "Not Applicable": 0.15})
    labels, confidence = JevBinder(client=unsure).answer_from_profile({"applicant": {}}, [("b", field)])["b"]
    assert labels is None and confidence == 0.0

    # Two none-like options (Never held / Do not wish to disclose) are ambiguous: no pick.
    two = _multi("Active Security Clearance(s)", "Top Secret", "Never held a clearance", "None of the above")
    labels, _ = JevBinder(client=NoulAwareClient({"Top Secret": 0.02})).answer_from_profile({"applicant": {}}, [("c", two)])["c"]
    assert labels is None
