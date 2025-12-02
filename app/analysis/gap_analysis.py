"""
Gap analysis logic for comparing resume skills against job requirements.
This is deterministic logic (no LLM calls).
"""
from typing import Dict, List, Set
from app.schemas import ResumeParsed, JobParsed

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
        - missing_required: Required skills the candidate lacks
        - missing_preferred: Preferred skills the candidate lacks
        - weak_skills: Skills mentioned but possibly not strong (placeholder for now)
    """
    # Get all skills from resume
    resume_skills = resume.skills
    
    # Find overlapping skills
    overlapping_required = find_matching_skills(resume_skills, job.required_skills)
    overlapping_preferred = find_matching_skills(resume_skills, job.preferred_skills)
    
    # Find missing required skills
    missing_required = [
        skill for skill in job.required_skills
        if not any(skills_match(skill, rs) for rs in resume_skills)
    ]
    
    # Find missing preferred skills
    missing_preferred = [
        skill for skill in job.preferred_skills
        if not any(skills_match(skill, rs) for rs in resume_skills)
    ]
    
    # Combine all overlapping skills
    all_overlapping = list(set(overlapping_required + overlapping_preferred))
    
    return {
        "overlapping_skills": all_overlapping,
        "missing_required_skills": missing_required,
        "missing_preferred_skills": missing_preferred,
        "weak_skills": []  # Placeholder - could be enhanced with more sophisticated analysis
    }