from fastapi.testclient import TestClient
from app.main import app
from unittest.mock import patch, Mock
from app.schemas import (
    ResumeParsed, JobParsed, ProjectPlanParsed, ProjectIdea, 
    ImprovedResumeParsed, ImprovedExperienceItem
)
from app.pipeline.state import PipelineState

client = TestClient(app)

@patch('app.routers.pipeline.run_pipeline')
def test_pipeline_endpoint_success(mock_run_pipeline):
    """Test T 11.3.1: POST /api/pipeline/run returns complete results"""
    # Mock the pipeline result
    mock_result: PipelineState = {
        "resume_id": 1,
        "job_id": 2,
        "resume_parsed": ResumeParsed(
            name="Test User",
            skills=["Python"],
            experience=[],
            projects=[],
            education=[]
        ),
        "job_parsed": JobParsed(
            job_title="Developer",
            required_skills=["Python", "React"],
            preferred_skills=[],
            keywords=[],
            responsibilities=[],
            qualifications=[]
        ),
        "gap_analysis": {
            "overlapping_skills": ["Python"],
            "missing_required_skills": ["React"],
            "missing_preferred_skills": [],
            "weak_skills": []
        },
        "projects": ProjectPlanParsed(
            projects=[
                ProjectIdea(
                    title="React App",
                    skill_targets=["React"],
                    difficulty="Intermediate",
                    description="Build a React app",
                    estimated_duration="2 weeks",
                    key_features=["Components"],
                    technologies=["React"]
                )
            ]
        ),
        "improved_resume": ImprovedResumeParsed(
            name="Test User",
            contact="test@email.com",
            skills=["Python", "React"],
            experience=[],
            projects=[],
            education=[]
        ),
        "analysis_id": 10,
        "project_plan_id": 20,
        "improved_resume_id": 30,
        "error": None
    }
    
    mock_run_pipeline.return_value = mock_result
    
    response = client.post(
        "/api/pipeline/run",
        json={"resume_id": 1, "job_id": 2}
    )
    
    assert response.status_code == 200
    data = response.json()
    
    assert data["resume_id"] == 1
    assert data["job_id"] == 2
    assert data["analysis_id"] == 10
    assert data["project_plan_id"] == 20
    assert data["improved_resume_id"] == 30
    assert "gap_analysis" in data
    assert "projects" in data
    assert "improved_resume" in data
    assert len(data["projects"]) == 1
    assert data["projects"][0]["title"] == "React App"

@patch('app.routers.pipeline.run_pipeline')
def test_pipeline_endpoint_error_handling(mock_run_pipeline):
    """Test that pipeline errors are handled properly"""
    mock_run_pipeline.side_effect = Exception("Pipeline failed")
    
    response = client.post(
        "/api/pipeline/run",
        json={"resume_id": 999, "job_id": 888}
    )
    
    assert response.status_code == 500
    assert "Pipeline execution failed" in response.json()["detail"]