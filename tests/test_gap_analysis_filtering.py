"""
Tests that non-skill job requirements never enter the skills pipeline.

A job description's "required_skills" routinely contains entries that are not
skills — "5+ years experience", "Bachelor's degree in Computer Science". They
can never match a resume skill, so they stayed in missing_required_skills
permanently, and that list is handed to the project generator as the skills
the student should build a portfolio project around. The result was a
suggested project to build "5+ years experience": a correctness bug in the
output that is invisible unless you read the generated projects.

Three layers guard this now — the schema example, the parsing prompt, and the
deterministic filter here. Only the third is testable without a live model.
"""
from unittest.mock import patch

import pytest

from app.analysis.gap_analysis import compute_gap, is_skill_like, partition_skills
from app.chains.project_generator import generate_projects
from app.schemas import JobParsed, ResumeParsed


# --- the filter -------------------------------------------------------------

@pytest.mark.parametrize(
    "entry",
    [
        "Python",
        "FastAPI",
        "PostgreSQL",
        "Docker",
        "AWS",
        "React.js",
        "CI/CD",
        "C++",
        "Node.js",
        "REST APIs",
        "Machine Learning",
        "Amazon Web Services",
        "Test Driven Development",
        "Natural Language Processing",
        "Object Oriented Programming",
        # Must survive: the credential pattern deliberately excludes the
        # ambiguous "MS"/"BS" abbreviations because they collide with these.
        "MS SQL Server",
    ],
)
def test_real_technologies_are_kept(entry):
    assert is_skill_like(entry)


@pytest.mark.parametrize(
    "entry",
    [
        "5+ years experience",
        "3-5 years of experience",
        "5 yrs Python",
        "Bachelor's degree in Computer Science",
        "Master's degree",
        "PhD in a related field",
        "AWS certification",
        "Strong understanding of database design",
        "Excellent written and verbal communication skills",
        "Proven ability to work in a fast-paced team environment",
        "",
        "   ",
    ],
)
def test_requirements_are_rejected(entry):
    assert not is_skill_like(entry)


def test_partition_loses_nothing():
    """Non-skills are routed, not silently discarded."""
    entries = ["Python", "5+ years experience", "Docker", "Bachelor's degree"]

    skills, requirements = partition_skills(entries)

    assert skills == ["Python", "Docker"]
    assert requirements == ["5+ years experience", "Bachelor's degree"]
    assert sorted(skills + requirements) == sorted(entries)


# --- compute_gap ------------------------------------------------------------

def _resume(skills):
    return ResumeParsed(
        name="Test User", skills=skills, experience=[], projects=[], education=[]
    )


def _job(required, preferred=None):
    return JobParsed(
        job_title="Developer",
        required_skills=required,
        preferred_skills=preferred or [],
        keywords=[],
        responsibilities=[],
        qualifications=[],
    )


def test_non_skills_excluded_from_missing_required():
    gap = compute_gap(
        _resume(["Python"]),
        _job(["Python", "React", "5+ years experience", "Bachelor's degree"]),
    )

    assert gap["missing_required_skills"] == ["React"]
    assert "5+ years experience" not in gap["missing_required_skills"]


def test_non_skills_are_reported_separately():
    gap = compute_gap(
        _resume(["Python"]),
        _job(["Python", "5+ years experience"], ["Master's degree"]),
    )

    assert gap["non_skill_requirements"] == ["5+ years experience", "Master's degree"]


def test_genuine_matching_still_works():
    gap = compute_gap(
        _resume(["Python", "JavaScript"]),
        _job(["Python", "React"], ["JavaScript"]),
    )

    assert gap["overlapping_skills"] == ["JavaScript", "Python"]
    assert gap["missing_required_skills"] == ["React"]


def test_overlapping_skills_are_deterministic():
    """Result is persisted and shown to the user, so ordering must be stable.

    Set iteration order for strings varies between processes, so an unsorted
    list made identical inputs produce differently-ordered output per run.
    """
    gap = compute_gap(
        _resume(["Python", "Docker", "AWS", "React", "SQL"]),
        _job(["Python", "Docker", "AWS"], ["React", "SQL"]),
    )

    assert gap["overlapping_skills"] == sorted(gap["overlapping_skills"])
    assert gap["overlapping_skills"] == ["AWS", "Docker", "Python", "React", "SQL"]


# --- the property that actually matters -------------------------------------

def test_a_requirement_never_becomes_a_project_to_build():
    """End-to-end: nothing non-skill reaches the project generator's prompt."""
    gap = compute_gap(
        _resume(["Python"]),
        _job(
            ["Python", "Kubernetes", "5+ years experience", "Bachelor's degree"],
            ["Terraform", "10 years of leadership"],
        ),
    )

    with patch(
        "app.chains.project_generator.invoke_with_retry", return_value=None
    ) as mock_invoke:
        generate_projects(gap)

    payload = mock_invoke.call_args[0][1]
    prompt_text = " ".join(payload.values())

    assert "Kubernetes" in prompt_text, "real missing skills must still be sent"
    assert "years" not in prompt_text
    assert "degree" not in prompt_text.lower()
