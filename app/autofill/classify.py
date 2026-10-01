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
    "gpa undergraduate": "gpa",            # Freeform: "GPA (Undergraduate)"
    "state": "state_of_residence",
    "state province": "state_of_residence",
    "state province region": "state_of_residence",  # TransMarket: "State/Province/Region:"
    "province state": "state_of_residence",         # Visier (Canada): "Province/State"
    "state if n a select other": "state_of_residence",   # Chicago Trading
    "what is your preferred first name": "preferred_first_name",  # NISC
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
    "github or personal website": "github",      # Sierra (Ashby): one field, GitHub first
    "linkedin github personal website or portfolio": "links_combined",
    "website portfolio": "links_combined",
    # identity extras
    # DRW 7957243 asks the legal name as two custom questions beside the
    # standard ones; the stored name is the legal one.
    "legal first name": "first_name",
    "legal last name": "last_name",
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
    # Ashby custom text fields that restate a core fact under a per-posting
    # UUID key (coverage baseline 2026-09-30, 413 Ashby forms): "Current
    # Location" x37, "Current Company" x22, "Please provide your LinkedIn
    # profile" x10, "Please provide your phone number" x8, "School" x8,
    # "University" x8, "Degree" x8, "Location" x7, "Last Name" x4. Each one
    # went to the model gate (or review) for a value the profile holds.
    "current location": "current_location",
    "location": "current_location",
    "current city": "city_of_residence",
    "current company": "current_employer",
    "current employer": "current_employer",
    "company": "current_employer",
    "current title": "current_job_title",
    "current job title": "current_job_title",
    "please provide your linkedin profile": "linkedin",
    "linkedin profile link": "linkedin",
    "linkedin link": "linkedin",
    "please provide your phone number": "phone",
    "mobile phone": "phone",
    "mobile number": "phone",
    "cell phone": "phone",
    "mobile": "phone",
    "email address": "email",
    "e mail": "email",
    "first name": "first_name",
    "last name": "last_name",
    "surname": "last_name",
    "full name": "full_name",
    "name": "full_name",
    "preferred name nickname": "preferred_first_name",
    "nickname": "preferred_first_name",
    "school": "university",
    "school name": "university",
    "university": "university",
    "university name": "university",
    "college": "university",
    "college university": "university",
    "degree": "degree",
    "twitter": "twitter",
    "twitter handle": "twitter",
    "x twitter": "twitter",
    "twitter x": "twitter",
    "portfolio website link": "website",
    "portfolio link": "website",
    "portfolio website url": "website",
    "portfolio website": "website",
    "website url": "website",
    "personal website url": "website",
    "address line 2": "street_address_2",
    "address 2": "street_address_2",
    "apt suite": "street_address_2",
    "apartment suite": "street_address_2",
}

#: Memory keys whose absence on an *optional* field means "leave it blank",
#: not "ask". A second address line, a Twitter handle or a second website is
#: something most applicants do not have; recording nothing is the answer.
OPTIONAL_BLANK_KEYS = frozenset({
    "street_address_2", "twitter", "website_other", "alternate_email",
    "preferred_last_name",
})

#: Follow-up fields that only apply after a particular answer to the question
#: before them: "If other, please specify" (x24 in the Greenhouse corpus),
#: "Please specify" (x24), "If other, please explain" (x15), "Please provide
#: additional detail if appropriate." (x19), "Other" (x5 on Ashby). When the
#: form marks one optional and the preceding answer did not trigger it, blank
#: is the answer.
FOLLOW_UP_LABEL = re.compile(
    r"^(if (other|yes|no|so|applicable|not listed|you (selected|chose|answered|were referred|have)|"
    r"referred)\b"
    r"|please (specify|explain|elaborate|describe|provide (additional|more|further))\b"
    r"|other( please specify)?$"
    r"|additional (detail|information|comments?|context)s?\b"
    r"|comments?$)",
    re.I,
)

#: The answer to the preceding question that makes an "other" follow-up apply.
FOLLOW_UP_ON_OTHER = re.compile(r"^(if )?other\b|please (specify|explain)\b|^other$", re.I)


#: Core facts asked in a sentence rather than a name, so no exact alias can
#: hit: "Please include your LinkedIn profile" (Antares), "Your Phone Number"
#: and "Your LinkedIn Profile" (Exegy), "Your GitHub" (Composio), "GitHub or
#: Portfolio URL" (Etched) — all from the first twelve held-out Ashby boards
#: of R48-1. Text controls only; checked after the exact aliases.
CORE_LABEL_PATTERNS = (
    (re.compile(r"^(your |please (provide|include|share|enter|add) (your )?|what is your |link to your )?"
                r"(linkedin|linked in)( profile| url| link| profile url| profile link| page)?( url| link)?$", re.I), "linkedin"),
    (re.compile(r"^(your |please (provide|include|share|enter|add) (your )?|what is your |link to your )?"
                r"github( profile| url| link| profile url| handle| username| account)?( url| link)?$", re.I), "github"),
    (re.compile(r"^(your |please (provide|include|share|enter|add) (your )?|what is your |best )?"
                r"(phone|mobile|cell|telephone|contact)( phone)? ?(number|no)?$", re.I), "phone"),
    (re.compile(r"^(your |please (provide|include|share|enter) (your )?|what is your )?e ?mail( address)?$", re.I), "email"),
    (re.compile(r"^(your |please (provide|include|share|enter) (your )?)?(first|given) name$", re.I), "first_name"),
    (re.compile(r"^(your |please (provide|include|share|enter) (your )?)?(last|family) name$", re.I), "last_name"),
    (re.compile(r"^(your |please (provide|include|share|enter) (your )?)?(full )?name$", re.I), "full_name"),
    (re.compile(r"^(your |please (provide|include|share|enter) (your )?)?(resume|cv|resume cv)$", re.I), "resume_file"),
    (re.compile(r"^(your |please (provide|include|share|enter) (your )?)?preferred (first )?name$", re.I), "preferred_first_name"),
    (re.compile(r"^(github|linkedin|portfolio|website|twitter)(( or | |/|, ?)(github|linkedin|portfolio|website|twitter|etc|url|link|links))+$", re.I), "links_combined"),
    (re.compile(r"^(your |please (provide|include|share) (your )?)?(personal )?(website|portfolio)( url| link| website| site)?$", re.I), "website"),
)


def core_pattern_key_for(label: str, kind: Optional[FieldKind]) -> Optional[str]:
    """A core memory key for a sentence-shaped label on a text control."""
    if kind not in (None, FieldKind.TEXT):
        return None
    normalized = normalize_label(label)
    for pattern, key in CORE_LABEL_PATTERNS:
        if pattern.match(normalized):
            return key
    return None


def is_follow_up(label: str) -> bool:
    """Whether a label is a follow-up to the question before it."""
    return bool(FOLLOW_UP_LABEL.search(normalize_label(label)))


#: Free-text questions that are essays even though the ATS gives them a
#: single-line control: Ashby's String type carries "What excites you about
#: the opportunity to join Talos?" and "What is one project you're really
#: proud of?". These are the applicant's own words, not a replayable fact.
NARRATIVE_LABEL = re.compile(
    r"^(why (do|are|would|did) you\b|why [a-z0-9&.' -]{1,30}\?$|what excites you\b|what interests you\b"
    r"|tell us (about|why|a bit)\b|describe (a|an|the|your|one)\b|what is one (project|thing)\b"
    r"|what are you most proud\b|what (would|do) you (bring|hope|want|like) to\b|cover letter\b"
    r"|anything else\b|is there anything else\b|what makes you\b|record a video\b"
    r"|additional (information|comments?|notes?)( or a note)?( you('d| would) like to share)?$"
    r"|additional information or a note\b)",
    re.I,
)

#: Standing consents. Each regex names a *kind* of agreement the applicant can
#: decide once; the profile's `consents` section holds that decision and the
#: binder replays it. Anything that matches no bucket stays human — the AI
#: policy attestation and arbitration agreements never enter this table.
#: Order matters: an SMS opt-in that mentions the privacy policy is an SMS
#: opt-in.
CONSENT_BUCKETS = (
    (re.compile(r"\b(sms|text messag|texting|text message|whatsapp|recruiting (sms|texts))\b", re.I),
     "sms_messages"),
    (re.compile(r"\b(marketing|talent (community|network|pool)|future (career )?opportunit|"
                r"newsletter|promotional|recruitment marketing|occasional (messages|emails))\b", re.I),
     "marketing_communications"),
    (re.compile(r"\b(record(ing|ed)?\s+(the\s+)?(interview|conversation)|brighthire|metaview)\b", re.I),
     "interview_recording"),
    (re.compile(r"\b(background check|background screening|verify the accuracy|"
                r"authorize .{0,30}(contact|verify))\b", re.I),
     "background_check"),
    (re.compile(r"\b(privacy (statement|notice|policy)|data (protection|processing|privacy)|"
                r"personal data|candidate (privacy|data)|gdpr|process(ing)? (of )?(my|your) (personal )?"
                r"(data|information))\b", re.I),
     "privacy_notice"),
    (re.compile(r"\b(certify|attest|confirm|declare|acknowledge) .{0,60}\b(true|accurate|complete|"
                r"correct|truthful)\b|\b(true|accurate) and (complete|correct)\b|\bbest of my knowledge\b", re.I),
     "truthful_certification"),
    (re.compile(r"\b(terms (and|&) conditions|terms of (use|service)|code of conduct|"
                r"interview (conduct|code)|confidentiality agreement|candidate agreement|"
                r"application statement|pre.?employment requirements)\b", re.I),
     "terms_and_conditions"),
)


def consent_key_for(label: str) -> Optional[str]:
    """Which standing consent, if any, a consent-class field asks for."""
    text = label or ""
    if _AI_POLICY.search(normalize_label(text)) or re.search(r"\barbitrat", text, re.I):
        return None
    for pattern, key in CONSENT_BUCKETS:
        if pattern.search(text):
            return key
    return None

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
    # Maven Securities 8043552: "If you require any support or adjustments
    # during the recruitment process…" is the UK wording. Unmatched, it was
    # SCREENING and the model gate answered it "No" from nothing (§40).
    (re.compile(r"\b(reasonable accommodations?|accommodations? (for|during|request)|"
                r"support or adjustments|reasonable adjustments|adjustments (during|to) the)\b", re.I),
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
    re.compile(r"\b(reasonable accommodations?|accommodations? (for|during|request)"
               r"|support or adjustments|reasonable adjustments|adjustments (during|to) the"
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
    if kind == FieldKind.TEXT and option_count == 0 and core_pattern_key_for(label, kind):
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

    # A single-line control can still ask for an essay (Ashby's String type
    # does). Judged by the opening words only, so "Describe your work
    # authorization status" with options is not caught: options exclude it.
    if kind == FieldKind.TEXT and option_count == 0 and NARRATIVE_LABEL.search(normalized):
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

    return CORE_LABEL_ALIASES.get(normalize_label(label)) or core_pattern_key_for(label, kind)
