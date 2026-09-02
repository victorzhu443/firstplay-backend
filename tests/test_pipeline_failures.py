"""
Tests for pipeline failure handling: halt-on-failure and partial results.

Regression cover for two linked defects:

  1. Edges were unconditional, so a node could fail, leave its output as None,
     and every later node would still run and crash on `None.get(...)`. Each
     crash overwrote `state["error"]`, so the reported error was always the
     *last* node's complaint about missing data — the root cause was gone.

  2. run_pipeline raised whenever `error` was set, so any single node failure
     became a blank 500 and every artifact already produced was discarded.
"""
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.pipeline.graph import run_pipeline
from app.schemas import (
    ImprovedResumeParsed,
    JobParsed,
    ProjectIdea,
    ProjectPlanParsed,
    ResumeParsed,
)

client = TestClient(app)


# --- fixtures ---------------------------------------------------------------

def _resume_parsed():
    return ResumeParsed(
        name="Test User", skills=["Python"], experience=[], projects=[], education=[]
    )


def _job_parsed():
    return JobParsed(
        job_title="Developer",
        required_skills=["Python", "React"],
        preferred_skills=[],
        keywords=[],
        responsibilities=[],
        qualifications=[],
    )


def _gap():
    return {
        "overlapping_skills": ["Python"],
        "missing_required_skills": ["React"],
        "missing_preferred_skills": [],
        "weak_skills": [],
    }


def _projects():
    return ProjectPlanParsed(
        projects=[
            ProjectIdea(
                title="React App",
                skill_targets=["React"],
                difficulty="Intermediate",
                description="Build a React app",
                estimated_duration="2 weeks",
                key_features=["Components"],
                technologies=["React"],
            )
        ]
    )


def _improved():
    return ImprovedResumeParsed(
        name="Test User",
        contact="test@email.com",
        skills=["Python", "React"],
        experience=[],
        projects=[],
        education=[],
    )


def _mock_db(resume=..., job=...):
    """A mock session returning a resume row then a job row."""
    if resume is ...:
        resume = Mock(id=1, raw_text="Sample resume text", parsed_json=None)
    if job is ...:
        job = Mock(id=2, extracted_text="Sample job text", parsed_json=None)

    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [resume, job]
    return db


# --- halt on failure --------------------------------------------------------

@patch("app.pipeline.nodes.improve_resume")
@patch("app.pipeline.nodes.generate_projects")
@patch("app.pipeline.nodes.compute_gap")
@patch("app.pipeline.nodes.parse_jd_text")
@patch("app.pipeline.nodes.parse_resume_text")
def test_failure_halts_downstream_nodes(
    mock_parse_resume, mock_parse_job, mock_gap, mock_projects, mock_improve
):
    """A failed node must stop the run, not let the next node crash on None."""
    mock_parse_resume.return_value = _resume_parsed()
    mock_parse_job.return_value = _job_parsed()
    mock_gap.side_effect = ValueError("gap analysis exploded")

    result = run_pipeline(1, 2, _mock_db())

    mock_projects.assert_not_called()
    mock_improve.assert_not_called()
    assert result["projects"] is None
    assert result["improved_resume"] is None


@patch("app.pipeline.nodes.improve_resume")
@patch("app.pipeline.nodes.generate_projects")
@patch("app.pipeline.nodes.compute_gap")
@patch("app.pipeline.nodes.parse_jd_text")
@patch("app.pipeline.nodes.parse_resume_text")
def test_root_cause_is_not_overwritten(
    mock_parse_resume, mock_parse_job, mock_gap, mock_projects, mock_improve
):
    """The reported failure must name the node that actually broke.

    Before conditional edges, this surfaced as
    "Error improving resume: ... 'NoneType' object has no attribute 'get'".
    """
    mock_parse_resume.return_value = _resume_parsed()
    mock_parse_job.return_value = _job_parsed()
    mock_gap.side_effect = ValueError("gap analysis exploded")

    result = run_pipeline(1, 2, _mock_db())

    assert len(result["failures"]) == 1
    failure = result["failures"][0]
    assert failure["node"] == "analyze_gap"
    assert failure["error_type"] == "ValueError"
    assert "gap analysis exploded" in failure["message"]

    # The compat field agrees with the first (root) failure.
    assert "gap analysis exploded" in result["error"]
    assert "NoneType" not in result["error"]


@patch("app.pipeline.nodes.parse_jd_text")
@patch("app.pipeline.nodes.parse_resume_text")
def test_missing_resume_fails_first_node_only(mock_parse_resume, mock_parse_job):
    """A missing row is a node failure, not an exception out of run_pipeline."""
    result = run_pipeline(99, 2, _mock_db(resume=None))

    assert result["completed_nodes"] == []
    assert len(result["failures"]) == 1
    assert result["failures"][0]["node"] == "parse_resume"
    assert result["failures"][0]["error_type"] == "PipelineNodeError"
    mock_parse_resume.assert_not_called()
    mock_parse_job.assert_not_called()


@patch("app.pipeline.nodes.parse_resume_text")
def test_resume_with_no_text_is_rejected_before_the_llm(mock_parse_resume):
    """The node validates raw_text, matching the router's own check."""
    empty = Mock(id=1, raw_text="", parsed_json=None)

    result = run_pipeline(1, 2, _mock_db(resume=empty))

    assert result["failures"][0]["node"] == "parse_resume"
    assert "no text" in result["failures"][0]["message"]
    mock_parse_resume.assert_not_called()


# --- partial results survive ------------------------------------------------

@patch("app.pipeline.nodes.improve_resume")
@patch("app.pipeline.nodes.generate_projects")
@patch("app.pipeline.nodes.compute_gap")
@patch("app.pipeline.nodes.parse_jd_text")
@patch("app.pipeline.nodes.parse_resume_text")
def test_partial_results_are_preserved(
    mock_parse_resume, mock_parse_job, mock_gap, mock_projects, mock_improve
):
    """Work completed before the failure must survive, not be discarded."""
    mock_parse_resume.return_value = _resume_parsed()
    mock_parse_job.return_value = _job_parsed()
    mock_gap.return_value = _gap()
    mock_projects.side_effect = RuntimeError("project generation exploded")

    result = run_pipeline(1, 2, _mock_db())

    assert result["completed_nodes"] == ["parse_resume", "parse_job", "analyze_gap"]
    assert result["resume_parsed"] is not None
    assert result["job_parsed"] is not None
    assert result["gap_analysis"] == _gap()
    assert result["failures"][0]["node"] == "generate_projects"
    mock_improve.assert_not_called()


@patch("app.pipeline.nodes.improve_resume")
@patch("app.pipeline.nodes.generate_projects")
@patch("app.pipeline.nodes.compute_gap")
@patch("app.pipeline.nodes.parse_jd_text")
@patch("app.pipeline.nodes.parse_resume_text")
def test_clean_run_records_every_node(
    mock_parse_resume, mock_parse_job, mock_gap, mock_projects, mock_improve
):
    mock_parse_resume.return_value = _resume_parsed()
    mock_parse_job.return_value = _job_parsed()
    mock_gap.return_value = _gap()
    mock_projects.return_value = _projects()
    mock_improve.return_value = _improved()

    result = run_pipeline(1, 2, _mock_db())

    assert result["failures"] == []
    assert result["error"] is None
    assert result["completed_nodes"] == [
        "parse_resume",
        "parse_job",
        "analyze_gap",
        "generate_projects",
        "improve_resume",
    ]


# --- the HTTP contract ------------------------------------------------------

@patch("app.routers.pipeline.run_pipeline")
def test_endpoint_reports_partial_with_200(mock_run):
    """Partial results are renderable, so they must not arrive as a 500."""
    mock_run.return_value = {
        "resume_id": 1,
        "job_id": 2,
        "resume_parsed": _resume_parsed(),
        "job_parsed": _job_parsed(),
        "gap_analysis": _gap(),
        "projects": None,
        "improved_resume": None,
        "analysis_id": 10,
        "project_plan_id": None,
        "improved_resume_id": None,
        "completed_nodes": ["parse_resume", "parse_job", "analyze_gap"],
        "failures": [
            {
                "node": "generate_projects",
                "error_type": "LLMOutputError",
                "message": "Failed to generate projects: bad output",
            }
        ],
        "error": "Failed to generate projects: bad output",
    }

    response = client.post("/api/pipeline/run", json={"resume_id": 1, "job_id": 2})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "partial"
    assert data["gap_analysis"] is not None
    assert data["analysis_id"] == 10
    assert data["projects"] == []
    assert data["improved_resume"] is None
    assert data["failures"][0]["node"] == "generate_projects"
    assert data["completed_steps"] == ["parse_resume", "parse_job", "analyze_gap"]


@patch("app.routers.pipeline.run_pipeline")
def test_endpoint_reports_total_failure_with_502(mock_run):
    """Nothing was produced, so this is not a success."""
    mock_run.return_value = {
        "resume_id": 99,
        "job_id": 2,
        "resume_parsed": None,
        "job_parsed": None,
        "gap_analysis": None,
        "projects": None,
        "improved_resume": None,
        "analysis_id": None,
        "project_plan_id": None,
        "improved_resume_id": None,
        "completed_nodes": [],
        "failures": [
            {
                "node": "parse_resume",
                "error_type": "PipelineNodeError",
                "message": "Resume 99 not found",
            }
        ],
        "error": "Resume 99 not found",
    }

    response = client.post("/api/pipeline/run", json={"resume_id": 99, "job_id": 2})

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["status"] == "failed"
    assert detail["failures"][0]["message"] == "Resume 99 not found"


@patch("app.routers.pipeline.run_pipeline")
def test_endpoint_reports_complete(mock_run):
    mock_run.return_value = {
        "resume_id": 1,
        "job_id": 2,
        "resume_parsed": _resume_parsed(),
        "job_parsed": _job_parsed(),
        "gap_analysis": _gap(),
        "projects": _projects(),
        "improved_resume": _improved(),
        "analysis_id": 10,
        "project_plan_id": 20,
        "improved_resume_id": 30,
        "completed_nodes": [
            "parse_resume",
            "parse_job",
            "analyze_gap",
            "generate_projects",
            "improve_resume",
        ],
        "failures": [],
        "error": None,
    }

    response = client.post("/api/pipeline/run", json={"resume_id": 1, "job_id": 2})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "complete"
    assert data["failures"] == []
    assert len(data["projects"]) == 1
