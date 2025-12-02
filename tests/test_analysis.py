from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.models import GapAnalysis
from unittest.mock import patch
from app.schemas import ResumeParsed, JobParsed
import json

client = TestClient(app)

def test_analyze_success():
    """Test T 8.2.1: GapAnalysis row saved with proper lists"""
    # Create and parse a resume
    with open("tests/fixtures/sample_resume.pdf", "rb") as f:
        files = {"file": ("sample_resume.pdf", f, "application/pdf")}
        upload_response = client.post("/api/resume/upload", files=files)
    resume_id = upload_response.json()["resume_id"]
    
    # Mock parsing
    with patch('app.routers.resume.parse_resume_text') as mock_parse_resume:
        mock_parse_resume.return_value = ResumeParsed(
            name="Test User",
            skills=["Python", "React", "PostgreSQL"],
            experience=[],
            projects=[],
            education=[]
        )
        parse_response = client.post("/api/resume/parse", params={"resume_id": resume_id})
        assert parse_response.status_code == 200
    
    # Create and parse a job description
    jd_text = """
    Backend Developer Position
    
    Required Skills: Python, FastAPI, AWS
    Preferred Skills: Docker, Kubernetes
    """
    job_response = client.post("/api/job/description/manual", json={"jd_text": jd_text})
    job_id = job_response.json()["job_id"]
    
    with patch('app.routers.job.parse_jd_text') as mock_parse_job:
        mock_parse_job.return_value = JobParsed(
            job_title="Backend Developer",
            required_skills=["Python", "FastAPI", "AWS"],
            preferred_skills=["Docker", "Kubernetes"],
            keywords=["backend"],
            responsibilities=["Build APIs"],
            qualifications=["3+ years"]
        )
        parse_job_response = client.post("/api/job/parse", params={"job_id": job_id})
        assert parse_job_response.status_code == 200
    
    # Run analysis
    response = client.post(
        "/api/analyze",
        json={"resume_id": resume_id, "job_id": job_id}
    )
    
    # Debug output
    print(f"\n=== DEBUG INFO ===")
    print(f"Status Code: {response.status_code}")
    print(f"Response Text: {response.text}")
    print(f"Response Headers: {response.headers}")
    print(f"==================\n")
    
    assert response.status_code == 200, f"Status: {response.status_code}, Body: {response.text}"
    
    # Try to parse JSON
    try:
        data = response.json()
    except Exception as e:
        assert False, f"Failed to parse JSON: {e}. Response text: {response.text}"
    
    assert data is not None, f"Data is None. Response text: {response.text}"
    assert "analysis_id" in data, f"analysis_id not in data. Data keys: {data.keys() if data else 'None'}"
    assert data["resume_id"] == resume_id
    assert data["job_id"] == job_id
    assert "gap_analysis" in data
    
    gap = data["gap_analysis"]
    assert "overlapping_skills" in gap
    assert "missing_required_skills" in gap
    
    # Python should be overlapping
    assert "Python" in gap["overlapping_skills"]
    
    # FastAPI and AWS should be missing (resume doesn't have them)
    assert "FastAPI" in gap["missing_required_skills"]
    assert "AWS" in gap["missing_required_skills"]
    
    # Verify saved to database
    db = SessionLocal()
    try:
        analysis = db.query(GapAnalysis).filter(GapAnalysis.id == data["analysis_id"]).first()
        assert analysis is not None
        assert analysis.resume_id == resume_id
        assert analysis.job_id == job_id
        assert analysis.analysis_json is not None
        
        # Verify the JSON structure
        saved_gap = json.loads(analysis.analysis_json)
        assert "overlapping_skills" in saved_gap
        assert "missing_required_skills" in saved_gap
    finally:
        db.close()

def test_analyze_missing_resume():
    """Test that missing resume returns 404"""
    response = client.post(
        "/api/analyze",
        json={"resume_id": 99999, "job_id": 1}
    )
    
    assert response.status_code == 404
    assert "resume" in response.json()["detail"].lower()

def test_analyze_missing_job():
    """Test that missing job returns 404"""
    # Create and PARSE a resume first
    with open("tests/fixtures/sample_resume.pdf", "rb") as f:
        files = {"file": ("sample_resume.pdf", f, "application/pdf")}
        upload_response = client.post("/api/resume/upload", files=files)
    resume_id = upload_response.json()["resume_id"]
    
    # Parse the resume
    with patch('app.routers.resume.parse_resume_text') as mock_parse_resume:
        mock_parse_resume.return_value = ResumeParsed(
            name="Test User",
            skills=["Python"],
            experience=[],
            projects=[],
            education=[]
        )
        client.post("/api/resume/parse", params={"resume_id": resume_id})
    
    # Try with non-existent job
    response = client.post(
        "/api/analyze",
        json={"resume_id": resume_id, "job_id": 99999}
    )
    
    assert response.status_code == 404
    assert "job" in response.json()["detail"].lower()

def test_analyze_resume_not_parsed():
    """Test T 8.2.2: missing parsed data → 400"""
    # Create a resume but don't parse it
    with open("tests/fixtures/sample_resume.pdf", "rb") as f:
        files = {"file": ("sample_resume.pdf", f, "application/pdf")}
        upload_response = client.post("/api/resume/upload", files=files)
    resume_id = upload_response.json()["resume_id"]
    
    # Create a job description
    jd_text = "Backend Developer. Required: Python, AWS. This is a sample job description text."
    job_response = client.post("/api/job/description/manual", json={"jd_text": jd_text})
    job_id = job_response.json()["job_id"]
    
    # Try to analyze without parsing
    response = client.post(
        "/api/analyze",
        json={"resume_id": resume_id, "job_id": job_id}
    )
    
    assert response.status_code == 400
    assert "parsed" in response.json()["detail"].lower()

from app.models import ProjectPlan
from app.schemas import ProjectIdea, ProjectPlanParsed

def test_generate_projects_success():
    """Test T 9.4.1: Project list returned + DB saved"""
    # Create full pipeline: resume, job, parse both, analyze
    with open("tests/fixtures/sample_resume.pdf", "rb") as f:
        files = {"file": ("sample_resume.pdf", f, "application/pdf")}
        upload_response = client.post("/api/resume/upload", files=files)
    resume_id = upload_response.json()["resume_id"]
    
    with patch('app.routers.resume.parse_resume_text') as mock_parse_resume:
        mock_parse_resume.return_value = ResumeParsed(
            name="Test User",
            skills=["Python", "JavaScript"],
            experience=[],
            projects=[],
            education=[]
        )
        client.post("/api/resume/parse", params={"resume_id": resume_id})
    
    jd_text = """
    Full Stack Developer
    Required: Python, React, AWS, Docker
    Preferred: Kubernetes
    """
    job_response = client.post("/api/job/description/manual", json={"jd_text": jd_text})
    job_id = job_response.json()["job_id"]
    
    with patch('app.routers.job.parse_jd_text') as mock_parse_job:
        mock_parse_job.return_value = JobParsed(
            job_title="Full Stack Developer",
            required_skills=["Python", "React", "AWS", "Docker"],
            preferred_skills=["Kubernetes"],
            keywords=["fullstack"],
            responsibilities=["Build apps"],
            qualifications=["3+ years"]
        )
        client.post("/api/job/parse", params={"job_id": job_id})
    
    # Run analysis
    analysis_response = client.post(
        "/api/analyze",
        json={"resume_id": resume_id, "job_id": job_id}
    )
    analysis_id = analysis_response.json()["analysis_id"]
    
    # Generate projects (mocked) - patch from the chains module
    with patch('app.routers.analysis.generate_projects') as mock_generate:
        mock_generate.return_value = ProjectPlanParsed(
            projects=[
                ProjectIdea(
                    title="React Todo App",
                    skill_targets=["React"],
                    difficulty="Beginner",
                    description="Build a todo app with React",
                    estimated_duration="1 week",
                    key_features=["CRUD", "State management"],
                    technologies=["React", "JavaScript"]
                ),
                ProjectIdea(
                    title="AWS Deployment",
                    skill_targets=["AWS", "Docker"],
                    difficulty="Intermediate",
                    description="Deploy app to AWS",
                    estimated_duration="2 weeks",
                    key_features=["EC2", "Docker", "CI/CD"],
                    technologies=["AWS", "Docker"]
                )
            ]
        )
        
        response = client.post(
            "/api/projects",
            params={"analysis_id": analysis_id}
        )
    
    # Debug output
    print(f"\nStatus: {response.status_code}")
    print(f"Response: {response.text}")
    
    assert response.status_code == 200, f"Status: {response.status_code}, Body: {response.text}"
    data = response.json()
    
    assert "project_plan_id" in data
    assert "projects" in data
    assert len(data["projects"]) == 2
    assert data["projects"][0]["title"] == "React Todo App"
    
    # Verify saved to database
    db = SessionLocal()
    try:
        plan = db.query(ProjectPlan).filter(ProjectPlan.id == data["project_plan_id"]).first()
        assert plan is not None
        assert plan.analysis_id == analysis_id
        assert plan.plan_json is not None
    finally:
        db.close()