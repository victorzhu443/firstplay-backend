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
from app.autofill.schema import FieldClass, FieldKind, FieldOption, FormField, FormSchema
from app.autofill.themes import country_in_text


class MalformedPayload(ValueError):
    """An ATS payload that is JSON but not the shape the adapter reads.

    Raised with the path of the offending node (`form.questions[2].fields`)
    so the router can answer 400 and name it. Before this, eight such shapes
    reached `.get` on a string and came back as 500s with a traceback in
    the log (audit 2026-10-12, F11; probe p14). Shared with the Ashby
    adapter, which is why it lives at module level here.
    """


def _as_list(value: Any, path: str) -> List[Any]:
    """`value` as a list; None is empty; anything else is malformed."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    raise MalformedPayload("{}: expected a list, got {}".format(path, type(value).__name__))


def _as_dict(value: Any, path: str) -> Dict[str, Any]:
    """`value` as a dict; None is empty; anything else is malformed."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    raise MalformedPayload("{}: expected an object, got {}".format(path, type(value).__name__))

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


def _options(raw_values: Any, path: str = "values") -> List[FieldOption]:
    """Option label/value pairs, with the value coerced to a string.

    Greenhouse types option values inconsistently — a yes/no question uses the
    integers 1 and 0, while an arbitration agreement uses the integer
    44048486008 and an EEOC question uses the strings "1", "2", "3". Carrying
    that straight through would make every consumer handle both, so it is
    normalised once, here.
    """
    options = []

    for index, value in enumerate(_as_list(raw_values, path)):
        value = _as_dict(value, "{}[{}]".format(path, index))
        options.append(
            FieldOption(label=str(value.get("label", "")), value=str(value.get("value", "")))
        )

    return options


def _fields_from_question(
    question: Any, *, from_compliance_block: bool, path: str = "question"
) -> List[FormField]:
    """Flatten one Greenhouse question into normalised fields.

    One question can carry several fields: "Resume/CV" ships both `resume`
    (an upload) and `resume_text` (a paste alternative). They share the
    question's label and required flag but are separate controls.
    """
    question = _as_dict(question, path)
    label = str(question.get("label") or "").strip()
    required = bool(question.get("required"))

    fields = []

    for index, raw in enumerate(_as_list(question.get("fields"), path + ".fields")):
        raw = _as_dict(raw, "{}.fields[{}]".format(path, index))
        kind = _KIND_BY_TYPE.get(raw.get("type"))

        if kind is None:
            # An unrecognised type is recorded rather than dropped: a field we
            # cannot classify must still appear in the corpus, or the eval
            # silently scores itself against a form it has quietly shrunk.
            kind = FieldKind.TEXT

        options = _options(raw.get("values"), "{}.fields[{}].values".format(path, index))

        fields.append(
            FormField(
                key=str(raw.get("name") or ""),
                label=label,
                kind=kind,
                field_class=classify(
                    str(raw.get("name") or ""),
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


def _location_text(payload: Dict[str, Any]) -> str:
    """The posting's location and office names, joined."""
    names = [str(_as_dict(payload.get("location"), "form.location").get("name") or "")]
    for index, office in enumerate(_as_list(payload.get("offices"), "form.offices")):
        names.append(str(_as_dict(office, "form.offices[{}]".format(index)).get("name") or ""))
    return " ".join(names)


def _is_remote(payload: Dict[str, Any]) -> Optional[bool]:
    """Whether the ATS describes this posting as remote.

    Returns None rather than False when nothing says either way, so a caller
    can tell "stated to be onsite" from "not stated".
    """
    text = _location_text(payload).lower()

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
    block = _as_dict(payload.get("demographic_questions"), "form.demographic_questions")
    fields = []

    for index, question in enumerate(_as_list(block.get("questions"), "form.demographic_questions.questions")):
        path = "form.demographic_questions.questions[{}]".format(index)
        question = _as_dict(question, path)
        key = "demographic_answers.{}".format(question.get("id"))
        label = str(question.get("label") or "").strip()
        kind = _KIND_BY_TYPE.get(question.get("type"), FieldKind.SINGLE_SELECT)
        options = [
            FieldOption(label=str(_as_dict(o, path + ".answer_options[]").get("label", "")).strip(),
                        value=str(_as_dict(o, path + ".answer_options[]").get("id", "")))
            for o in _as_list(question.get("answer_options"), path + ".answer_options")
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
    extra = _as_dict(payload.get("education_options"), "form.education_options")

    def select(key: str, label: str, items: Any,
               fallback_options: Optional[List[FieldOption]] = None) -> FormField:
        path = "form.education_options." + key
        options = [FieldOption(label=str(_as_dict(i, path + "[]").get("text", "")),
                               value=str(_as_dict(i, path + "[]").get("id", "")))
                   for i in _as_list(items, path)] or (fallback_options or [])
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

    Its plan entry also carries `iso2` and `dial_code` ("US", "+1"), set by
    `binder._mark_country_codes`: a localised board renders the option as
    "États-Unis +1" (Haven Interactive, French; R50 live round) and the dial
    code is the one token the extension can match across locales. `value`
    stays the stored country name.
    """
    key, label, kind = "country", "Country", FieldKind.SINGLE_SELECT
    return [FormField(key=key, label=label, kind=kind,
                      field_class=classify(key, label, kind), required=True, options=[])]


def _hispanic_field(fields: List[FormField]) -> List[FormField]:
    """Greenhouse's first EEO question, which the board API never lists.

    The API's compliance block carries one field, `race`; the page renders
    the two-question block — `hispanic_ethnicity` ("Yes" / "No" / "Decline
    To Self Identify") first, with `race` mounted only after it is answered.
    Measured on 7 of 9 live boards (DECISIONS §34). Synthesised, like
    `_country_field`, whenever the form carries a `race` compliance field.

    Answered only by exact replay of the applicant's own
    `protected.hispanic_latino`; never derived from the stored race. The
    extension used to derive it by substring, and Ashby's own race wording
    "White (Not Hispanic or Latino)" came out as Yes (audit 2026-10-12, H1).
    With nothing stored the entry is a `needs_fact` question, not a guess.
    """
    if not any(f.key == "race" and f.field_class == FieldClass.LEGAL for f in fields):
        return []

    key, label, kind = "hispanic_ethnicity", "Are you Hispanic or Latino?", FieldKind.SINGLE_SELECT
    options = [FieldOption(label=o, value=o) for o in ("Yes", "No", "Decline To Self Identify")]
    return [FormField(key=key, label=label, kind=kind,
                      field_class=classify(key, label, kind, option_count=len(options),
                                           from_compliance_block=True),
                      required=False, options=options)]


def _location_fields(payload: Dict[str, Any]) -> List[FormField]:
    """The candidate-location block: `location` plus hidden longitude/latitude.

    Present and required on 57/57 corpus postings (2026-09-27), and never in
    `questions`. The visible control is a geocoder typeahead; choosing a
    suggestion fills the two hidden coordinates, so only `location` is a
    field to answer and the hidden pair is dropped rather than shown as
    unfillable text.
    """
    fields = []

    for q_index, question in enumerate(_as_list(payload.get("location_questions"), "form.location_questions")):
        path = "form.location_questions[{}]".format(q_index)
        question = _as_dict(question, path)
        for f_index, raw in enumerate(_as_list(question.get("fields"), path + ".fields")):
            raw = _as_dict(raw, "{}.fields[{}]".format(path, f_index))
            if raw.get("type") == "input_hidden":
                continue
            key = str(raw.get("name") or "")
            label = str(question.get("label") or "").strip()
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

    for index, question in enumerate(_as_list(payload.get("questions"), "form.questions")):
        fields.extend(_fields_from_question(question, from_compliance_block=False,
                                            path="form.questions[{}]".format(index)))

    # Compliance blocks arrive separately and are trusted as legal on the
    # strength of arriving there, rather than on their wording.
    for b_index, block in enumerate(_as_list(payload.get("compliance"), "form.compliance")):
        block_path = "form.compliance[{}]".format(b_index)
        block = _as_dict(block, block_path)
        for q_index, question in enumerate(_as_list(block.get("questions"), block_path + ".questions")):
            fields.extend(_fields_from_question(question, from_compliance_block=True,
                                                path="{}.questions[{}]".format(block_path, q_index)))

    # Rendered before `race` on the page, so placed before it in the plan.
    hispanic = _hispanic_field(fields)
    if hispanic:
        race_at = next(i for i, f in enumerate(fields) if f.key == "race")
        fields[race_at:race_at] = hispanic

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
        country=country_in_text(_location_text(payload)),
        fields=fields,
    )
