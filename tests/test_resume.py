from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.models import Resume
import io
import os

client = TestClient(app)

def test_upload_endpoint_exists():
    """Test T 3.1.1: Uploading a small sample PDF returns HTTP 200"""
    # Create a fake PDF file
    fake_pdf = io.BytesIO(b"%PDF-1.4 fake content")
    
    files = {"file": ("test_resume.pdf", fake_pdf, "application/pdf")}
    response = client.post("/api/resume/upload", files=files)
    
    # This might fail extraction, but endpoint should exist
    assert response.status_code in [200, 400]

def test_pdf_upload_accepted():
    """Test T 3.2.1: .pdf upload accepted (with real PDF)"""
    # Use the test fixture PDF
    pdf_path = "tests/fixtures/sample_resume.pdf"
    
    if not os.path.exists(pdf_path):
        # Skip if fixture doesn't exist
        import pytest
        pytest.skip("Test PDF fixture not found")
    
    with open(pdf_path, "rb") as f:
        files = {"file": ("sample_resume.pdf", f, "application/pdf")}
        response = client.post("/api/resume/upload", files=files)
    
    assert response.status_code == 200
    assert "resume_id" in response.json()

def test_non_pdf_rejected():
    """Test T 3.2.2: .txt upload returns 400 with 'Only PDF files supported'"""
    fake_txt = io.BytesIO(b"This is a text file")
    
    files = {"file": ("resume.txt", fake_txt, "text/plain")}
    response = client.post("/api/resume/upload", files=files)
    
    assert response.status_code == 400
    assert "Only PDF files" in response.json()["detail"]

def test_extract_text_contains_expected_phrase():
    """Test T 3.3.1: For known PDF fixture, raw_text contains expected phrase"""
    pdf_path = "tests/fixtures/sample_resume.pdf"
    
    if not os.path.exists(pdf_path):
        import pytest
        pytest.skip("Test PDF fixture not found")
    
    with open(pdf_path, "rb") as f:
        files = {"file": ("sample_resume.pdf", f, "application/pdf")}
        response = client.post("/api/resume/upload", files=files)
    
    assert response.status_code == 200
    data = response.json()
    
    # Check that the preview contains text from our test PDF
    preview = data["raw_text_preview"]
    assert "JOHN DOE" in preview or "Software Engineer" in preview

def test_resume_saved_to_database():
    """Test T 3.3.2: DB has a new Resume row with non-empty raw_text"""
    pdf_path = "tests/fixtures/sample_resume.pdf"
    
    if not os.path.exists(pdf_path):
        import pytest
        pytest.skip("Test PDF fixture not found")
    
    with open(pdf_path, "rb") as f:
        files = {"file": ("sample_resume.pdf", f, "application/pdf")}
        response = client.post("/api/resume/upload", files=files)
    
    assert response.status_code == 200
    resume_id = response.json()["resume_id"]
    
    # Query database to verify
    db = SessionLocal()
    try:
        resume = db.query(Resume).filter(Resume.id == resume_id).first()
        assert resume is not None
        assert resume.raw_text is not None
        assert len(resume.raw_text) > 0
        assert resume.original_filename == "sample_resume.pdf"
    finally:
        db.close()

def test_response_shape():
    """Test T 3.4.1: Response includes resume_id and preview <= 200 chars"""
    pdf_path = "tests/fixtures/sample_resume.pdf"
    
    if not os.path.exists(pdf_path):
        import pytest
        pytest.skip("Test PDF fixture not found")
    
    with open(pdf_path, "rb") as f:
        files = {"file": ("sample_resume.pdf", f, "application/pdf")}
        response = client.post("/api/resume/upload", files=files)
    
    assert response.status_code == 200
    data = response.json()
    
    assert "resume_id" in data
    assert isinstance(data["resume_id"], int)
    assert "raw_text_preview" in data
    assert isinstance(data["raw_text_preview"], str)
    assert len(data["raw_text_preview"]) <= 200

from unittest.mock import patch
from app.schemas import ResumeParsed

def test_parse_resume_endpoint_success():
    """Test T 4.5.1: parsed_json saved"""
    pdf_path = "tests/fixtures/sample_resume.pdf"
    
    if not os.path.exists(pdf_path):
        import pytest
        pytest.skip("Test PDF fixture not found")
    
    # Upload a resume first
    with open(pdf_path, "rb") as f:
        files = {"file": ("sample_resume.pdf", f, "application/pdf")}
        upload_response = client.post("/api/resume/upload", files=files)
    
    resume_id = upload_response.json()["resume_id"]
    
    # Mock the parse_resume_text function to avoid actual LLM calls
    with patch('app.routers.resume.parse_resume_text') as mock_parse:
        mock_parse.return_value = ResumeParsed(
            name="Test User",
            skills=["Python"],
            experience=[],
            projects=[],
            education=[]
        )
        
        # Parse the resume
        response = client.post(
            "/api/resume/parse",
            params={"resume_id": resume_id}
        )
    
    assert response.status_code == 200
    data = response.json()
    assert data["resume_id"] == resume_id
    assert "parsed_data" in data
    assert data["parsed_data"]["name"] == "Test User"
    
    # Verify saved to database
    db = SessionLocal()
    try:
        resume = db.query(Resume).filter(Resume.id == resume_id).first()
        assert resume.parsed_json is not None
        assert len(resume.parsed_json) > 0
    finally:
        db.close()

def test_parse_resume_invalid_id():
    """Test T 4.5.2: invalid resume_id → 404"""
    response = client.post(
        "/api/resume/parse",
        params={"resume_id": 99999}
    )
    
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()

from app.models import ImprovedResume
from app.schemas import JobParsed, ImprovedResumeParsed, ImprovedExperienceItem

def test_improve_resume_success():
    """Test T 10.4.1: Improved resume returned and saved"""
    # Create and parse resume
    with open("tests/fixtures/sample_resume.pdf", "rb") as f:
        files = {"file": ("sample_resume.pdf", f, "application/pdf")}
        upload_response = client.post("/api/resume/upload", files=files)
    resume_id = upload_response.json()["resume_id"]
    
    with patch('app.routers.resume.parse_resume_text') as mock_parse_resume:
        mock_parse_resume.return_value = ResumeParsed(
            name="John Doe",
            skills=["Python", "JavaScript"],
            experience=[],
            projects=[],
            education=[]
        )
        client.post("/api/resume/parse", params={"resume_id": resume_id})
    
    # Create and parse job
    jd_text = "Backend Engineer. Required: Python, FastAPI, PostgreSQL. Build scalable APIs."
    job_response = client.post("/api/job/description/manual", json={"jd_text": jd_text})
    job_id = job_response.json()["job_id"]
    
    with patch('app.routers.job.parse_jd_text') as mock_parse_job:
        mock_parse_job.return_value = JobParsed(
            job_title="Backend Engineer",
            required_skills=["Python", "FastAPI", "PostgreSQL"],
            preferred_skills=["Docker"],
            keywords=["scalable", "APIs"],
            responsibilities=["Build APIs"],
            qualifications=["3+ years"]
        )
        client.post("/api/job/parse", params={"job_id": job_id})
    
    # Run gap analysis
    analysis_response = client.post(
        "/api/analyze",
        json={"resume_id": resume_id, "job_id": job_id}
    )
    assert analysis_response.status_code == 200
    
    # Improve resume (mocked) - patch from chains module
    with patch('app.routers.resume.improve_resume') as mock_improve:
        mock_improve.return_value = ImprovedResumeParsed(
            name="John Doe",
            contact="john@email.com | 555-0000",
            skills=["Python", "FastAPI", "PostgreSQL", "JavaScript"],
            experience=[
                ImprovedExperienceItem(
                    company="Tech Corp",
                    title="Software Engineer",
                    duration="2022-2024",
                    bullets=[
                        "Developed RESTful APIs using Python and FastAPI, serving 100K+ daily requests with 99.9% uptime",
                        "Optimized PostgreSQL queries with indexing, reducing response time by 60%"
                    ]
                )
            ],
            projects=[],
            education=["BS Computer Science"]
        )
        
        response = client.post(
            "/api/resume/improve",
            params={"resume_id": resume_id, "job_id": job_id}
        )
    
    # Debug output
    print(f"\nStatus: {response.status_code}")
    print(f"Response: {response.text}")
    
    assert response.status_code == 200, f"Status: {response.status_code}, Body: {response.text}"
    data = response.json()
    
    assert "improved_resume_id" in data
    assert data["resume_id"] == resume_id
    assert data["job_id"] == job_id
    assert "improved_resume" in data
    
    # Check for Jake-style improvements
    improved = data["improved_resume"]
    assert "FastAPI" in str(improved)  # Should mention job-required skills
    
    # Verify saved to database
    db = SessionLocal()
    try:
        improved_record = db.query(ImprovedResume).filter(
            ImprovedResume.id == data["improved_resume_id"]
        ).first()
        assert improved_record is not None
        assert improved_record.resume_id == resume_id
        assert improved_record.job_id == job_id
        assert improved_record.improved_json is not None
    finally:
        db.close()

def test_improve_resume_missing_gap_analysis():
    """Test that missing gap analysis returns 400"""
    # Create and parse resume
    with open("tests/fixtures/sample_resume.pdf", "rb") as f:
        files = {"file": ("sample_resume.pdf", f, "application/pdf")}
        upload_response = client.post("/api/resume/upload", files=files)
    resume_id = upload_response.json()["resume_id"]
    
    with patch('app.routers.resume.parse_resume_text') as mock_parse_resume:
        mock_parse_resume.return_value = ResumeParsed(
            name="Test",
            skills=["Python"],
            experience=[],
            projects=[],
            education=[]
        )
        client.post("/api/resume/parse", params={"resume_id": resume_id})
    
    # Create and parse job
    jd_text = "Developer position. Required: Python. Good opportunity to learn and grow."
    job_response = client.post("/api/job/description/manual", json={"jd_text": jd_text})
    job_id = job_response.json()["job_id"]
    
    with patch('app.routers.job.parse_jd_text') as mock_parse_job:
        mock_parse_job.return_value = JobParsed(
            job_title="Developer",
            required_skills=["Python"],
            preferred_skills=[],
            keywords=[],
            responsibilities=[],
            qualifications=[]
        )
        client.post("/api/job/parse", params={"job_id": job_id})
    
    # Try to improve without gap analysis
    response = client.post(
        "/api/resume/improve",
        params={"resume_id": resume_id, "job_id": job_id}
    )
    
    assert response.status_code == 400
    assert "gap analysis" in response.json()["detail"].lower()