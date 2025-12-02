import pytest
from unittest.mock import Mock, patch
from app.chains.project_generator import create_project_generation_chain, generate_projects
from app.schemas import ProjectPlanParsed, ProjectIdea

def test_create_chain_returns_runnable():
    """Test that create_project_generation_chain returns a LangChain runnable"""
    chain = create_project_generation_chain()
    assert chain is not None
    assert hasattr(chain, 'invoke')

@patch('app.chains.project_generator.create_project_generation_chain')
def test_generate_with_mocked_output(mock_create_chain):
    """Test T 9.2.1: Mocked output validated"""
    # Create a mock chain that returns valid ProjectPlanParsed
    mock_chain = Mock()
    mock_chain.invoke.return_value = ProjectPlanParsed(
        projects=[
            ProjectIdea(
                title="REST API with FastAPI",
                skill_targets=["FastAPI", "PostgreSQL"],
                difficulty="Intermediate",
                description="Build a RESTful API with database integration",
                estimated_duration="2-3 weeks",
                key_features=["CRUD operations", "Authentication", "Database"],
                technologies=["Python", "FastAPI", "PostgreSQL"]
            ),
            ProjectIdea(
                title="AWS Deployment Project",
                skill_targets=["AWS", "Docker"],
                difficulty="Intermediate",
                description="Deploy a web application to AWS",
                estimated_duration="1-2 weeks",
                key_features=["EC2 setup", "Docker containers", "Load balancing"],
                technologies=["AWS", "Docker", "Nginx"]
            )
        ]
    )
    mock_create_chain.return_value = mock_chain
    
    gap_analysis = {
        "overlapping_skills": ["Python", "React"],
        "missing_required_skills": ["FastAPI", "AWS"],
        "missing_preferred_skills": ["Docker"]
    }
    
    result = generate_projects(gap_analysis)
    
    assert isinstance(result, ProjectPlanParsed)
    assert len(result.projects) == 2
    assert any("FastAPI" in p.skill_targets for p in result.projects)

@patch('app.chains.project_generator.create_project_generation_chain')
def test_generate_includes_missing_skills(mock_create_chain):
    """Test T 9.3.1: Missing React skill → React project returned"""
    mock_chain = Mock()
    mock_chain.invoke.return_value = ProjectPlanParsed(
        projects=[
            ProjectIdea(
                title="React Dashboard",
                skill_targets=["React", "TypeScript"],
                difficulty="Intermediate",
                description="Build an interactive dashboard",
                estimated_duration="2 weeks",
                key_features=["Charts", "Real-time updates", "Responsive design"],
                technologies=["React", "TypeScript", "Chart.js"]
            )
        ]
    )
    mock_create_chain.return_value = mock_chain
    
    gap_analysis = {
        "overlapping_skills": ["JavaScript"],
        "missing_required_skills": ["React"],
        "missing_preferred_skills": []
    }
    
    result = generate_projects(gap_analysis)
    
    # Should have at least one project targeting React
    assert any("React" in p.skill_targets for p in result.projects)

@patch('app.chains.project_generator.create_project_generation_chain')
def test_generate_handles_errors_gracefully(mock_create_chain):
    """Test that generation errors are handled gracefully"""
    mock_chain = Mock()
    mock_chain.invoke.side_effect = ValueError("Invalid format")
    mock_create_chain.return_value = mock_chain
    
    gap_analysis = {
        "overlapping_skills": ["Python"],
        "missing_required_skills": ["AWS"],
        "missing_preferred_skills": []
    }
    
    with pytest.raises(Exception) as exc_info:
        generate_projects(gap_analysis)
    
    assert "Failed to generate projects" in str(exc_info.value)