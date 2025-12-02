import pytest
from unittest.mock import Mock, patch
from app.chains.job_parser import create_job_parsing_chain, parse_jd_text
from app.schemas import JobParsed

def test_create_chain_returns_runnable():
    """Test that create_job_parsing_chain returns a LangChain runnable"""
    chain = create_job_parsing_chain()
    assert chain is not None
    assert hasattr(chain, 'invoke')

@patch('app.chains.job_parser.create_job_parsing_chain')
def test_parse_with_mocked_output(mock_create_chain):
    """Test T 7.2.1: Mocked output validates"""
    # Create a mock chain that returns a valid JobParsed object
    mock_chain = Mock()
    mock_chain.invoke.return_value = JobParsed(
        job_title="Software Engineer",
        company="Tech Co",
        required_skills=["Python", "FastAPI"],
        preferred_skills=["AWS"],
        keywords=["backend", "REST API"],
        responsibilities=["Build APIs", "Write tests"],
        qualifications=["BS in CS", "3+ years experience"]
    )
    mock_create_chain.return_value = mock_chain
    
    result = parse_jd_text("Sample job description text")
    
    assert isinstance(result, JobParsed)
    assert result.job_title == "Software Engineer"
    assert "Python" in result.required_skills

@patch('app.chains.job_parser.create_job_parsing_chain')
def test_parse_handles_errors_gracefully(mock_create_chain):
    """Test that parsing errors are handled gracefully"""
    # Mock chain that raises an exception
    mock_chain = Mock()
    mock_chain.invoke.side_effect = ValueError("Invalid format")
    mock_create_chain.return_value = mock_chain
    
    with pytest.raises(Exception) as exc_info:
        parse_jd_text("Sample job description text")
    
    assert "Failed to parse job description" in str(exc_info.value)