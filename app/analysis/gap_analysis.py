"""
Gap analysis logic for comparing resume skills against job requirements.
This is deterministic logic (no LLM calls).
"""
import re
from typing import Dict, List, Set
from app.schemas import ResumeParsed, JobParsed

# A job description's "skills" list routinely contains entries that are not
# skills: "5+ years experience", "Bachelor's degree in Computer Science".
# They can never match a resume skill, so they stayed in missing_required_skills
# permanently — and that list is handed to the project generator as the skills
# the student should build a portfolio project around. The prompt and the
# schema example now steer the model away from this, but the model will not
# comply every time, so the filter below is the deterministic guard.

# "5+ years", "3-5 years", "5 yrs"
_YEARS_OF_EXPERIENCE = re.compile(r"\b\d+\s*\+?\s*(?:-\s*\d+\s*)?(?:years?|yrs?)\b", re.I)

# Academic credentials. Deliberately excludes the ambiguous "BS"/"MS"
# abbreviations, which collide with real skills such as "MS SQL Server".
_CREDENTIAL = re.compile(
    r"\b(?:bachelors?|masters?|phd|ph\.d|doctorate|degree|diploma|certification)\b",
    re.I,
)

# A skill is a short noun phrase ("Amazon Web Services", "Test Driven
# Development"). Anything longer is a sentence-shaped requirement such as
# "Strong understanding of database design".
MAX_SKILL_WORDS = 4


def is_skill_like(entry: str) -> bool:
    """
    Whether a job-description entry names a technology rather than a requirement.

    Args:
        entry: A single entry from a JD's required/preferred skills list

    Returns:
        True if it looks like a skill that could match a resume entry
    """
    text = entry.strip()

    if not text:
        return False

    if _YEARS_OF_EXPERIENCE.search(text):
        return False

    if _CREDENTIAL.search(text):
        return False

    return len(text.split()) <= MAX_SKILL_WORDS


def partition_skills(entries: List[str]) -> "tuple[List[str], List[str]]":
    """
    Split JD entries into genuine skills and non-skill requirements.

    Non-skill entries are returned rather than discarded: they are real
    requirements a candidate may not meet, and dropping them silently would
    hide them from the caller entirely.

    Args:
        entries: Raw entries from a JD skills list

    Returns:
        (skills, non_skill_requirements)
    """
    skills = [e for e in entries if is_skill_like(e)]
    requirements = [e for e in entries if not is_skill_like(e)]
    return skills, requirements

# Aliases map every spelling of a technology onto one canonical token. The
# previous table mapped in the opposite direction ("javascript" -> "js"), which
# meant a resume saying "JavaScript" and a JD saying "JS" only matched because
# both happened to be listed; anything unlisted, like "Javascript ES6", missed.
_SKILL_ALIASES = {
    "js": "javascript",
    "ecmascript": "javascript",
    "ts": "typescript",
    "postgres": "postgresql",
    "psql": "postgresql",
    "reactjs": "react",
    "react js": "react",
    "nodejs": "node",
    "node js": "node",
    "golang": "go",
    "k8s": "kubernetes",
    "amazon web services": "aws",
    "google cloud platform": "gcp",
    "google cloud": "gcp",
    "microsoft azure": "azure",
    "postgres sql": "postgresql",
    "rest api": "rest",
    "rest apis": "rest",
    "restful api": "rest",
    "restful apis": "rest",
    "ci cd": "cicd",
    "continuous integration": "cicd",
    "ml": "machine learning",
    "nlp": "natural language processing",
    "oop": "object oriented programming",
    "tdd": "test driven development",
}

# Trailing version numbers: "Python 3", "Java 17", "Angular 15", "Vue 3.2".
_TRAILING_VERSION = re.compile(r"\s+v?\d+(?:\.\d+)*$")

# Separators that are noise inside a skill name. Deliberately excludes + and #
# so that C++ and C# survive.
_SEPARATORS = re.compile(r"[._/\\,\-]+")

_WHITESPACE = re.compile(r"\s+")


def normalize_skill(skill: str) -> str:
    """
    Reduce a skill name to a canonical token for comparison.

    Comparison was previously exact equality against a seven-entry table, so
    "React.js" did not match "React" and "Python 3" did not match "Python" —
    every such pair became a false gap, and then a recommended project to
    learn something the candidate already had.

    Args:
        skill: A skill name as written in a resume or job description

    Returns:
        Canonical form, or "" for an entry that normalizes to nothing
    """
    text = skill.lower().strip()

    text = _TRAILING_VERSION.sub("", text)
    text = _SEPARATORS.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()

    # Applied after cleanup so "React.js" and "react js" reach the same key.
    return _SKILL_ALIASES.get(text, text)

def skills_match(skill1: str, skill2: str) -> bool:
    """
    Check if two skills match (case-insensitive, normalized).
    """
    return normalize_skill(skill1) == normalize_skill(skill2)


def collect_declared_skills(resume: ResumeParsed) -> List[str]:
    """
    Every skill the resume explicitly claims.

    The skills section alone understates the candidate: technologies listed
    against a project are claimed just as directly, and reading only
    `resume.skills` reported them as missing — then recommended a project to
    learn something they had already built with.

    Args:
        resume: Parsed resume data

    Returns:
        Declared skills from the skills section and from project technologies
    """
    declared = list(resume.skills)

    for project in resume.projects:
        declared.extend(project.technologies)

    return declared


def collect_evidence_text(resume: ResumeParsed) -> str:
    """
    Prose in which a skill may be demonstrated without being claimed.

    Experience bullets and project descriptions mention tools the candidate
    never lists. That is weaker evidence than a declared skill — hence
    `weak_skills` — but it is not nothing.

    Args:
        resume: Parsed resume data

    Returns:
        Lowercased text of every bullet, description and highlight
    """
    parts: List[str] = []

    for experience in resume.experience:
        parts.extend(experience.bullets)

    for project in resume.projects:
        parts.append(project.description)
        parts.extend(project.highlights)

    return " ".join(parts).lower()


def appears_in_text(skill: str, text: str) -> bool:
    """
    Whether `skill` occurs as a whole token in `text`.

    Whole-token matching matters: a substring test reports "R" as present in
    "React" and "Go" in "Django".

    Args:
        skill: A single skill name
        text: Lowercased evidence text

    Returns:
        True if the skill appears as its own token
    """
    normalized = normalize_skill(skill)

    if not normalized:
        return False

    return re.search(rf"(?<![\w+#]){re.escape(normalized)}(?![\w+#])", text) is not None

def find_matching_skills(resume_skills: List[str], job_skills: List[str]) -> List[str]:
    """
    Find skills from resume that match job requirements.
    Returns the original job skill names (for consistency).
    """
    matches = []
    resume_normalized = {normalize_skill(s): s for s in resume_skills}
    
    for job_skill in job_skills:
        job_normalized = normalize_skill(job_skill)
        if job_normalized in resume_normalized:
            matches.append(job_skill)
    
    return matches

def compute_gap(resume: ResumeParsed, job: JobParsed) -> Dict:
    """
    Compute skill gap analysis between resume and job description.
    
    Args:
        resume: Parsed resume data
        job: Parsed job description data
    
    Returns:
        Dictionary with:
        - overlapping_skills: Skills the candidate has that match requirements
        - missing_required_skills: Required skills the candidate lacks
        - missing_preferred_skills: Preferred skills the candidate lacks
        - non_skill_requirements: JD entries that are requirements rather than
          skills (degrees, years of experience). Reported so they are not lost,
          but excluded from the missing-skills lists that drive project
          generation
        - weak_skills: Skills the resume demonstrates in prose but never
          claims in a skills or technologies list. Real evidence, weaker than
          a declared skill, so they are neither counted as overlapping nor
          reported as missing
    """
    # Everything the resume claims outright, including project technologies.
    resume_skills = collect_declared_skills(resume)

    # Prose where a skill may be shown without being claimed.
    evidence = collect_evidence_text(resume)

    # Keep only entries that name a technology; a degree or a duration cannot
    # be matched against a resume skill, and must not become a project idea.
    required_skills, required_requirements = partition_skills(job.required_skills)
    preferred_skills, preferred_requirements = partition_skills(job.preferred_skills)

    # Find overlapping skills
    overlapping_required = find_matching_skills(resume_skills, required_skills)
    overlapping_preferred = find_matching_skills(resume_skills, preferred_skills)

    def _undeclared(skills: List[str]) -> List[str]:
        return [
            skill for skill in skills
            if not any(skills_match(skill, rs) for rs in resume_skills)
        ]

    # A skill the candidate never lists but demonstrably used is not a gap to
    # close with a new project — it is a gap in how the resume presents them.
    weak = [
        skill for skill in _undeclared(required_skills) + _undeclared(preferred_skills)
        if appears_in_text(skill, evidence)
    ]
    weak_normalized = {normalize_skill(s) for s in weak}

    def _missing(skills: List[str]) -> List[str]:
        return [
            skill for skill in _undeclared(skills)
            if normalize_skill(skill) not in weak_normalized
        ]

    missing_required = _missing(required_skills)
    missing_preferred = _missing(preferred_skills)

    # Combine all overlapping skills. Sorted, not just deduplicated: set
    # iteration order for strings varies between processes, and this result is
    # persisted and shown to the user, so an unsorted list made identical
    # inputs produce different output on each run.
    all_overlapping = sorted(set(overlapping_required + overlapping_preferred))

    return {
        "overlapping_skills": all_overlapping,
        "missing_required_skills": missing_required,
        "missing_preferred_skills": missing_preferred,
        "non_skill_requirements": required_requirements + preferred_requirements,
        "weak_skills": sorted(set(weak)),
    }