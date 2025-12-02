import pytest
from unittest.mock import Mock, patch
from app.chains.resume_parser import create_resume_parsing_chain, parse_resume_text
from app.schemas import ResumeParsed

def test_create_chain_returns_runnable():
    """Test that create_resume_parsing_chain returns a LangChain runnable"""
    chain = create_resume_parsing_chain()
    assert chain is not None
    assert hasattr(chain, 'invoke')

@patch('app.chains.resume_parser.create_resume_parsing_chain')
def test_parse_with_mocked_output(mock_create_chain):
    """Test T 4.3.1: mocked output validates"""
    # Create a mock chain that returns a valid ResumeParsed object
    mock_chain = Mock()
    mock_chain.invoke.return_value = ResumeParsed(
        name="Test User",
        email="test@example.com",
        phone="555-0000",
        skills=["Python", "FastAPI"],
        experience=[{
            "company": "Test Corp",
            "title": "Developer",
            "duration": "2020-2022",
            "bullets": ["Built things"]
        }],
        projects=[],
        education=[{
            "institution": "Test University",
            "degree": "BS CS",
            "graduation_date": "2020"
        }]
    )
    mock_create_chain.return_value = mock_chain
    
    result = parse_resume_text("Sample resume text")
    
    assert isinstance(result, ResumeParsed)
    assert result.name == "Test User"
    assert "Python" in result.skills

@patch('app.chains.resume_parser.create_resume_parsing_chain')
def test_parse_handles_malformed_json_gracefully(mock_create_chain):
    """Test T 4.3.2: malformed JSON handled gracefully"""
    # Mock chain that raises an exception
    mock_chain = Mock()
    mock_chain.invoke.side_effect = ValueError("Invalid JSON format")
    mock_create_chain.return_value = mock_chain
    
    with pytest.raises(Exception) as exc_info:
        parse_resume_text("Sample resume text")
    
    assert "Failed to parse resume" in str(exc_info.value)