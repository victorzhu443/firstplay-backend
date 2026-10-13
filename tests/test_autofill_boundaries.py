"""
The public autofill endpoints against a stranger's payloads (audit
2026-10-12, F10 and F11; probes p05b, p06, p14).

  - the rate limiter is bounded in memory, sweeps idle keys, and binds the
    client-chosen install token to the address it came from;
  - a body over the limit is refused from its headers, before it is read;
  - fields, labels, options, answers and observations are capped, with
    caps set from the frozen corpora with headroom, and every refusal is
    a 422 naming a path, never a 500;
  - JSON of the wrong shape is a 400 naming the path, never a 500.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import AUTOFILL_MAX_BODY_BYTES, BodySizeLimitMiddleware, app
from app.rate_limit import (IP_LIMIT_MULTIPLIER, SlidingWindowLimiter, client_ip, client_key,
                            limit_key, rate_limit)
from app.routers.autofill import (MAX_ANSWERS, MAX_FIELDS, MAX_LABEL_CHARS, MAX_OBSERVATIONS,
                                  MAX_OPTIONS_PER_FIELD, MAX_PROFILE_STRING_CHARS)

client = TestClient(app, raise_server_exceptions=False)

PROFILE = {"facts": {"first_name": "Ada"}, "legal_status": {"work_authorization": "Yes"}}


def _gh(questions, **extra):
    return {"ats": "greenhouse", "profile": PROFILE, "use_model": False,
            "form": {"id": 1, "company_name": "Acme", "title": "t", "questions": questions, **extra}}


def _select(i, label, n_options=2):
    return {"label": label, "required": False,
            "fields": [{"name": "question_%d" % i, "type": "multi_value_single_select",
                        "values": [{"label": "Option %d" % k, "value": k} for k in range(n_options)]}]}


# --- F10: the limiter -----------------------------------------------------------

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_the_limiter_is_bounded_under_a_flood_of_fresh_keys():
    """Probe p06 retained 50,000 keys for 50,000 tokens; the map is now
    capped, least recently seen dropped first."""
    limiter = SlidingWindowLimiter(600, 3600, clock=_Clock(), max_keys=1000)
    for i in range(50_000):
        limiter.check("install:%08d" % i)
    assert limiter.tracked_keys == 1000


def test_idle_keys_are_swept_once_a_window_has_passed():
    """The old `if not hits: del` sat after `append` and could never run."""
    clock = _Clock()
    limiter = SlidingWindowLimiter(5, 60, clock=clock)
    for key in ("a", "b", "c"):
        limiter.check(key)
    assert limiter.tracked_keys == 3

    clock.now += 61
    limiter.check("d")                       # the sweep runs on the next check

    assert limiter.tracked_keys == 1         # a, b, c had aged out entirely


def test_a_key_still_inside_the_window_survives_the_sweep():
    clock = _Clock()
    limiter = SlidingWindowLimiter(5, 60, clock=clock)
    limiter.check("old")
    clock.now += 40
    limiter.check("recent")
    clock.now += 25                          # old's hit is 65 s ago, recent's 25 s
    limiter.check("new")
    assert limiter.tracked_keys == 2


def _request(headers=None, host="1.2.3.4"):
    from starlette.datastructures import Headers

    class _Req:
        def __init__(self):
            self.headers = Headers(raw=[(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()])
            self.client = type("C", (), {"host": host})()

    return _Req()


def test_the_limit_key_binds_the_install_token_to_the_address():
    a = limit_key(_request({"X-FirstPlay-Install": "tok-1"}, host="10.0.0.1"))
    b = limit_key(_request({"X-FirstPlay-Install": "tok-1"}, host="10.0.0.2"))
    c = limit_key(_request({}, host="10.0.0.1"))
    assert a == "install:tok-1@10.0.0.1" and a != b and c == "ip:10.0.0.1"
    # client_key (the model-status record) keeps its old shape.
    assert client_key(_request({"X-FirstPlay-Install": "tok-1"})) == "install:tok-1"
    assert client_ip(_request({"X-Forwarded-For": "9.9.9.9, 203.0.113.7"})) == "203.0.113.7"


def test_rotating_the_install_token_is_capped_by_the_address():
    """Each fresh token used to buy a fresh allowance. The address now has
    its own cap of IP_LIMIT_MULTIPLIER times the per-install one."""
    dependency = rate_limit(2, 60, "test endpoint")
    allowed = 2 * IP_LIMIT_MULTIPLIER
    statuses = []
    for i in range(allowed + 1):
        try:
            dependency(_request({"X-FirstPlay-Install": "fresh-%d" % i}, host="10.0.0.9"))
            statuses.append(200)
        except Exception as e:  # noqa: BLE001
            statuses.append(e.status_code)
    assert statuses[:allowed] == [200] * allowed
    assert statuses[-1] == 429
    # Another address is unaffected.
    dependency(_request({"X-FirstPlay-Install": "fresh-0"}, host="10.0.0.10"))


def test_installs_sharing_an_address_keep_separate_allowances_below_the_address_cap():
    dependency = rate_limit(2, 60, "test endpoint")
    for token in ("a", "b", "c"):
        dependency(_request({"X-FirstPlay-Install": token}, host="10.0.0.11"))
        dependency(_request({"X-FirstPlay-Install": token}, host="10.0.0.11"))
    with pytest.raises(Exception) as excinfo:
        dependency(_request({"X-FirstPlay-Install": "a"}, host="10.0.0.11"))
    assert excinfo.value.status_code == 429
    assert "network address" not in excinfo.value.detail


# --- F10: body size ---------------------------------------------------------------

def test_a_body_over_the_limit_is_refused_with_413():
    big = _gh([_select(0, "x" * 500) for _ in range(10)])
    big["form"]["content"] = "p" * (AUTOFILL_MAX_BODY_BYTES + 1)
    response = client.post("/api/autofill/plan", json=big)
    assert response.status_code == 413
    assert "exceeds the limit" in response.json()["detail"]
    assert response.headers.get("x-request-id")


def test_a_corpus_sized_body_is_accepted():
    """The largest frozen corpus form is 172 KB of board-API JSON."""
    body = _gh([_select(i, "Question %d" % i) for i in range(50)])
    body["form"]["content"] = "p" * (180 * 1024)
    assert len(json.dumps(body)) > 180 * 1024
    assert client.post("/api/autofill/plan", json=body).status_code == 200


def test_a_declared_length_over_the_limit_is_refused_before_the_body_is_read():
    response = client.post("/api/autofill/learn", data=b"{}",
                           headers={"Content-Type": "application/json",
                                    "Content-Length": str(AUTOFILL_MAX_BODY_BYTES + 1)})
    assert response.status_code == 413


@pytest.mark.anyio
async def test_an_undeclared_body_on_a_limited_path_is_411():
    calls = []

    async def downstream(scope, receive, send):
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    middleware = BodySizeLimitMiddleware(downstream, limit=10)
    await middleware({"type": "http", "method": "POST", "path": "/api/autofill/plan",
                      "headers": [(b"transfer-encoding", b"chunked")]}, receive, send)
    assert sent[0]["status"] == 411 and calls == []

    sent.clear()
    await middleware({"type": "http", "method": "GET", "path": "/api/autofill/health",
                      "headers": []}, receive, send)
    assert sent[0]["status"] == 200 and calls == ["/api/autofill/health"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- F10: caps ------------------------------------------------------------------------

def test_a_form_with_too_many_fields_is_a_422_naming_the_form():
    response = client.post("/api/autofill/plan", json=_gh([_select(i, "Q%d" % i) for i in range(MAX_FIELDS)]))
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "form"]
    assert "exceeds the limit of {}".format(MAX_FIELDS) in response.json()["detail"][0]["msg"]


def test_the_largest_corpus_form_is_well_inside_the_field_cap():
    """59 fields is the corpus maximum (1,269 Greenhouse postings)."""
    response = client.post("/api/autofill/plan", json=_gh([_select(i, "Q%d" % i) for i in range(120)]))
    assert response.status_code == 200
    assert response.json()["summary"]["total"] >= 120


def test_a_long_label_is_truncated_before_classification_not_refused():
    """Probe p05b: 50 KB of label cost 9.6 s in the theme regexes."""
    label = ("Are you able to work in person and do you sponsor " * 2000) + " authorized to work"
    response = client.post("/api/autofill/plan", json=_gh([_select(0, label)]))
    assert response.status_code == 200
    entry = next(e for e in response.json()["plan"]["entries"] if e["field_key"] == "question_0")
    assert len(entry["label"]) <= MAX_LABEL_CHARS
    assert entry["label"] == label[:MAX_LABEL_CHARS].strip()


def test_a_field_with_too_many_options_is_a_422_naming_the_field():
    response = client.post("/api/autofill/plan", json=_gh([_select(0, "Which college?", MAX_OPTIONS_PER_FIELD + 1)]))
    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["loc"] == ["body", "form", "question_0", "options"]


def test_a_university_list_of_corpus_size_is_accepted():
    """2,464 options is the corpus maximum (a 'select your college' list on
    three postings); the audit's proposed cap of 300 would have refused 44
    real fields."""
    response = client.post("/api/autofill/plan", json=_gh([_select(0, "Which college did you attend?", 2464)]))
    assert response.status_code == 200


def test_too_many_answers_in_the_profile_is_a_422_naming_the_path():
    body = _gh([])
    body["profile"] = {"answers": {str(i): "v" for i in range(MAX_ANSWERS + 1)}}
    response = client.post("/api/autofill/plan", json=body)
    assert response.status_code == 422
    row = response.json()["detail"][0]
    assert row["loc"] == ["body", "profile"] and "answers" in row["msg"] and "limit of {}".format(MAX_ANSWERS) in row["msg"]


def test_an_oversized_profile_value_is_refused_by_path_not_truncated():
    body = _gh([])
    body["profile"] = {"facts": {"first_name": "Ada"}, "learned": {"essay": "x" * (MAX_PROFILE_STRING_CHARS + 1)}}
    response = client.post("/api/autofill/plan", json=body)
    assert response.status_code == 422
    row = response.json()["detail"][0]
    assert "learned.essay" in row["msg"] and "x" * 50 not in response.text


def test_learn_truncates_a_huge_label_and_refuses_a_huge_user_value():
    """Probe p14's last case: a 200 KB label and a 50,000-item value took
    ~90 s and returned 200."""
    response = client.post("/api/autofill/learn", json={
        "profile": {}, "observations": [{"label": "x" * 200_000, "user_value": ["y"] * 50_000}]})
    assert response.status_code == 422
    row = response.json()["detail"][0]
    assert "observations[0].user_value" in row["msg"] and row["loc"] == ["body", "observations"]

    response = client.post("/api/autofill/learn", json={
        "profile": {}, "observations": [{"label": "x" * 200_000, "user_value": "M"}]})
    assert response.status_code == 200
    assert all(len(k) <= MAX_LABEL_CHARS + 20 for k in response.json()["profile"].get("answers", {}))


def test_too_many_observations_is_a_422():
    response = client.post("/api/autofill/learn", json={
        "profile": {}, "observations": [{"label": "q%d" % i, "user_value": "v"} for i in range(MAX_OBSERVATIONS + 1)]})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "observations"]


# --- F11: malformed-but-JSON payloads -----------------------------------------------

MALFORMED = {
    "greenhouse questions is a string": ({"ats": "greenhouse", "form": {"id": 1, "questions": "oops"}}, "form.questions"),
    "greenhouse question.fields is a string": ({"ats": "greenhouse", "form": {"id": 1, "questions": [{"label": "x", "fields": "oops"}]}}, "form.questions[0].fields"),
    "greenhouse field.values items are strings": ({"ats": "greenhouse", "form": {"id": 1, "questions": [{"label": "x", "fields": [{"name": "q", "type": "multi_value_single_select", "values": ["a", "b"]}]}]}}, "form.questions[0].fields[0].values[0]"),
    "greenhouse compliance block is a dict": ({"ats": "greenhouse", "form": {"id": 1, "compliance": {"questions": []}}}, "form.compliance"),
    "greenhouse location is a string": ({"ats": "greenhouse", "form": {"id": 1, "location": "Ithaca"}}, "form.location"),
    "greenhouse offices items are strings": ({"ats": "greenhouse", "form": {"id": 1, "offices": ["Ithaca"]}}, "form.offices[0]"),
    "greenhouse question is a string": ({"ats": "greenhouse", "form": {"id": 1, "questions": ["x"]}}, "form.questions[0]"),
    "greenhouse demographic block is a list": ({"ats": "greenhouse", "form": {"id": 1, "demographic_questions": []}}, "form.demographic_questions"),
    "greenhouse location_questions is a dict": ({"ats": "greenhouse", "form": {"id": 1, "location_questions": {}}}, "form.location_questions"),
    "ashby fieldEntries.field is a string": ({"ats": "ashby", "form": {"applicationForm": {"sections": [{"fieldEntries": [{"field": "x"}]}]}}}, "form.applicationForm.sections[0].fieldEntries[0].field"),
    "ashby selectableValues items are strings": ({"ats": "ashby", "form": {"applicationForm": {"sections": [{"fieldEntries": [{"field": {"path": "p", "title": "t", "type": "ValueSelect", "selectableValues": ["a"]}}]}]}}}, "form.applicationForm.sections[0].fieldEntries[0].field.selectableValues[]"),
    "ashby sections is a string": ({"ats": "ashby", "form": {"applicationForm": {"sections": "oops"}}}, "form.applicationForm.sections"),
    "ashby DOM controls is a string": ({"ats": "ashby", "form": {"controls": "oops"}}, "form.controls"),
}


@pytest.mark.parametrize("name", list(MALFORMED))
def test_malformed_payloads_are_400s_naming_the_path(name):
    body, path = MALFORMED[name]
    response = client.post("/api/autofill/plan", json={**body, "profile": {}})
    assert response.status_code == 400, (name, response.status_code, response.text[:200])
    assert path in response.json()["detail"], (name, response.json()["detail"])


def test_well_formed_payloads_still_parse_after_the_guards():
    """The fixtures the extension really sends."""
    import os
    fixtures = os.path.join(os.path.dirname(__file__), "fixtures", "forms")
    for name in sorted(os.listdir(fixtures)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(fixtures, name)) as handle:
            form = json.load(handle)
        ats = "ashby" if name.startswith("ashby") else "greenhouse"
        response = client.post("/api/autofill/plan", json={"ats": ats, "form": form, "profile": PROFILE, "use_model": False})
        assert response.status_code == 200, (name, response.text[:200])
