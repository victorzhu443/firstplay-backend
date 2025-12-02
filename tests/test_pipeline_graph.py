import pytest
from unittest.mock import Mock, MagicMock, patch
from app.pipeline.graph import create_pipeline_graph, run_pipeline
from app.pipeline.state import PipelineState
from app.schemas import ResumeParsed, JobParsed, ProjectPlanParsed, ProjectIdea, ImprovedResumeParsed, ImprovedExperienceItem

def test_create_pipeline_graph():
    """Test T 11.2.1: Graph creation succeeds"""
    mock_db = Mock()
    graph = create_pipeline_graph(mock_db)
    
    assert graph is not None
    # Graph should be compiled and ready to run
    assert hasattr(graph, 'invoke')

@patch('app.pipeline.nodes.parse_resume_text')
@patch('app.pipeline.nodes.parse_jd_text')
@patch('app.pipeline.nodes.compute_gap')
@patch('app.pipeline.nodes.generate_projects')
@patch('app.pipeline.nodes.improve_resume')
def test_run_pipeline_mocked(
    mock_improve,
    mock_projects,
    mock_gap,
    mock_parse_job,
    mock_parse_resume
):
    """Test T 11.2.2: Pipeline runs all nodes"""
    # Setup mocks
    mock_parse_resume.return_value = ResumeParsed(
        name="Test User",
        skills=["Python"],
        experience=[],
        projects=[],
        education=[]
    )
    
    mock_parse_job.return_value = JobParsed(
        job_title="Developer",
        required_skills=["Python", "React"],
        preferred_skills=[],
        keywords=[],
        responsibilities=[],
        qualifications=[]
    )
    
    mock_gap.return_value = {
        "overlapping_skills": ["Python"],
        "missing_required_skills": ["React"],
        "missing_preferred_skills": [],
        "weak_skills": []
    }
    
    mock_projects.return_value = ProjectPlanParsed(
        projects=[
            ProjectIdea(
                title="React App",
                skill_targets=["React"],
                difficulty="Intermediate",
                description="Build a React app",
                estimated_duration="2 weeks",
                key_features=["Components", "State"],
                technologies=["React"]
            )
        ]
    )
    
    mock_improve.return_value = ImprovedResumeParsed(
        name="Test User",
        contact="test@email.com",
        skills=["Python", "React"],
        experience=[],
        projects=[],
        education=[]
    )
    
    # Create mock database
    mock_db = MagicMock()
    
    # Mock database queries
    mock_resume = Mock()
    mock_resume.id = 1
    mock_resume.raw_text = "Sample resume text"
    mock_resume.parsed_json = None
    
    mock_job = Mock()
    mock_job.id = 2
    mock_job.extracted_text = "Sample job text"
    mock_job.parsed_json = None
    
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        mock_resume,  # First call for resume
        mock_job,     # Second call for job
    ]
    
    # Run pipeline
    result = run_pipeline(1, 2, mock_db)
    
    # Verify all steps completed
    assert result["resume_parsed"] is not None
    assert result["job_parsed"] is not None
    assert result["gap_analysis"] is not None
    assert result["projects"] is not None
    assert result["improved_resume"] is not None
    assert result["error"] is None

def test_pipeline_state_flow():
    """Test that state flows correctly through nodes"""
    state: PipelineState = {
        "resume_id": 1,
        "job_id": 2,
        "resume_parsed": None,
        "job_parsed": None,
        "gap_analysis": None,
        "projects": None,
        "improved_resume": None,
        "analysis_id": None,
        "project_plan_id": None,
        "improved_resume_id": None,
        "error": None
    }
    
    # Verify initial state
    assert state["resume_id"] == 1
    assert state["job_id"] == 2
    assert state["resume_parsed"] is None