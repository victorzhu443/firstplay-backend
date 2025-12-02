from fastapi.testclient import TestClient
from app.main import app
from unittest.mock import patch, AsyncMock
from app.db import SessionLocal
from app.models import JobDescription
import httpx

client = TestClient(app)

def test_valid_url_accepted():
    """Test T 5.1.1: valid URL accepted"""
    with patch('app.routers.job.fetch_html', new=AsyncMock(return_value="<html>mock</html>")):
        response = client.post(
            "/api/job/url",
            json={"url": "https://careers.google.com/jobs/results/12345"}
        )
    
    assert response.status_code == 200
    data = response.json()
    assert "job_id" in data

def test_invalid_url_rejected():
    """Test T 5.1.2: invalid URL → 400"""
    # Test with invalid URL (no http/https)
    response = client.post(
        "/api/job/url",
        json={"url": "not-a-valid-url"}
    )
    
    assert response.status_code == 422  # Pydantic validation error
    
    # Test with too short URL
    response = client.post(
        "/api/job/url",
        json={"url": "http://x"}
    )
    
    assert response.status_code == 422

def test_missing_url_field():
    """Test that missing URL field is rejected"""
    response = client.post(
        "/api/job/url",
        json={}
    )
    
    assert response.status_code == 422

@patch('app.routers.job.httpx.AsyncClient')
def test_fetch_html_success(mock_client):
    """Test T 5.2.1: mock 200 response returns HTML string"""
    # Create mock response
    mock_response = AsyncMock()
    mock_response.text = "<html><body>Job Description</body></html>"
    mock_response.status_code = 200
    mock_response.raise_for_status = AsyncMock()
    
    # Mock the client context manager
    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client.return_value.__aenter__.return_value = mock_client_instance
    
    response = client.post(
        "/api/job/url",
        json={"url": "https://example.com/job"}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "job_id" in data

@patch('app.routers.job.httpx.AsyncClient')
def test_fetch_html_timeout(mock_client):
    """Test T 5.2.2: timeout handled"""
    # Mock timeout exception
    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))
    mock_client.return_value.__aenter__.return_value = mock_client_instance
    
    response = client.post(
        "/api/job/url",
        json={"url": "https://example.com/job"}
    )
    
    assert response.status_code == 504
    assert "timed out" in response.json()["detail"].lower()

@patch('app.routers.job.httpx.AsyncClient')
def test_fetch_html_404(mock_client):
    """Test T 5.2.2: 404 handled"""
    # Create a proper mock for HTTPStatusError
    mock_response = AsyncMock()
    mock_response.status_code = 404
    
    # Create the exception with proper request object
    mock_request = AsyncMock()
    http_error = httpx.HTTPStatusError(
        "Not Found",
        request=mock_request,
        response=mock_response
    )
    
    # Make raise_for_status actually raise the exception
    def raise_error():
        raise http_error
    
    mock_response.raise_for_status = raise_error
    
    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client.return_value.__aenter__.return_value = mock_client_instance
    
    response = client.post(
        "/api/job/url",
        json={"url": "https://example.com/nonexistent"}
    )
    
    assert response.status_code == 404

def test_extract_text_contains_job_content():
    """Test T 5.3.1: extraction contains JD content"""
    with patch('app.routers.job.fetch_html') as mock_fetch:
        # Mock HTML with job description content
        mock_html = """
        <html>
            <head><title>Job Posting</title></head>
            <body>
                <h1>Software Engineer</h1>
                <div class="description">
                    <p>We are looking for a talented software engineer.</p>
                    <h2>Responsibilities</h2>
                    <ul>
                        <li>Write clean code</li>
                        <li>Collaborate with team</li>
                    </ul>
                    <h2>Requirements</h2>
                    <p>Python, JavaScript, React</p>
                </div>
                <script>console.log('tracking');</script>
            </body>
        </html>
        """
        mock_fetch.return_value = mock_html
        
        response = client.post(
            "/api/job/url",
            json={"url": "https://example.com/job/12345"}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Check that important content is extracted
        assert "Software Engineer" in data["text_preview"] or "talented software engineer" in data["text_preview"]

def test_job_saved_to_database():
    """Test T 5.3.2: DB row created"""
    with patch('app.routers.job.fetch_html') as mock_fetch:
        mock_html = "<html><body><h1>Job Title</h1><p>Job description here</p></body></html>"
        mock_fetch.return_value = mock_html
        
        response = client.post(
            "/api/job/url",
            json={"url": "https://example.com/job/67890"}
        )
        
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        
        # Verify in database
        db = SessionLocal()
        try:
            job = db.query(JobDescription).filter(JobDescription.id == job_id).first()
            assert job is not None
            assert job.url == "https://example.com/job/67890"
            assert job.extracted_text is not None
            assert len(job.extracted_text) > 0
            assert job.raw_html is not None
        finally:
            db.close()

def test_response_includes_job_id_and_preview():
    """Test T 5.4.1: job_id + preview returned"""
    with patch('app.routers.job.fetch_html') as mock_fetch:
        mock_html = "<html><body><p>This is a job description with some content</p></body></html>"
        mock_fetch.return_value = mock_html
        
        response = client.post(
            "/api/job/url",
            json={"url": "https://example.com/job/999"}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert "job_id" in data
        assert isinstance(data["job_id"], int)
        assert "text_preview" in data
        assert isinstance(data["text_preview"], str)
        assert len(data["text_preview"]) <= 200

def test_manual_jd_with_valid_text():
    """Test T 6.1.1: Non-empty jd_text → 200 and job_id"""
    jd_text = """
    Software Engineer Position
    
    We are seeking a talented software engineer to join our team.
    
    Requirements:
    - 3+ years of experience with Python
    - Strong knowledge of web frameworks
    - Experience with databases
    
    Responsibilities:
    - Design and develop web applications
    - Collaborate with cross-functional teams
    - Write clean, maintainable code
    """
    
    response = client.post(
        "/api/job/description/manual",
        json={"jd_text": jd_text}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "job_id" in data
    assert isinstance(data["job_id"], int)
    assert "text_preview" in data

def test_manual_jd_empty_text_rejected():
    """Test T 6.1.2: Empty jd_text → 400 with helpful message"""
    # Test with empty string
    response = client.post(
        "/api/job/description/manual",
        json={"jd_text": ""}
    )
    
    assert response.status_code == 422
    assert "empty" in response.json()["detail"][0]["msg"].lower()
    
    # Test with only whitespace
    response = client.post(
        "/api/job/description/manual",
        json={"jd_text": "   \n   "}
    )
    
    assert response.status_code == 422

def test_manual_jd_too_short():
    """Test that very short JD text is rejected"""
    response = client.post(
        "/api/job/description/manual",
        json={"jd_text": "Short text"}
    )
    
    assert response.status_code == 422
    assert "too short" in response.json()["detail"][0]["msg"].lower()

def test_manual_jd_saved_to_database():
    """Test T 6.2.1: DB row's extracted_text equals posted jd_text"""
    jd_text = """
    Backend Developer - Remote
    
    Join our growing team as a backend developer. We're looking for someone
    with strong Python skills and experience with RESTful APIs. You'll work
    on exciting projects and have the opportunity to learn new technologies.
    
    Requirements: Python, FastAPI, PostgreSQL, Docker
    """
    
    response = client.post(
        "/api/job/description/manual",
        json={"jd_text": jd_text}
    )
    
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    
    # Verify in database
    db = SessionLocal()
    try:
        job = db.query(JobDescription).filter(JobDescription.id == job_id).first()
        assert job is not None
        assert job.url is None  # Should be null for manual entries
        assert job.raw_html is None  # Should be null for manual entries
        assert job.extracted_text == jd_text.strip()
        assert job.extracted_text is not None
        assert len(job.extracted_text) > 0
    finally:
        db.close()

from unittest.mock import patch
from app.schemas import JobParsed

def test_parse_job_endpoint_success():
    """Test T 7.4.1: parsed_json saved"""
    # First create a job description
    jd_text = """
    Senior Backend Engineer
    
    We are looking for an experienced backend engineer to join our team.
    
    Required Skills:
    - Python, FastAPI, PostgreSQL
    - 5+ years experience
    
    Preferred Skills:
    - AWS, Docker, Kubernetes
    
    Responsibilities:
    - Design and build scalable APIs
    - Mentor junior developers
    - Lead technical decisions
    """
    
    create_response = client.post(
        "/api/job/description/manual",
        json={"jd_text": jd_text}
    )
    job_id = create_response.json()["job_id"]
    
    # Mock the parse_jd_text function to avoid actual LLM calls
    with patch('app.routers.job.parse_jd_text') as mock_parse:
        mock_parse.return_value = JobParsed(
            job_title="Senior Backend Engineer",
            company=None,
            required_skills=["Python", "FastAPI", "PostgreSQL"],
            preferred_skills=["AWS", "Docker"],
            keywords=["backend", "APIs", "scalable"],
            responsibilities=["Design APIs", "Mentor developers"],
            qualifications=["5+ years experience"]
        )
        
        # Parse the job
        response = client.post(
            "/api/job/parse",
            params={"job_id": job_id}
        )
    
    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == job_id
    assert "parsed_data" in data
    assert data["parsed_data"]["job_title"] == "Senior Backend Engineer"
    
    # Verify saved to database
    db = SessionLocal()
    try:
        job = db.query(JobDescription).filter(JobDescription.id == job_id).first()
        assert job.parsed_json is not None
        assert len(job.parsed_json) > 0
    finally:
        db.close()

def test_parse_job_invalid_id():
    """Test T 7.4.2: Missing extracted_text → 400"""
    response = client.post(
        "/api/job/parse",
        params={"job_id": 99999}
    )
    
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()

def test_parse_job_no_extracted_text():
    """Test that job without extracted_text returns 400"""
    # Create a job with no text (we'll have to do this via database directly)
    db = SessionLocal()
    try:
        job = JobDescription(
            url="https://example.com",
            raw_html="<html></html>",
            extracted_text=""  # Empty text
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()
    
    response = client.post(
        "/api/job/parse",
        params={"job_id": job_id}
    )
    
    assert response.status_code == 400
    assert "no text to parse" in response.json()["detail"].lower()