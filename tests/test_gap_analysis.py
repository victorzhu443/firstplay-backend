import pytest
from app.analysis.gap_analysis import (
    normalize_skill,
    skills_match,
    find_matching_skills,
    compute_gap
)
from app.schemas import ResumeParsed, JobParsed

def test_normalize_skill():
    """Test skill normalization"""
    assert normalize_skill("Python") == "python"
    assert normalize_skill("JavaScript") == "js"
    assert normalize_skill("React.js") == "react"
    assert normalize_skill("PostgreSQL") == "postgres"

def test_skills_match():
    """Test skill matching with normalization"""
    assert skills_match("Python", "python") == True
    assert skills_match("JavaScript", "JS") == True
    assert skills_match("React.js", "React") == True
    assert skills_match("Python", "Java") == False

def test_find_matching_skills():
    """Test finding matching skills between two lists"""
    resume_skills = ["Python", "React", "PostgreSQL"]
    job_skills = ["python", "AWS", "postgres"]
    
    matches = find_matching_skills(resume_skills, job_skills)
    assert "python" in matches
    assert "postgres" in matches
    assert "AWS" not in matches
    assert len(matches) == 2

def test_compute_gap_basic():
    """Test T 8.1.1: correct overlaps/missing calculation"""
    # Create sample resume
    resume = ResumeParsed(
        name="Test User",
        skills=["Python", "React"],
        experience=[],
        projects=[],
        education=[]
    )
    
    # Create sample job
    job = JobParsed(
        job_title="Backend Developer",
        required_skills=["Python", "AWS"],
        preferred_skills=["Docker"],
        keywords=["backend"],
        responsibilities=["Build APIs"],
        qualifications=["BS in CS"]
    )
    
    gap = compute_gap(resume, job)
    
    # Check overlapping skills
    assert "Python" in gap["overlapping_skills"]
    
    # Check missing required skills
    assert "AWS" in gap["missing_required_skills"]
    
    # Check missing preferred skills
    assert "Docker" in gap["missing_preferred_skills"]
    
    # React shouldn't be in missing (it's not required by job)
    assert "React" not in gap["missing_required_skills"]

def test_compute_gap_case_insensitive():
    """Test that gap analysis is case-insensitive"""
    resume = ResumeParsed(
        name="Test User",
        skills=["python", "react", "aws"],
        experience=[],
        projects=[],
        education=[]
    )
    
    job = JobParsed(
        job_title="Developer",
        required_skills=["Python", "React", "AWS"],
        preferred_skills=[],
        keywords=[],
        responsibilities=[],
        qualifications=[]
    )
    
    gap = compute_gap(resume, job)
    
    # All skills should overlap despite case differences
    assert len(gap["overlapping_skills"]) == 3
    assert len(gap["missing_required_skills"]) == 0

def test_compute_gap_no_overlap():
    """Test gap analysis with no overlapping skills"""
    resume = ResumeParsed(
        name="Test User",
        skills=["Java", "Spring"],
        experience=[],
        projects=[],
        education=[]
    )
    
    job = JobParsed(
        job_title="Frontend Developer",
        required_skills=["React", "TypeScript"],
        preferred_skills=["Next.js"],
        keywords=[],
        responsibilities=[],
        qualifications=[]
    )
    
    gap = compute_gap(resume, job)
    
    assert len(gap["overlapping_skills"]) == 0
    assert len(gap["missing_required_skills"]) == 2
    assert "React" in gap["missing_required_skills"]
    assert "TypeScript" in gap["missing_required_skills"]

def test_compute_gap_all_skills_match():
    """Test gap analysis when candidate has all required skills"""
    resume = ResumeParsed(
        name="Test User",
        skills=["Python", "Django", "PostgreSQL", "Docker", "AWS"],
        experience=[],
        projects=[],
        education=[]
    )
    
    job = JobParsed(
        job_title="Backend Developer",
        required_skills=["Python", "Django", "PostgreSQL"],
        preferred_skills=["Docker", "AWS"],
        keywords=[],
        responsibilities=[],
        qualifications=[]
    )
    
    gap = compute_gap(resume, job)
    
    # Should have all overlapping
    assert len(gap["overlapping_skills"]) == 5
    # Should have no missing required
    assert len(gap["missing_required_skills"]) == 0
    # Should have no missing preferred
    assert len(gap["missing_preferred_skills"]) == 0