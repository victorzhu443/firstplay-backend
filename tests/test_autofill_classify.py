"""
Classification rules from the outside audit of 2026-10-12 (AUDIT_REPORT.md).

The audit traced 58 labels through `classify` and the deterministic binder
and found the wrong rows in one place: a keyword read in another sense. Each
test here names the audit item it closes and the label that was wrong, so a
later pattern change that re-opens the row fails with the measurement
attached.
"""
import pytest

from app.autofill.classify import (
    classify,
    consent_key_for,
    memory_key_for,
    memory_only_key_for,
    protected_key_for,
)
from app.autofill.schema import FieldClass, FieldKind, FieldOption, FormField


def _field(label, kind=FieldKind.SINGLE_SELECT, options=("Yes", "No"), key="question_1"):
    opts = [FieldOption(label=o, value=o) for o in options]
    return FormField(key=key, label=label, kind=kind, options=opts,
                     field_class=classify(key, label, kind, option_count=len(opts)))


# --- H3: an accommodation request is not the EEOC disability question --------

@pytest.mark.parametrize("label", [
    # The three wordings MEMORY_ONLY_LABEL_KEYS was written against.
    "Please describe any need for a reasonable accommodation for this hiring process",
    "If you require any support or adjustments during the recruitment process, please let us know",
    "Do you require any accommodations for the interview?",
    # The two the audit traced to `disability_status` (H3, §2 rows 3 and 4).
    "If you require accommodations due to a disability, please describe them",
    "Do you have a disability that requires accommodation during the interview?",
    "If you need assistance during the interview process, please let us know",
])
def test_accommodation_wording_never_replays_the_eeoc_disability_answer(label):
    """Audit H3: after the 2026-10-12 protected-label rule, any label with
    "disabilit" replayed the stored EEOC sentence — into a free-text
    accommodation box. The accommodation wording is the question; it has its
    own memory-only key, and it is read before the protected patterns."""
    assert memory_only_key_for(label) == "accommodation_needs"
    assert memory_key_for("question_1", label, FieldKind.TEXT) == "accommodation_needs"
    assert memory_key_for("question_1", label, FieldKind.SINGLE_SELECT) != "disability_status"
    assert classify("question_1", label, FieldKind.TEXT) == FieldClass.LEGAL


@pytest.mark.parametrize("label", [
    "Disability Status",
    "Do you have a disability?",
    "Please indicate your disability status",
])
def test_the_eeoc_disability_question_as_a_custom_label_still_replays(label):
    """The rule H3 corrected stays: a disability question with no accommodation
    wording is the EEOC question wherever it sits (Canonical, §52)."""
    assert protected_key_for("question_1", label) == "disability_status"
    assert memory_key_for("question_1", label, FieldKind.SINGLE_SELECT) == "disability_status"
    field = _field(label, options=("Yes", "No", "I don't wish to answer"))
    assert field.field_class == FieldClass.LEGAL
    assert not field.allows_model_judgement()


def test_the_vendor_marker_outranks_accommodation_wording():
    """Greenhouse's compliance block names the EEOC disability question by key
    and its official text mentions accommodation. The vendor's marker is
    exact and wins; only custom keys are decided by wording."""
    label = ("Please check one of the boxes below: ... a disability is a condition that "
             "substantially limits ... reasonable accommodation")
    assert memory_key_for("disability_status", label) == "disability_status"
    assert memory_key_for("abc__systemfield_eeoc_disability_status", label) == "disability_status"
    assert memory_key_for("question_1", "Will you need housing accommodation in Seattle?") is None
