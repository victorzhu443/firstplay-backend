"""
Greenhouse adapter.

Greenhouse publishes the whole application form as JSON, unauthenticated:

    GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}?questions=true

That is worth dwelling on, because it removes the single largest risk from
this project for one of the two launch targets. Scraping an application form
out of a live DOM is the part that sinks autofill tools — it breaks on every
redesign, and the value submitted for a select is frequently not the text the
applicant sees. Here the ATS hands over field names, types, required flags,
option label/value pairs and help text directly. Extraction stops being a
scraping problem and becomes a parsing problem.

The browser extension still has to find the live controls in order to fill
them. But the API is the *authority* on what the form contains, and the two
reconcile on field name, which is stable in both.
"""
from typing import Any, Dict, List, Optional

from app.autofill.classify import classify
from app.autofill.schema import FieldKind, FieldOption, FormField, FormSchema
from app.autofill.themes import country_in_text

#: Observed across 40 live postings; no other type appeared. `multi_value_
#: multi_select` is included from the documented vocabulary though it did not
#: occur in the sample.
_KIND_BY_TYPE = {
    "input_text": FieldKind.TEXT,
    "textarea": FieldKind.LONG_TEXT,
    "input_file": FieldKind.FILE,
    "multi_value_single_select": FieldKind.SINGLE_SELECT,
    "multi_value_multi_select": FieldKind.MULTI_SELECT,
}


def _options(raw_values: List[Dict[str, Any]]) -> List[FieldOption]:
    """Option label/value pairs, with the value coerced to a string.

    Greenhouse types option values inconsistently — a yes/no question uses the
    integers 1 and 0, while an arbitration agreement uses the integer
    44048486008 and an EEOC question uses the strings "1", "2", "3". Carrying
    that straight through would make every consumer handle both, so it is
    normalised once, here.
    """
    options = []

    for value in raw_values or []:
        options.append(
            FieldOption(label=str(value.get("label", "")), value=str(value.get("value", "")))
        )

    return options


def _fields_from_question(
    question: Dict[str, Any], *, from_compliance_block: bool
) -> List[FormField]:
    """Flatten one Greenhouse question into normalised fields.

    One question can carry several fields: "Resume/CV" ships both `resume`
    (an upload) and `resume_text` (a paste alternative). They share the
    question's label and required flag but are separate controls.
    """
    label = (question.get("label") or "").strip()
    required = bool(question.get("required"))

    fields = []

    for raw in question.get("fields") or []:
        kind = _KIND_BY_TYPE.get(raw.get("type"))

        if kind is None:
            # An unrecognised type is recorded rather than dropped: a field we
            # cannot classify must still appear in the corpus, or the eval
            # silently scores itself against a form it has quietly shrunk.
            kind = FieldKind.TEXT

        options = _options(raw.get("values"))

        fields.append(
            FormField(
                key=raw.get("name") or "",
                label=label,
                kind=kind,
                field_class=classify(
                    raw.get("name") or "",
                    label,
                    kind,
                    option_count=len(options),
                    from_compliance_block=from_compliance_block,
                ),
                required=required,
                options=options,
            )
        )

    return fields


def _is_remote(payload: Dict[str, Any]) -> Optional[bool]:
    """Whether the ATS describes this posting as remote.

    Returns None rather than False when nothing says either way, so a caller
    can tell "stated to be onsite" from "not stated".
    """
    text = " ".join(
        [(payload.get("location") or {}).get("name") or ""]
        + [(o or {}).get("name") or "" for o in payload.get("offices") or []]
    ).lower()

    if "remote" in text:
        return True

    return False if text.strip() else None


_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _demographic_fields(payload: Dict[str, Any]) -> List[FormField]:
    """Greenhouse's "Voluntary Self Identification" block.

    Separate from `compliance` (EEOC) and separate from `questions`; keyed by
    number, rendered as `demographic_answers.<id>`. Measured on Duolingo
    8805925002 (2026-09-27): six questions, two of them multi-select. Treated
    as LEGAL on the strength of arriving in this block, like compliance.
    """
    block = payload.get("demographic_questions") or {}
    fields = []

    for question in block.get("questions") or []:
        key = "demographic_answers.{}".format(question.get("id"))
        label = (question.get("label") or "").strip()
        kind = _KIND_BY_TYPE.get(question.get("type"), FieldKind.SINGLE_SELECT)
        options = [
            FieldOption(label=str(o.get("label", "")).strip(), value=str(o.get("id", "")))
            for o in question.get("answer_options") or []
        ]
        fields.append(FormField(
            key=key, label=label, kind=kind,
            field_class=classify(key, label, kind, option_count=len(options),
                                 from_compliance_block=True),
            required=bool(question.get("required")), options=options,
        ))

    return fields


def _education_fields(payload: Dict[str, Any]) -> List[FormField]:
    """The education block, which the API describes only by a mode flag.

    `education: education_required|education_optional` says the form renders
    school / degree / discipline / end date, but lists none of them under
    `questions`. Degree and discipline options come from the board's public
    `/education/degrees` and `/education/disciplines` endpoints, attached by
    the caller as `payload["education_options"]`; without them the two are
    plain text and the filler types the answer.

    Only the end date is synthesised: the one form measured (Duolingo) renders
    no start date. Fields the form does not render are reported by the filler
    as "not on page", which costs nothing.
    """
    mode = payload.get("education")
    if mode not in ("education_required", "education_optional"):
        return []

    required = mode == "education_required"
    extra = payload.get("education_options") or {}

    def select(key: str, label: str, items: Optional[List[Dict[str, Any]]],
               fallback_options: Optional[List[FieldOption]] = None) -> FormField:
        options = [FieldOption(label=str(i.get("text", "")), value=str(i.get("id", "")))
                   for i in items or []] or (fallback_options or [])
        kind = FieldKind.SINGLE_SELECT if options else FieldKind.TEXT
        return FormField(key=key, label=label, kind=kind,
                         field_class=classify(key, label, kind, option_count=len(options)),
                         required=required, options=options)

    months = [FieldOption(label=m, value=str(i + 1)) for i, m in enumerate(_MONTHS)]

    return [
        select("educations[0].school_name_id", "School", None),
        select("educations[0].degree_id", "Degree", extra.get("degrees")),
        select("educations[0].discipline_id", "Discipline", extra.get("disciplines")),
        # Start dates: rendered by some boards (Chicago Trading, round 2) and
        # required there; "not on page" elsewhere, which costs nothing.
        select("educations[0].start_date.month", "Start date month", None, months),
        select("educations[0].start_date.year", "Start date year", None),
        select("educations[0].end_date.month", "End date month", None, months),
        select("educations[0].end_date.year", "End date year", None),
    ]


def _country_field() -> List[FormField]:
    """The `country` select every standard Greenhouse form renders beside Phone.

    Not in any API block; the form marks it required (measured on Mill,
    Coinbase, Gallup, Chicago Trading: "Country: Select a country" after a
    dry-run submit). Answered from the applicant's country of residence.
    """
    key, label, kind = "country", "Country", FieldKind.SINGLE_SELECT
    return [FormField(key=key, label=label, kind=kind,
                      field_class=classify(key, label, kind), required=True, options=[])]


def _location_fields(payload: Dict[str, Any]) -> List[FormField]:
    """The candidate-location block: `location` plus hidden longitude/latitude.

    Present and required on 57/57 corpus postings (2026-09-27), and never in
    `questions`. The visible control is a geocoder typeahead; choosing a
    suggestion fills the two hidden coordinates, so only `location` is a
    field to answer and the hidden pair is dropped rather than shown as
    unfillable text.
    """
    fields = []

    for question in payload.get("location_questions") or []:
        for raw in question.get("fields") or []:
            if raw.get("type") == "input_hidden":
                continue
            key = raw.get("name") or ""
            label = (question.get("label") or "").strip()
            kind = _KIND_BY_TYPE.get(raw.get("type"), FieldKind.TEXT)
            fields.append(FormField(
                key=key, label=label, kind=kind,
                field_class=classify(key, label, kind),
                required=bool(question.get("required")), options=[],
            ))

    return fields


def parse_greenhouse_job(payload: Dict[str, Any], *, board: Optional[str] = None) -> FormSchema:
    """Normalise a Greenhouse job payload into a FormSchema.

    Args:
        payload: Decoded JSON from the board API with `questions=true`
        board: Board token, recorded for provenance

    Returns:
        The normalised form
    """
    fields: List[FormField] = []

    for question in payload.get("questions") or []:
        fields.extend(_fields_from_question(question, from_compliance_block=False))

    # Compliance blocks arrive separately and are trusted as legal on the
    # strength of arriving there, rather than on their wording.
    for block in payload.get("compliance") or []:
        for question in block.get("questions") or []:
            fields.extend(_fields_from_question(question, from_compliance_block=True))

    fields.extend(_education_fields(payload))
    fields.extend(_demographic_fields(payload))
    fields.extend(_location_fields(payload))
    fields.extend(_country_field())

    return FormSchema(
        source="greenhouse_api",
        ats="greenhouse",
        posting_id=str(payload.get("id", "")),
        company=payload.get("company_name") or board,
        title=payload.get("title"),
        apply_url=payload.get("absolute_url"),
        remote=_is_remote(payload),
        country=country_in_text(
            " ".join(
                [(payload.get("location") or {}).get("name") or ""]
                + [(o or {}).get("name") or "" for o in payload.get("offices") or []]
            )
        ),
        fields=fields,
    )
