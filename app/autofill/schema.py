"""
Normalised representation of a job-application form.

Three facts about real ATS forms drive this design. All three were established
by reading live postings — the Greenhouse job board API and the DOM of a live
Ashby form — rather than assumed:

  1. **Core fields carry stable machine names; custom questions do not.**
     Greenhouse names core fields `first_name`, `email`, `resume`, and names
     custom questions `question_8581808008`. Ashby uses `_systemfield_name`,
     `_systemfield_email` and a bare UUID for custom questions. Both allocate
     the custom identifier per posting, so a lookup table keyed on field name
     can never resolve a custom question, and **the label is the only thing
     carrying meaning**. That is precisely what makes binding a semantic
     judgement rather than a dictionary lookup — and therefore what Jev is
     for. It is also why binding cannot be hardcoded, however much one wants
     to.

  2. **The type vocabulary is tiny and closed.** Across 40 live Greenhouse
     postings the only field types appearing at all were `input_text` (357),
     `multi_value_single_select` (262), `textarea` (115) and `input_file`
     (44). Ashby adds a checkbox. A set that small belongs in an enum and a
     branch, not in a model — an `if` that costs nothing beats a call that can
     be wrong.

  3. **Legally sensitive questions are structurally separated, by both
     vendors.** Greenhouse delivers EEOC questions in a top-level `compliance`
     block tagged `{"type": "eeoc"}`; Ashby names them with an
     `_systemfield_eeoc` prefix. Classifying from the vendor's own marker is
     exact, and it is the only sensitive-field rule there is — an earlier
     label-wording regex decided nothing across 2,429 fields and, on a real
     Ashby form, would have missed race and gender entirely, because the radio
     inputs are labelled with the answer rather than the question.
"""
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class FieldKind(str, Enum):
    """The control's shape. Closed set; see note 2 in the module docstring."""

    TEXT = "text"
    LONG_TEXT = "long_text"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    BOOLEAN = "boolean"
    FILE = "file"


class FieldClass(str, Enum):
    """What kind of question this is. Decides routing *and* fill policy.

    This is the taxonomy jev-apply reports against (core / screening /
    narrative); LEGAL and CONSENT are split out because they are the two
    classes that must never be filled from a model's judgement, however
    confident it is.
    """

    CORE = "core"
    SCREENING = "screening"
    NARRATIVE = "narrative"
    LEGAL = "legal"
    CONSENT = "consent"
    DOCUMENT = "document"
    UNKNOWN = "unknown"


class FillSource(str, Enum):
    """Where a field's value is allowed to come from.

    Separating this from FieldClass is the correction that makes the tool
    worth using. The first cut of this file treated every legally sensitive
    field as unfillable, which confused two completely different acts:

      - A model **inferring** a protected characteristic from a resume. That
        is a guess about protected data, submitted under someone's name, and
        it must never happen.
      - The applicant **stating** the answer once and the tool replaying it.
        That is not a guess, it is memory. It is byte-for-byte what they would
        have typed, and typing it for the fortieth time is the chore this
        exists to remove.

    So "never let a model near veteran status" is right, and "never fill
    veteran status" was wrong.
    """

    #: An answer the applicant gave explicitly, replayed verbatim. No
    #: inference, so this is permitted even for protected characteristics.
    MEMORY = "memory"

    #: Jev choosing among stored facts or the form's own options.
    MODEL_DECISION = "model_decision"

    #: An LLM composing new prose.
    MODEL_DRAFT = "model_draft"

    #: Only the applicant, in the moment. Not stored, not replayed.
    HUMAN = "human"


#: What may produce a value, per class.
#:
#: LEGAL is MEMORY-only: EEOC, veteran and disability answers are the
#: applicant's own, and "I don't wish to answer" is itself a perfectly good
#: stored answer. They are replayed, never inferred.
#:
#: CONSENT is the one class that stays HUMAN-only, and the reason is a real
#: distinction rather than caution: veteran status **reports a fact that
#: already exists**, so replaying it changes nothing. An arbitration agreement
#: **creates an obligation at the moment of the click** — there is no prior
#: fact to replay, the act is the click. A tool may type what you told it. It
#: should not enter into an agreement for you.
ALLOWED_FILL_SOURCES = {
    FieldClass.CORE: frozenset({FillSource.MEMORY}),
    FieldClass.DOCUMENT: frozenset({FillSource.MEMORY}),
    FieldClass.LEGAL: frozenset({FillSource.MEMORY}),
    FieldClass.SCREENING: frozenset({FillSource.MEMORY, FillSource.MODEL_DECISION}),
    FieldClass.NARRATIVE: frozenset(
        {FillSource.MEMORY, FillSource.MODEL_DECISION, FillSource.MODEL_DRAFT}
    ),
    # MEMORY here means a *standing consent* the applicant recorded once
    # (Memory.consents); no model source, and no stored answer means human.
    FieldClass.CONSENT: frozenset({FillSource.MEMORY, FillSource.HUMAN}),
    FieldClass.UNKNOWN: frozenset({FillSource.HUMAN}),
}

#: Judgement sources, as opposed to the applicant's own stored answer. A
#: protected-characteristic field must be reachable by neither.
MODEL_SOURCES = frozenset({FillSource.MODEL_DECISION, FillSource.MODEL_DRAFT})


class FieldOption(BaseModel):
    """One selectable option, as the ATS names it.

    `value` is what gets submitted and `label` is what the applicant reads.
    They differ constantly in practice — Greenhouse sends `{"label": "Yes",
    "value": 1}` — so a binding decision must be made over labels and then
    submitted as the value.
    """

    label: str
    value: str


class FormField(BaseModel):
    """One question on an application form, normalised across ATS vendors."""

    key: str = Field(description="The ATS field name; stable within one posting")
    label: str
    kind: FieldKind
    field_class: FieldClass = FieldClass.UNKNOWN
    required: bool = False
    options: List[FieldOption] = Field(default_factory=list)
    #: The control accepts only a number (Ashby's `Number` type, rendered as
    #: <input type=number>). Kept as a flag rather than a FieldKind so every
    #: text rule still applies; the binder refuses to call a non-numeric
    #: value filled (R48-2: "8 weeks +" into "notice period (in months)").
    numeric: bool = False

    def _allowed_sources(self) -> frozenset:
        allowed = ALLOWED_FILL_SOURCES.get(self.field_class, frozenset())
        # A consent is fillable only when it is one of the standing consents
        # the applicant can record once (classify.CONSENT_BUCKETS). Arbitration
        # agreements and the AI-policy attestation match no bucket and remain
        # human whatever the profile says.
        if self.field_class == FieldClass.CONSENT:
            from app.autofill.classify import consent_key_for  # local: classify imports this module

            if consent_key_for(self.label) is None:
                return frozenset({FillSource.HUMAN})
        return allowed

    def may_fill_from(self, source: "FillSource") -> bool:
        """Whether `source` is permitted to produce this field's value."""
        return source in self._allowed_sources()

    def allows_model_judgement(self) -> bool:
        """Whether a model's *judgement* may produce this field's value.

        Deliberately narrower than "can this be filled at all": a legally
        sensitive field is filled from the applicant's own stored answer, so
        it is fillable without a model ever seeing it.
        """
        return bool(ALLOWED_FILL_SOURCES.get(self.field_class, frozenset()) & MODEL_SOURCES)

    def allows_option_translation(self) -> bool:
        """Whether a model may map a stored answer onto this form's wording.

        Narrower than `may_fill_from(MODEL_DECISION)`: the model is not deciding
        *what* the answer is, only how this particular form words it. The value
        still comes from the applicant. That distinction is what lets "Careers
        Website" reach an option list offering "Career Page".

        LEGAL and CONSENT are excluded anyway. For a protected characteristic
        the stricter rule is worth keeping even for translation, and a consent
        field has no stored answer to translate.
        """
        return self.field_class not in (
            FieldClass.LEGAL, FieldClass.CONSENT, FieldClass.UNKNOWN
        )

    def is_autofillable(self) -> bool:
        """Whether this field can be filled without interrupting the applicant.

        True for everything the applicant has already answered once, including
        demographics — which is the whole point of the product.
        """
        return bool(self._allowed_sources() - {FillSource.HUMAN})

    def needs_onboarding_answer(self) -> bool:
        """Whether this field can only be answered by asking the applicant once.

        These are the questions that belong on a one-time onboarding screen:
        answered on day one, replayed on every application after it.
        """
        return ALLOWED_FILL_SOURCES.get(self.field_class) == frozenset({FillSource.MEMORY})


class FormSchema(BaseModel):
    """Every question on one application, plus where it came from."""

    source: str = Field(description="Adapter that produced this, e.g. 'greenhouse_api'")
    ats: str
    posting_id: str
    company: Optional[str] = None
    title: Optional[str] = None
    apply_url: Optional[str] = None

    #: Country the role is in, as a two-letter code. Load-bearing rather than
    #: decorative: 28 postings ask "are you authorized to work in the country
    #: for which you applied" and name no country at all, so the only place the
    #: answer can come from is the posting's own location.
    country: Optional[str] = None

    #: Whether the posting is remote, where the ATS says so. Only 5 of 57
    #: intern postings were, so this decides very little — but it is the one
    #: condition the applicant's term preference actually turns on.
    remote: Optional[bool] = None

    fields: List[FormField] = Field(default_factory=list)

    def by_class(self, field_class: FieldClass) -> List[FormField]:
        return [f for f in self.fields if f.field_class == field_class]
