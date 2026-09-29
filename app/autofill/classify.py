"""
Shared field classification, used by every ATS adapter.

Everything here is deterministic, and there is deliberately very little of it.
An earlier version carried two pattern-matching regexes that tried to spot
legally sensitive and consent questions by their wording. Measured against
2,429 fields from 145 live Greenhouse postings, **they decided nothing at
all** — every sensitive field had already been caught by structure. They were
removed, and looking for the equivalent structure in Ashby is what found this:

    d5e6e981-...__systemfield_eeoc_race
    d5e6e981-...__systemfield_eeoc_veteran_status

Both vendors mark protected-characteristic questions in the data. Greenhouse
puts them in a separate `compliance` block; Ashby names them with an
`_systemfield_eeoc` prefix. Reading the marker is exact. Matching "veteran"
against a label is a guess — and on a real Ashby form it is a guess that
*loses*, because the radio inputs are labelled with the answer ("Hispanic or
Latino"), not the question, so there is no question text to match against.

The one asymmetry that governs every rule here: a rule may only ever make a
field *less* auto-fillable, never more. Misreading a screening question as
sensitive costs one manual answer. The reverse puts a guess on a protected
characteristic.
"""
import re
from typing import Optional

from app.autofill.schema import FieldClass, FieldKind

#: Marks a protected-characteristic question in an Ashby field name. Ashby's
#: own convention, not ours.
ASHBY_EEOC_MARKER = "_systemfield_eeoc"

#: ATS field names that identify a core fact, mapped to the canonical memory
#: key. These are stable across postings — unlike custom question names, which
#: are allocated per posting and carry no meaning.
#:
#: Note Ashby collects one `Name` where Greenhouse splits first and last. The
#: memory layer therefore has to hold the parts and compose the whole, not the
#: other way round: you can build "Ada Lovelace" from two fields, but splitting
#: a full name back into two is guesswork the moment anyone has two surnames.
CORE_FIELD_NAMES = {
    # Greenhouse
    "first_name": "first_name",
    "last_name": "last_name",
    "email": "email",
    "phone": "phone",
    # Ashby
    "_systemfield_name": "full_name",
    "_systemfield_email": "email",
    "_systemfield_phone": "phone",
    # The Location autocomplete carries no name or id of its own; the
    # extension keys it by the field id its container label points at.
    "_systemfield_location": "current_location",
    # Greenhouse's candidate-location block (`location_questions`): a geocoder
    # typeahead whose chosen suggestion also fills hidden longitude/latitude.
    "location": "current_location",
    "country": "country_of_residence",
    # Greenhouse's education block. Not in the API's `questions`; the payload
    # says only `education: education_required|education_optional`, and the
    # form renders these names (measured on Duolingo, 2026-09-27). Degree and
    # discipline options come from the board's public /education endpoints.
    "educations[0].school_name_id": "university",
    "educations[0].degree_id": "degree_type",
    "educations[0].discipline_id": "discipline",
    "educations[0].end_date.month": "graduation_month",
    "educations[0].end_date.year": "graduation_year",
    "educations[0].start_date.month": "education_start_month",
    "educations[0].start_date.year": "education_start_year",
}

#: Document uploads and paste-alternatives. Split from CORE because they are
#: filled with a file or a stored document, never with a typed fact.
#:
#: `resume_text` matters: Greenhouse offers a textarea alternative to the
#: resume upload, and without naming it here it would classify as a narrative
#: field by shape and be handed to an essay writer.
DOCUMENT_FIELD_NAMES = {
    "resume": "resume_file",
    "resume_text": "resume_text",
    "cover_letter": "cover_letter",
    "cover_letter_text": "cover_letter",
    "_systemfield_resume": "resume_file",
}

#: Custom questions whose label is unambiguous, mapped to a memory key.
#:
#: Every entry is a promotion to "fill without asking", so this table is the
#: riskiest thing in the file and each line has to earn its place by frequency
#: *and* by being impossible to misread. The counts are occurrences across 148
#: live Greenhouse postings.
#:
#: Matched against `normalize_label`, so trailing punctuation is already gone —
#: "LinkedIn Profile:" recurs 29 times and used to miss on the colon alone.
CORE_LABEL_ALIASES = {
    "alternate email": "alternate_email",
    "secondary email": "alternate_email",
    # profile links
    "linkedin": "linkedin",
    "linkedin profile": "linkedin",           # x29 (was missing: trailing colon)
    "linkedin url": "linkedin",
    "linkedin profile url": "linkedin",
    "github": "github",
    "github profile": "github",
    "github url": "github",
    "github link": "github",                  # Notion (Ashby), 2026-09-27
    "phone": "phone",                         # Ashby's label; the key is a UUID
    "phone number": "phone",
    # Custom address questions (Relay, Gallup, round 3): derivable pieces of
    # the stored location, and a street address the profile may hold.
    "state": "state_of_residence",
    "state province": "state_of_residence",
    "state/province": "state_of_residence",
    "country": "country_of_residence",
    "country of residence": "country_of_residence",
    "city": "city_of_residence",
    "address": "street_address",
    "address line 1": "street_address",
    "street address": "street_address",
    "zip": "postal_code",
    "zip code": "postal_code",
    "zip postal code": "postal_code",
    "postal code": "postal_code",
    "website": "website",
    "websites": "website",                    # x6  "Website(s)"
    "website s": "website",                   # "Website(s)" after punctuation strip
    "personal website": "website",
    "portfolio": "website",
    # A distinct key, not an alias of `website`. Figma's form carries both
    # "Website" and "Other Website"; mapping them together filled the same URL
    # into both. Memory usually has nothing here, and an empty memory value
    # means the field is left blank rather than duplicated.
    "other website": "website_other",
    "other websites": "website_other",
    # A single field asking for any of several links (x8 in the intern corpus):
    # "LinkedIn Profile, Github, Personal Website, or Portfolio". Mapped to its
    # own key rather than to `linkedin`, because the applicant may want to put
    # something different in a combined field than in a LinkedIn-only one.
    "linkedin profile github personal website or portfolio": "links_combined",
    "linkedin github personal website or portfolio": "links_combined",
    "website portfolio": "links_combined",
    # identity extras
    "preferred first name": "preferred_first_name",   # x72, the single largest
    "preferred name": "preferred_first_name",
    # Asked alongside the first-name variant. Its own key rather than an alias
    # of `last_name`, because someone whose preferred surname differs needs to
    # say so — and if it is the same, setup records it once either way.
    "preferred last name": "preferred_last_name",
    "pronouns": "pronouns",                           # x29
    "please share your gender pronouns": "pronouns",  # x13, Lyft's wording
    "gender pronouns": "pronouns",
    "your pronouns": "pronouns",
    "preferred pronouns": "pronouns",
    # how the applicant found the role — a stored preference, not a judgement
    "how did you hear about this job": "heard_about",  # x78, the most frequent
    "how did you hear about us": "heard_about",
    "how did you hear about this role": "heard_about",
}

#: Protected-characteristic fields, mapped to the canonical memory key.
#:
#: Greenhouse names these stably — `veteran_status`, `race`, `gender`,
#: `disability_status` — and Ashby suffixes them onto a per-posting UUID
#: (`..._systemfield_eeoc_race`), so both are matched below.
#:
#: Wiring these up is what lets a protected answer be *replayed*. Without it
#: they were recognised as LEGAL and then sent to review on every single
#: application — 212 fields across 149 postings, which is exactly the chore the
#: product exists to remove. Replaying the applicant's own stated answer is not
#: inference; `ALLOWED_FILL_SOURCES` still bars any model from these.
PROTECTED_FIELD_NAMES = {
    "veteran_status": "veteran_status",
    "disability": "disability_status",
    "race": "race_ethnicity",
    "gender": "gender",
    "disability_status": "disability_status",
}

#: Ashby's suffix form, matched against the end of the field name.
PROTECTED_FIELD_SUFFIXES = {
    "_systemfield_eeoc_veteran_status": "veteran_status",
    "_systemfield_eeoc_race": "race_ethnicity",
    "_systemfield_eeoc_gender": "gender",
    "_systemfield_eeoc_disability_status": "disability_status",
}


#: Label patterns for the memory-only questions, mapped to their profile key.
#: Matched on the label because these arrive as custom questions with per-posting
#: names, so there is no stable key to look up.
MEMORY_ONLY_LABEL_KEYS = (
    # Order matters: "Can you perform these essential functions of the job with
    # reasonable accommodation?" mentions both. It asks about ability, and with
    # accommodation first it was answered from accommodation_needs ("None"),
    # which rendered as "No" — the inverted answer, on a live Lyft posting.
    (re.compile(r"\bessential functions\b", re.I), "essential_functions"),
    (re.compile(r"\b(reasonable accommodation|accommodation (for|during|request))\b", re.I),
     "accommodation_needs"),
    (re.compile(r"\bclose relative\b", re.I), "close_relative_official"),
    (re.compile(r"\bgovernment official\b", re.I), "government_official"),
    (re.compile(r"\b(personal/familial|familial) relationship", re.I),
     "familial_relationship"),
)


#: Document requests whose label is a sentence rather than a name, so an exact
#: alias can never match: "Please upload your academic record document (e.g.
#: University transcript)" alongside the bare "Undergraduate Transcript". x8
#: across the intern corpus in three wordings.
DOCUMENT_LABEL_PATTERNS = (
    (re.compile(r"\b(transcript|academic record|academic transcript)\b", re.I),
     "transcript_file"),
)


def document_key_for(label: str) -> Optional[str]:
    """Profile key for a document request, matched on its wording."""
    for pattern, key in DOCUMENT_LABEL_PATTERNS:
        if pattern.search(label or ""):
            return key

    return None


def memory_only_key_for(label: str) -> Optional[str]:
    """Profile key for a memory-only question, matched on its wording."""
    for pattern, key in MEMORY_ONLY_LABEL_KEYS:
        if pattern.search(label or ""):
            return key

    return None


#: Greenhouse's "Voluntary Self Identification" block (`demographic_questions`)
#: keys its questions by number, so the stored answer is chosen by wording.
#: Only consulted for keys in that block — the same words elsewhere ("gender
#: pronouns") are not protected characteristics.
DEMOGRAPHIC_KEY_PREFIX = "demographic_answers."

PROTECTED_LABEL_PATTERNS = (
    (re.compile(r"\bsexual orientation\b", re.I), "sexual_orientation"),
    (re.compile(r"\btransgender\b", re.I), "transgender"),
    (re.compile(r"\bgender\b", re.I), "gender"),
    (re.compile(r"\b(racial|ethnic|race|ethnicity)\b", re.I), "race_ethnicity"),
    (re.compile(r"\bdisabilit", re.I), "disability_status"),
    (re.compile(r"\bveteran\b", re.I), "veteran_status"),
)


def protected_key_for(key: str, label: str = "") -> Optional[str]:
    """Canonical memory key for a protected-characteristic field, if it is one."""
    if key in PROTECTED_FIELD_NAMES:
        return PROTECTED_FIELD_NAMES[key]

    for suffix, memory_key in PROTECTED_FIELD_SUFFIXES.items():
        if (key or "").endswith(suffix):
            return memory_key

    if (key or "").startswith(DEMOGRAPHIC_KEY_PREFIX):
        for pattern, memory_key in PROTECTED_LABEL_PATTERNS:
            if pattern.search(label or ""):
                return memory_key

    return None


#: Questions a model must never answer, but the applicant answers **once** and
#: replays forever — the same treatment as veteran status. Routed to LEGAL,
#: which is MEMORY-only in `ALLOWED_FILL_SOURCES`.
#:
#: The first cut of this file made these permanently human, which was wrong and
#: cost ~70 fields of coverage across 57 intern postings. "Are you a government
#: official?" is a stable fact about the applicant; nothing about it has to be
#: re-answered per application. What must never happen is a model *inferring*
#: it, and LEGAL already forbids that.
_MEMORY_ONLY = (
    # ADA / accommodation. Free text or yes/no, and the applicant's own
    # statement either way — most people store "None" and never see it again.
    re.compile(r"\b(reasonable accommodation|accommodation (for|during|request)"
               r"|essential functions)\b", re.I),
    # Conflict-of-interest and political-exposure screens (~30 instances across
    # the fintech boards). Stable facts about relatives and holdings.
    re.compile(r"\bgovernment official\b", re.I),
    re.compile(r"\b(personal/familial|familial) relationship", re.I),
    re.compile(r"\bclose relative\b", re.I),
    re.compile(r"\bpolitically exposed\b", re.I),
    # "Please confirm whether any of the below applies to you" and its variants.
    # A disclosure of which facts apply, where a wrong selection is a
    # misrepresentation. Caught by wording because no vendor marks it.
    re.compile(r"\b(any|which) of the (below|following|above)\b.{0,24}\bappl(y|ies)\b",
               re.I),
    re.compile(r"\bselect all that apply\b.{0,40}\b(disclos|relationship|conflict)", re.I),
)

#: Genuine per-application attestations. These stay human, because the act of
#: certifying is *made* at submission time rather than reporting a fact that
#: already exists — the same distinction that keeps arbitration agreements
#: manual. "I certify that the facts set forth in this Application are true"
#: (x13) arrives as a plain text field, so the single-option-select rule never
#: sees it.
_ATTESTATION = (
    re.compile(r"\bi (certify|attest|declare)\b", re.I),
    re.compile(r"\bcertif(y|ication) that the (facts|information)\b", re.I),
    re.compile(r"\bi (understand|acknowledge) that\b", re.I),
)


#: Employer attestations about the applicant's own use of AI in the application.
#: Anthropic's "AI Policy for Application" appears on 27 of 148 postings.
#:
#: Routed to CONSENT — human-only — deliberately. This is the one field where an
#: autofill tool would be attesting to its own involvement, and it is an
#: attestation the applicant makes rather than a fact about them that already
#: exists. Costs one click per application.
#: Consent-shaped questions that are decisions rather than facts, found in the
#: intern corpus and initially unclassified. Each grants something — a recording,
#: a marketing channel, acceptance of terms — so none is a fact to replay.
_DECISIONS_NOT_FACTS = (
    # "To ensure we can focus fully on our conversation, we use Brighthire to
    # record interviews" (x4). Consent to being recorded.
    re.compile(r"\b(record(ing|ed)?\s+(the\s+)?(interview|conversation)|brighthire"
               r"|metaview|otter\.ai)\b", re.I),
    # "Do you opt-in to receive WhatsApp messages from Stripe Recruiting" (x2).
    re.compile(r"\bopt[-\s]?in\b|\b(receive|consent to).{0,20}\b(messages|texts|sms"
               r"|whatsapp|marketing)\b", re.I),
    # "[Compensation] Do you accept the listed salary range for this role" (x2).
    re.compile(r"\b(accept|agree to).{0,24}\b(salary|compensation|pay)\s*(range|band)?\b",
               re.I),
)

_AI_POLICY = re.compile(
    r"\bai\b.{0,24}\b(polic|guidance|guideline|disclosure)"
    # Intern-corpus phrasings: "I understand that Coinbase may use AI tools to
    # assist in the review" (x5) and "Which of the following best describes how
    # you use AI tools" (x5).
    r"|\b(use|using|usage of) ai tools?\b"
    r"|\bai tools? (to assist|in (the )?review)\b",
    re.I,
)

#: Trailing label noise: colons, required markers, stray punctuation.
_LABEL_TRAILING = re.compile(r"[\s:;,.*?!\-–—]+$")

#: Punctuation that is noise *inside* a label. Parentheses are flattened so
#: "Website(s)" reaches the alias table, but the content is kept: "(Optional)
#: Personal Preferences" is a real label and dropping the parenthetical would
#: change which question it is.
_LABEL_INNER = re.compile(r"[(),\[\]/\\]+")

_WHITESPACE = re.compile(r"\s+")


def normalize_label(label: Optional[str]) -> str:
    """Reduce a field label to a canonical form for table lookup.

    Mirrors `app.analysis.gap_analysis.normalize_skill`, and for the same
    reason: comparison against a literal string means every spelling variant
    is a miss, and a miss here sends a question the applicant already answered
    off to a model.

    Args:
        label: The label as the ATS wrote it

    Returns:
        Lowercased, punctuation-stripped, whitespace-collapsed form
    """
    text = (label or "").strip().lower()
    text = _LABEL_TRAILING.sub("", text)
    text = _LABEL_INNER.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()

    return text


def is_structurally_legal(key: str, *, from_compliance_block: bool = False) -> bool:
    """Whether the ATS itself marked this field as a protected characteristic.

    Args:
        key: The ATS field name
        from_compliance_block: True when the field arrived in a block the ATS
            tagged as compliance/EEOC, which is how Greenhouse marks them

    Returns:
        True when the vendor's own data says this is an EEOC question
    """
    return (from_compliance_block or ASHBY_EEOC_MARKER in (key or "")
            or (key or "").startswith(DEMOGRAPHIC_KEY_PREFIX))


def classify(
    key: str,
    label: str,
    kind: FieldKind,
    *,
    option_count: int = 0,
    from_compliance_block: bool = False,
) -> FieldClass:
    """Decide what kind of question this is.

    Args:
        key: The ATS field name
        label: The question as the applicant reads it
        kind: Normalised control shape
        option_count: Number of selectable options, for the single-option
            agreement case below
        from_compliance_block: Passed through to `is_structurally_legal`

    Returns:
        The field's class
    """
    normalized = normalize_label(label)

    # Structure first, and it is the only sensitive-field rule there is.
    if is_structurally_legal(key, from_compliance_block=from_compliance_block):
        return FieldClass.LEGAL

    if key in DOCUMENT_FIELD_NAMES:
        return FieldClass.DOCUMENT

    if key in CORE_FIELD_NAMES:
        return FieldClass.CORE

    # A question whose text cannot be read cannot be answered. Real Ashby
    # forms carry bare file inputs, unlabelled checkboxes, a reCAPTCHA
    # textarea, and a pronoun radio group with no legend — none of which a
    # filler has any business guessing at.
    if not normalized:
        return FieldClass.UNKNOWN

    # Attestations first: made at submission, never replayed.
    if any(pattern.search(normalized) for pattern in _ATTESTATION):
        return FieldClass.CONSENT

    # Then facts the applicant states once. LEGAL keeps every model out while
    # still allowing their own stored answer through.
    if any(pattern.search(normalized) for pattern in _MEMORY_ONLY):
        return FieldClass.LEGAL

    if _AI_POLICY.search(normalized):
        return FieldClass.CONSENT

    if any(pattern.search(normalized) for pattern in _DECISIONS_NOT_FACTS):
        return FieldClass.CONSENT

    # A required single-select offering exactly one option is not a question,
    # it is a checkbox with legal text — Anthropic's "Agreement to Arbitrate"
    # is modelled exactly this way. Ticking it on someone's behalf is signing,
    # not autofill.
    if kind == FieldKind.SINGLE_SELECT and option_count == 1:
        return FieldClass.CONSENT

    if kind == FieldKind.FILE:
        return FieldClass.DOCUMENT

    # A checkbox never takes a text fact. "LinkedIn" as a checkbox label means
    # "I heard about you via LinkedIn", not "here is my LinkedIn URL" — the
    # alias table aimed a URL at a checkbox on a live Ashby form.
    if normalized in CORE_LABEL_ALIASES and kind != FieldKind.BOOLEAN:
        return FieldClass.CORE

    # Multi-selects were once classified UNKNOWN wholesale, because 46 of the 52
    # in the general corpus were conflict-of-interest disclosures where a wrong
    # selection is a misrepresentation. That was too blunt: Stripe asks "select
    # the cohort dates that work best for you" as a multi-select, which is the
    # only way any form in the corpus lets an applicant say "any term works" —
    # and the blanket rule made that unanswerable.
    #
    # The disclosures are now caught by name above (_MEMORY_ONLY covers familial
    # relationships and government officials, _DECISIONS_NOT_FACTS covers
    # consent), so a multi-select can be routed like anything else. The binder
    # then fills one only when a theme explicitly handles multiple values —
    # an allow-list, so anything unrecognised still goes to review.

    # Checked after the alias table, so a recognised "LinkedIn" stays a stored
    # fact rather than becoming something an essay writer is asked to compose.
    if kind == FieldKind.LONG_TEXT:
        return FieldClass.NARRATIVE

    return FieldClass.SCREENING


def memory_key_for(key: str, label: str, kind: Optional[FieldKind] = None) -> Optional[str]:
    """Canonical memory key for a field answerable from a stored fact.

    Returns None when no stored fact answers this field directly, which is the
    signal to route it to the theme router instead.

    Args:
        key: The ATS field name
        label: The question as the applicant reads it

    Returns:
        A memory key, or None
    """
    if key in CORE_FIELD_NAMES:
        return CORE_FIELD_NAMES[key]

    if key in DOCUMENT_FIELD_NAMES:
        return DOCUMENT_FIELD_NAMES[key]

    protected = protected_key_for(key, label)
    if protected:
        return protected

    memory_only = memory_only_key_for(label)
    if memory_only:
        return memory_only

    document = document_key_for(label)
    if document:
        return document

    if kind == FieldKind.BOOLEAN:
        return None

    return CORE_LABEL_ALIASES.get(normalize_label(label))
