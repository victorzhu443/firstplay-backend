"""Prompt-injection robustness for the autofill / job-parsing source-text path.

Deterministic and cost-free: no model is called. We model the app's sink —
scraped job-page text concatenated into the extraction prompt in
`app/chains/job_parser.py` (`{job_text}`), the same shape the autofill binder
uses for field/option text — and prove two things:

1. Undefended, the documented injection corpus subverts it (the risk is real).
2. Routed through `app.security.harden_untrusted`, the corpus is resisted.

This is the measure->mitigate->verify loop from security-agents/promptinject,
bound to THIS app's untrusted channel (an attacker-controlled job posting).
"""
from promptinject.corpus import CANARY, corpus
from promptinject.harness import evaluate

from app.security import harden_untrusted, scrub_output


def _sink(job_text: str) -> str:
    """Stand-in for the scraped-text prompt sink. A vulnerable app effectively
    lets embedded instructions through; we emulate that by surfacing the text,
    so a canary planted in the posting reaches 'output'. Mirrors the user
    message built in app/chains/job_parser.py."""
    return f"Parse the following job description:\n{job_text}"


def _defended(job_text: str) -> str:
    return scrub_output(_sink(harden_untrusted(job_text)))


def test_corpus_lands_on_undefended_sink():
    # Sanity: the threat is real; the naive sink surfaces the injected canary.
    report = evaluate(_sink, corpus())
    assert report.landed, "corpus should expose the undefended sink"


def test_hardened_untrusted_resists_corpus():
    report = evaluate(_defended, corpus())
    assert report.ok, "\n" + report.render()


def test_control_document_is_not_flagged():
    # The benign control must pass through hardening without a canary.
    assert CANARY not in _defended("Backend intern role. Python, FastAPI, SQL.")
