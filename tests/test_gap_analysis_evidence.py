"""
Tests for where skill evidence is read from, and how names are matched.

Two sources of false gaps, both of which ended as a recommended project to
learn something the candidate already had:

  - only `resume.skills` was read, so a technology listed against a project
    counted for nothing
  - names were compared by exact equality against a seven-entry table, so
    "React.js" did not match "React", nor "Python 3" match "Python"
"""
import pytest

from app.analysis.gap_analysis import (
    appears_in_text,
    collect_declared_skills,
    collect_evidence_text,
    compute_gap,
    normalize_skill,
    skills_match,
)
from app.schemas import ExperienceItem, JobParsed, ProjectItem, ResumeParsed


def _resume(skills=None, projects=None, experience=None):
    return ResumeParsed(
        name="Test User",
        skills=skills or [],
        experience=experience or [],
        projects=projects or [],
        education=[],
    )


def _job(required=None, preferred=None):
    return JobParsed(
        job_title="Developer",
        required_skills=required or [],
        preferred_skills=preferred or [],
        keywords=[],
        responsibilities=[],
        qualifications=[],
    )


def _project(name="P", technologies=None, description="", highlights=None):
    return ProjectItem(
        name=name,
        description=description,
        technologies=technologies or [],
        highlights=highlights or [],
    )


# --- B3: normalization ------------------------------------------------------

@pytest.mark.parametrize(
    "a,b",
    [
        ("React.js", "React"),
        ("react js", "ReactJS"),
        ("Python 3", "Python"),
        ("Java 17", "java"),
        ("Vue 3.2", "vue"),
        ("Node.js", "node"),
        ("PostgreSQL", "postgres"),
        ("JavaScript", "JS"),
        ("Amazon Web Services", "AWS"),
        ("Google Cloud Platform", "gcp"),
        ("Kubernetes", "k8s"),
        ("REST APIs", "rest api"),
        ("CI/CD", "ci cd"),
    ],
)
def test_equivalent_spellings_match(a, b):
    assert skills_match(a, b), f"{a!r} should match {b!r}"


@pytest.mark.parametrize(
    "a,b",
    [
        ("Python", "Java"),
        ("React", "Angular"),
        ("C++", "C#"),
        ("Go", "Golang Django"),
    ],
)
def test_distinct_skills_do_not_match(a, b):
    assert not skills_match(a, b)


def test_plus_and_hash_survive_normalization():
    """C++ and C# must not be flattened into "c"."""
    assert normalize_skill("C++") == "c++"
    assert normalize_skill("C#") == "c#"
    assert not skills_match("C++", "C")


# --- B2: where declared skills come from ------------------------------------

def test_project_technologies_count_as_declared():
    resume = _resume(skills=["Python"], projects=[_project(technologies=["React"])])

    declared = collect_declared_skills(resume)

    assert "Python" in declared
    assert "React" in declared


def test_project_technology_is_not_reported_missing():
    """Previously: recommend learning React to someone who built with React."""
    resume = _resume(skills=["Python"], projects=[_project(technologies=["React"])])

    gap = compute_gap(resume, _job(required=["Python", "React"]))

    assert gap["missing_required_skills"] == []
    assert "React" in gap["overlapping_skills"]


# --- B5: weak_skills now means something ------------------------------------

def test_skill_shown_only_in_prose_is_weak_not_missing():
    resume = _resume(
        skills=["Python"],
        experience=[
            ExperienceItem(
                company="Corp",
                title="Engineer",
                duration="2022-2024",
                bullets=["Deployed services with Docker and monitored them"],
            )
        ],
    )

    gap = compute_gap(resume, _job(required=["Python", "Docker"]))

    assert gap["weak_skills"] == ["Docker"]
    assert "Docker" not in gap["missing_required_skills"]
    # Not claimed, so not an overlap either.
    assert "Docker" not in gap["overlapping_skills"]


def test_genuinely_absent_skill_is_still_missing():
    resume = _resume(skills=["Python"])

    gap = compute_gap(resume, _job(required=["Python", "Kubernetes"]))

    assert gap["missing_required_skills"] == ["Kubernetes"]
    assert gap["weak_skills"] == []


def test_declared_skill_outranks_prose_evidence():
    """A claimed skill is an overlap, never merely weak."""
    resume = _resume(
        skills=["Docker"],
        experience=[
            ExperienceItem(
                company="Corp",
                title="Engineer",
                duration="2022-2024",
                bullets=["Deployed with Docker"],
            )
        ],
    )

    gap = compute_gap(resume, _job(required=["Docker"]))

    assert gap["overlapping_skills"] == ["Docker"]
    assert gap["weak_skills"] == []


# --- whole-token matching ---------------------------------------------------

def test_evidence_matching_requires_whole_tokens():
    """A substring test reports Go present in Django and R in React."""
    text = "built a django service and a react frontend"

    assert not appears_in_text("Go", text)
    assert not appears_in_text("R", text)
    assert appears_in_text("react", text)
    assert appears_in_text("Django", text)


def test_evidence_text_covers_bullets_and_project_prose():
    resume = _resume(
        experience=[
            ExperienceItem(
                company="C", title="T", duration="D", bullets=["used Terraform"]
            )
        ],
        projects=[
            _project(description="a Redis cache", highlights=["tuned Kafka throughput"])
        ],
    )

    evidence = collect_evidence_text(resume)

    assert "terraform" in evidence
    assert "redis" in evidence
    assert "kafka" in evidence
