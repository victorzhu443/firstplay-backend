"""
Ashby, from its own form definition.

The application page loads its form with one public GraphQL operation,
`ApiJobPosting` (`POST jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting`,
no auth), whose response carries `applicationForm.sections[].fieldEntries[]`
with each field's type, title, path, options and required flag. The earlier
adapter (`ashby.py`) read the live DOM because `window.__appData` and the
posting API carry no form; the query text was recovered from Ashby's
front-end bundle (docs/DECISIONS.md §46) and is checked in beside this file.

Why this matters: a Greenhouse plan is built from the board API at first
sight of the posting and is ready before the page has rendered; Ashby plans
waited for the DOM to settle. With the definition in hand the same shape
applies, and the join to the page is exact — every Ashby control's `id` and
`name` equal the field's `path`, and its container carries
`data-field-path`. Nothing is matched by label.

The extension fetches the operation (it holds the host permission) and posts
the `jobPosting` object here, exactly as it posts Greenhouse's job JSON. This
module stays network-free and testable from a fixture.
"""
import os
from typing import Any, Dict, List, Optional

from app.autofill.classify import classify
from app.autofill.schema import FieldKind, FieldOption, FormField, FormSchema

#: The query the page itself sends, reconstructed from the bundle's AST.
QUERY_PATH = os.path.join(os.path.dirname(__file__), "ashby_posting.graphql")
ENDPOINT = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"

#: Ashby field types → control shape. Anything unlisted is text, which is
#: also what the page renders for it.
_KIND_BY_TYPE = {
    "String": FieldKind.TEXT,
    "Email": FieldKind.TEXT,
    "Phone": FieldKind.TEXT,
    "Number": FieldKind.TEXT,
    "Date": FieldKind.TEXT,
    "Location": FieldKind.TEXT,          # typeahead; the filler drives it as an autocomplete
    "LongText": FieldKind.LONG_TEXT,
    "File": FieldKind.FILE,
    "Boolean": FieldKind.BOOLEAN,
    "ValueSelect": FieldKind.SINGLE_SELECT,
    "MultiValueSelect": FieldKind.MULTI_SELECT,
}


def load_query() -> str:
    with open(QUERY_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def request_body(org: str, job_posting_id: str) -> Dict[str, Any]:
    """The JSON the extension posts to Ashby to fetch a posting's form."""
    return {
        "operationName": "ApiJobPosting",
        "variables": {"organizationHostedJobsPageName": org, "jobPostingId": job_posting_id},
        "query": load_query(),
    }


def looks_like_posting(payload: Dict[str, Any]) -> bool:
    """Whether a payload is the GraphQL `jobPosting` object rather than a DOM extract."""
    posting = _posting(payload)
    return isinstance(posting, dict) and isinstance(posting.get("applicationForm"), dict)


def _posting(payload: Dict[str, Any]) -> Dict[str, Any]:
    # Accept the raw GraphQL envelope, `data`, or the posting itself.
    if "data" in payload and isinstance(payload["data"], dict):
        payload = payload["data"]
    if "jobPosting" in payload and isinstance(payload["jobPosting"], dict):
        payload = payload["jobPosting"]
    return payload


def parse_ashby_posting(payload: Dict[str, Any], *, org: Optional[str] = None) -> FormSchema:
    """Normalise an `ApiJobPosting` response into a FormSchema.

    Hidden sections and entries are skipped: the page does not render them
    and the form does not require them. Deactivated fields likewise.
    """
    posting = _posting(payload)
    form = posting.get("applicationForm") or {}
    fields: List[FormField] = []

    for section in form.get("sections") or []:
        if section.get("isHidden"):
            continue
        for entry in section.get("fieldEntries") or []:
            if entry.get("isHidden"):
                continue
            field = entry.get("field") or {}
            if field.get("isDeactivated"):
                continue
            key = str(field.get("path") or field.get("id") or "")
            # Titles carry NBSPs and doubled spaces ("What University\xa0 do you…").
            label = " ".join(str(field.get("title") or field.get("humanReadablePath") or "").split())
            kind = _KIND_BY_TYPE.get(str(field.get("type") or ""), FieldKind.TEXT)
            options = [
                FieldOption(label=str(o.get("label", "")), value=str(o.get("value", "")))
                for o in field.get("selectableValues") or []
                if not o.get("isArchived")
            ]
            # A ValueSelect with one option and a checkbox-like title is an
            # agreement; the classifier sees the count and handles it.
            fields.append(
                FormField(
                    numeric=str(field.get("type") or "") == "Number",
                    key=key,
                    label=label,
                    kind=kind,
                    field_class=classify(key, label, kind, option_count=len(options)),
                    required=bool(entry.get("isRequired")),
                    options=options,
                )
            )

    workplace = str(posting.get("workplaceType") or "").lower()
    return FormSchema(
        source="ashby_api",
        ats="ashby",
        posting_id=str(posting.get("id") or ""),
        company=org or posting.get("organizationName"),
        title=posting.get("title"),
        apply_url=None,
        remote=(True if workplace == "remote" else None),
        fields=fields,
    )
