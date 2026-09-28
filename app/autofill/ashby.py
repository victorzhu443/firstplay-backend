"""
Ashby adapter.

Ashby's public posting API returns only posting metadata — title, location,
description HTML, apply URL. It does **not** return the application form, and
`window.__appData` on the application page carries the posting but not the
form either. So unlike Greenhouse, Ashby genuinely requires reading the live
DOM, and this adapter normalises what the browser extension saw.

Reading two real Ashby forms made that cheap rather than frightening, and also
caught a defect that no amount of reasoning had:

  - **No shadow DOM.** Both forms reported zero shadow roots, so ordinary
    `querySelectorAll` reaches every control. This was the specific fear worth
    testing: jev-fill lists "custom shadow-DOM controls" as out of scope.
  - **Radio groups share a `name`, and each input is labelled with its own
    *option*, not the question.** A race/ethnicity question arrives as eight
    separate radio inputs labelled "Hispanic or Latino", "White (Not Hispanic
    or Latino)" and so on, with no fieldset legend anywhere. Treating each
    input as its own field — which the first version of this file did — turned
    one protected-characteristic question into eight fields whose labels match
    no sensitive-wording pattern, and therefore classified them as ordinary
    screening questions a model was free to answer. Grouping by `name` is what
    makes that question a single field again.
  - **The group name carries the marker**:
    `..._systemfield_eeoc_race`, `..._systemfield_eeoc_veteran_status`. Ashby
    tags these itself, so classification reads the vendor's own structure
    rather than guessing from wording — which could not have worked here,
    since the question text is not in the DOM at all.
"""
from collections import OrderedDict
from typing import Any, Dict, List

from app.autofill.classify import classify
from app.autofill.schema import FieldKind, FieldOption, FormField, FormSchema

_KIND_BY_CONTROL = {
    ("input", "text"): FieldKind.TEXT,
    ("input", "email"): FieldKind.TEXT,
    ("input", "tel"): FieldKind.TEXT,
    ("input", "url"): FieldKind.TEXT,
    ("input", "number"): FieldKind.TEXT,
    ("input", "file"): FieldKind.FILE,
    ("input", "checkbox"): FieldKind.BOOLEAN,
    ("textarea", "textarea"): FieldKind.LONG_TEXT,
    ("select", "select-one"): FieldKind.SINGLE_SELECT,
    ("select", "select-multiple"): FieldKind.MULTI_SELECT,
}


def _kind(control: Dict[str, Any]) -> FieldKind:
    tag = (control.get("tag") or "").lower()
    control_type = (control.get("type") or "").lower()

    if tag == "textarea":
        return FieldKind.LONG_TEXT

    if tag == "select":
        return _KIND_BY_CONTROL.get((tag, control_type), FieldKind.SINGLE_SELECT)

    if control_type in ("radiogroup", "combobox", "radio"):
        return FieldKind.SINGLE_SELECT

    # Checkboxes sharing one Ashby field container are one multi-select
    # question; the extension groups them and sends the container's label.
    if control_type == "checkbox-group":
        return FieldKind.MULTI_SELECT

    return _KIND_BY_CONTROL.get((tag, control_type), FieldKind.TEXT)


def _group_controls(controls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse radio inputs sharing a `name` into one question.

    Radio inputs are the only controls that do this: each is a separate DOM
    element, but together they are one question with one answer. Checkboxes
    are left alone — on a real form, "How did you hear about us?" renders as
    independent checkboxes with *different* names, so they genuinely are
    separate fields.
    """
    grouped: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    output: List[Dict[str, Any]] = []

    for control in controls:
        control_type = (control.get("type") or "").lower()
        name = control.get("name") or ""

        if control_type != "radio" or not name:
            output.append(control)
            continue

        # Already one question with its options — the extension groups radios
        # itself and, on Ashby, reads the question text from the field
        # container. Regrouping here would throw that label away and turn the
        # question into an option of itself (measured on a live Notion form:
        # pronouns, sponsorship and all three EEOC groups came back blank).
        if control.get("options"):
            output.append(control)
            continue

        if name not in grouped:
            group = {
                "tag": "input",
                "type": "radio",
                "name": name,
                "id": name,
                # The question text is genuinely absent from these forms, so
                # the group carries no label. That is not a gap to paper over:
                # an unreadable question must not be auto-answered, and the
                # classifier turns a blank label into UNKNOWN for that reason.
                "label": "",
                "required": False,
                "options": [],
            }
            grouped[name] = group
            output.append(group)

        group = grouped[name]
        group["required"] = group["required"] or bool(control.get("required"))
        group["options"].append(
            {"label": control.get("label") or "", "value": control.get("id") or ""}
        )

    return output


def parse_ashby_dom(payload: Dict[str, Any]) -> FormSchema:
    """Normalise a DOM extract posted by the browser extension.

    Args:
        payload: `{posting_id, company, title, apply_url, controls: [...]}`
            where each control is `{tag, type, name, id, required, label,
            options?}` as read from the page

    Returns:
        The normalised form
    """
    fields: List[FormField] = []

    for control in _group_controls(payload.get("controls") or []):
        label = (control.get("label") or "").strip()
        kind = _kind(control)

        options = [
            FieldOption(label=str(o.get("label", "")), value=str(o.get("value", "")))
            for o in control.get("options") or []
        ]

        key = control.get("name") or control.get("id") or ""

        fields.append(
            FormField(
                key=key,
                label=label,
                kind=kind,
                field_class=classify(key, label, kind, option_count=len(options)),
                required=bool(control.get("required")),
                options=options,
            )
        )

    return FormSchema(
        source="ashby_dom",
        ats="ashby",
        posting_id=str(payload.get("posting_id", "")),
        company=payload.get("company"),
        title=payload.get("title"),
        apply_url=payload.get("apply_url"),
        fields=fields,
    )
