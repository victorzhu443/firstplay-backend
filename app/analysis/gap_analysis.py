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

def normalize_skill(skill: str) -> str:
    """
    Normalize a skill name for comparison.
    Converts to lowercase and removes common variations.
    """
    skill = skill.lower().strip()
    
    # Common normalizations
    replacements = {
        'javascript': 'js',
        'typescript': 'ts',
        'postgresql': 'postgres',
        'reactjs': 'react',
        'react.js': 'react',
        'node.js': 'node',
        'nodejs': 'node',
    }
    
    for old, new in replacements.items():
        if skill == old:
            return new
    
    return skill

def skills_match(skill1: str, skill2: str) -> bool:
    """
    Check if two skills match (case-insensitive, normalized).
    """
    return normalize_skill(skill1) == normalize_skill(skill2)

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
        - weak_skills: Skills mentioned but possibly not strong (placeholder for now)
    """
    # Get all skills from resume
    resume_skills = resume.skills

    # Keep only entries that name a technology; a degree or a duration cannot
    # be matched against a resume skill, and must not become a project idea.
    required_skills, required_requirements = partition_skills(job.required_skills)
    preferred_skills, preferred_requirements = partition_skills(job.preferred_skills)

    # Find overlapping skills
    overlapping_required = find_matching_skills(resume_skills, required_skills)
    overlapping_preferred = find_matching_skills(resume_skills, preferred_skills)

    # Find missing required skills
    missing_required = [
        skill for skill in required_skills
        if not any(skills_match(skill, rs) for rs in resume_skills)
    ]

    # Find missing preferred skills
    missing_preferred = [
        skill for skill in preferred_skills
        if not any(skills_match(skill, rs) for rs in resume_skills)
    ]

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
        "weak_skills": []  # Placeholder - could be enhanced with more sophisticated analysis
    }