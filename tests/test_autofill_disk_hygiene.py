"""
Files derived from the profile are owner-only and carry no value in a key.

The profile is 0600; `labels.json`, `options.json`, `themes.json` and the
coverage/audit dumps were written with the default umask and, for the
first two, keyed on the applicant's values in clear (audit 2026-10-12, F5).
Everything here runs against a temporary directory, never the real
`~/.config/firstplay`.
"""
import hashlib
import json
import os

import pytest

from app.autofill.evaluate import label_key, load_labels, save_labels
from app.autofill.private_json import private_file, write_private_json


def _mode(path):
    return oct(os.stat(str(path)).st_mode & 0o777)


# --- the helper ---------------------------------------------------------------

def test_write_private_json_creates_an_owner_only_file(tmp_path):
    path = tmp_path / "nested" / "out.json"

    returned = write_private_json(str(path), {"a": 1}, indent=1)

    assert returned == str(path)
    assert json.loads(path.read_text()) == {"a": 1}
    assert _mode(path) == "0o600"
    assert not os.path.exists(str(path) + ".tmp")


def test_write_private_json_tightens_an_existing_world_readable_file(tmp_path):
    path = tmp_path / "labels.json"
    path.write_text("{}")
    os.chmod(str(path), 0o644)

    write_private_json(str(path), {"k": "v"})

    assert _mode(path) == "0o600"
    assert json.loads(path.read_text()) == {"k": "v"}


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path):
    path = tmp_path / "out.json"
    path.write_text('{"old": true}')

    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        write_private_json(str(path), {"x": Unserialisable()})

    assert json.loads(path.read_text()) == {"old": True}
    assert not os.path.exists(str(path) + ".tmp")


def test_private_file_writes_lines_owner_only(tmp_path):
    path = tmp_path / "audit" / "model_decisions.jsonl"

    with private_file(str(path)) as handle:
        handle.write('{"a": 1}\n')
        handle.write('{"a": 2}\n')

    assert path.read_text().splitlines() == ['{"a": 1}', '{"a": 2}']
    assert _mode(path) == "0o600"


# --- labels.json --------------------------------------------------------------

class _Entry:
    def __init__(self, label, value, reason):
        self.label, self.value, self.reason = label, value, reason


def test_label_key_hashes_the_value_and_keeps_label_and_reason():
    key = label_key("123", "email", _Entry("Email", "ada@example.com", "facts.email"))

    label, digest, reason = key.split("||")
    assert label == "email" and reason == "facts.email"
    assert "ada@example.com" not in key
    assert digest == hashlib.sha256(b"ada@example.com").hexdigest()[:16]


def test_label_key_without_an_entry_is_the_per_instance_key():
    assert label_key("123", "email") == "123::email"


def test_save_labels_is_owner_only(tmp_path):
    path = tmp_path / "labels.json"

    save_labels({"a||b||c": "correct"}, str(path))

    assert _mode(path) == "0o600"


def test_load_labels_migrates_legacy_plaintext_keys_and_rewrites_the_file(tmp_path):
    """A legacy key holds `str(value)[:50]` in clear — an email, a veteran
    status. On load it is converted to the hashed form (exactly, since the
    digest is over that same segment) and the file is rewritten without it."""
    path = tmp_path / "labels.json"
    legacy = "email||ada@example.com||facts.email"
    hashed = label_key("1", "email", _Entry("Email", "ada@example.com", "facts.email"))
    path.write_text(json.dumps({legacy: "correct", "9::q": "wrong", hashed + "x": "correct"}))
    os.chmod(str(path), 0o644)

    labels = load_labels(str(path))

    assert labels[hashed] == "correct"
    assert legacy not in labels
    assert labels["9::q"] == "wrong"                      # per-instance keys untouched
    on_disk = json.loads(path.read_text())
    assert legacy not in on_disk and hashed in on_disk
    assert "ada@example.com" not in path.read_text()
    assert _mode(path) == "0o600"


def test_load_labels_leaves_an_already_hashed_file_alone(tmp_path):
    path = tmp_path / "labels.json"
    hashed = label_key("1", "email", _Entry("Email", "ada@example.com", "facts.email"))
    path.write_text(json.dumps({hashed: "correct"}))
    before = os.stat(str(path)).st_mtime_ns

    assert load_labels(str(path)) == {hashed: "correct"}
    assert os.stat(str(path)).st_mtime_ns == before


# --- the caches ---------------------------------------------------------------

def test_theme_cache_save_is_owner_only(tmp_path):
    from app.autofill.jev_binder import ThemeCache
    from app.autofill.themes import QuestionTheme

    cache = ThemeCache(str(tmp_path / "themes.json"))
    cache.put("First Name", QuestionTheme.UNKNOWN, 0.5)
    cache.save()

    assert _mode(tmp_path / "themes.json") == "0o600"


def test_option_cache_save_is_owner_only(tmp_path):
    from app.autofill.jev_binder import OptionCache, cache_signature

    cache = OptionCache(str(tmp_path / "options.json"))
    cache.put(cache_signature("optmatch", "x"), "Yes", 0.9)
    cache.save()

    assert _mode(tmp_path / "options.json") == "0o600"


def test_option_cache_without_plaintext_keys_is_not_rewritten_on_load(tmp_path):
    from app.autofill.jev_binder import OptionCache, cache_signature

    path = tmp_path / "options.json"
    path.write_text(json.dumps({cache_signature("optmatch", "x"): ["Yes", 0.9]}))
    before = os.stat(str(path)).st_mtime_ns

    OptionCache(str(path))

    assert os.stat(str(path)).st_mtime_ns == before


# --- the offline tools ----------------------------------------------------------

def test_coverage_report_is_owner_only(tmp_path, monkeypatch):
    """`python -m app.autofill.coverage --out` writes plan values; the file
    must come out 0600 whatever the umask."""
    from app.autofill import coverage
    from app.autofill.greenhouse import parse_greenhouse_job
    from app.autofill.memory import Memory

    form = parse_greenhouse_job({"id": 1, "company_name": "Acme", "questions": [
        {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text"}]}]})
    out = tmp_path / "coverage.json"
    monkeypatch.setattr(coverage, "load_memory", lambda *_a, **_k: Memory(facts={"first_name": "Ada"}))
    monkeypatch.setattr(coverage, "load_corpus", lambda *_a, **_k: [form])
    monkeypatch.setattr(coverage, "load_ashby", lambda *_a, **_k: [form])

    assert coverage.main(["--out", str(out)]) == 0
    assert _mode(out) == "0o600"
    assert json.loads(out.read_text())["ats"]["greenhouse"]["forms"] == 1
