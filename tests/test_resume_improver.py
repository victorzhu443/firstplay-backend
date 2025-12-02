import pytest
from unittest.mock import Mock, patch
from app.chains.resume_improver import create_resume_improvement_chain, improve_resume
from app.schemas import ResumeParsed, JobParsed, ImprovedResumeParsed, ImprovedExperienceItem

def test_create_chain_returns_runnable():
    """Test that create_resume_improvement_chain returns a LangChain runnable"""
    chain = create_resume_improvement_chain()
    assert chain is not None
    assert hasattr(chain, 'invoke')

@patch('app.chains.resume_improver.create_resume_improvement_chain')
def test_improve_with_mocked_output(mock_create_chain):
    """Test T 10.2.1: Mocked output includes action verbs + metrics"""
    mock_chain = Mock()
    mock_chain.invoke.return_value = ImprovedResumeParsed(
        name="Test User",
        contact="test@email.com | 555-0000",
        skills=["Python", "FastAPI", "AWS"],
        experience=[
            ImprovedExperienceItem(
                company="Tech Corp",
                title="Software Engineer",
                duration="2022-2024",
                bullets=[
                    "Developed RESTful APIs using FastAPI and PostgreSQL, reducing response time by 45%",
                    "Implemented JWT authentication system serving 10,000+ daily users with 99.9% uptime"
                ]
            )
        ],
        projects=[],
        education=["BS Computer Science"]
    )
    mock_create_chain.return_value = mock_chain
    
    resume = ResumeParsed(
        name="Test User",
        skills=["Python"],
        experience=[],
        projects=[],
        education=[]
    )
    
    job = JobParsed(
        job_title="Backend Developer",
        required_skills=["Python", "FastAPI"],
        preferred_skills=["AWS"],
        keywords=[],
        responsibilities=[],
        qualifications=[]
    )
    
    gap = {
        "overlapping_skills": ["Python"],
        "missing_required_skills": ["FastAPI"],
        "missing_preferred_skills": ["AWS"]
    }
    
    result = improve_resume(resume, job, gap)
    
    assert isinstance(result, ImprovedResumeParsed)
    assert result.name == "Test User"
    # Check for action verb
    assert any("Developed" in bullet or "Implemented" in bullet 
               for exp in result.experience for bullet in exp.bullets)
    # Check for metrics
    assert any("45%" in bullet or "10,000+" in bullet or "99.9%" in bullet
               for exp in result.experience for bullet in exp.bullets)

@patch('app.chains.resume_improver.create_resume_improvement_chain')
def test_improved_includes_job_keywords(mock_create_chain):
    """Test T 10.3.1: Output includes JD keywords"""
    mock_chain = Mock()
    mock_chain.invoke.return_value = ImprovedResumeParsed(
        name="Test User",
        contact="test@email.com",
        skills=["React", "TypeScript", "Node.js"],
        experience=[
            ImprovedExperienceItem(
                company="Web Co",
                title="Frontend Developer",
                duration="2021-2024",
                bullets=[
                    "Built responsive React components with TypeScript, improving code quality by 60%",
                    "Optimized Node.js backend API, reducing latency by 40%"
                ]
            )
        ],
        projects=[],
        education=["BS CS"]
    )
    mock_create_chain.return_value = mock_chain
    
    resume = ResumeParsed(
        name="Test User",
        skills=["JavaScript"],
        experience=[],
        projects=[],
        education=[]
    )
    
    job = JobParsed(
        job_title="Frontend Developer",
        required_skills=["React", "TypeScript", "Node.js"],
        preferred_skills=[],
        keywords=["responsive", "API"],
        responsibilities=[],
        qualifications=[]
    )
    
    gap = {
        "overlapping_skills": [],
        "missing_required_skills": ["React", "TypeScript", "Node.js"],
        "missing_preferred_skills": []
    }
    
    result = improve_resume(resume, job, gap)
    
    # Check that JD keywords appear in the improved resume
    all_text = " ".join([bullet for exp in result.experience for bullet in exp.bullets])
    assert "React" in all_text or "TypeScript" in all_text

@patch('app.chains.resume_improver.create_resume_improvement_chain')
def test_improve_handles_errors_gracefully(mock_create_chain):
    """Test that improvement errors are handled gracefully"""
    mock_chain = Mock()
    mock_chain.invoke.side_effect = ValueError("Invalid format")
    mock_create_chain.return_value = mock_chain
    
    resume = ResumeParsed(
        name="Test",
        skills=["Python"],
        experience=[],
        projects=[],
        education=[]
    )
    
    job = JobParsed(
        job_title="Dev",
        required_skills=["Python"],
        preferred_skills=[],
        keywords=[],
        responsibilities=[],
        qualifications=[]
    )
    
    gap = {"overlapping_skills": ["Python"], "missing_required_skills": []}
    
    with pytest.raises(Exception) as exc_info:
        improve_resume(resume, job, gap)
    
    assert "Failed to improve resume" in str(exc_info.value)