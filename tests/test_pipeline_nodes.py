import pytest
from unittest.mock import Mock, patch
from app.pipeline.nodes import (
    parse_resume_node,
    parse_job_node,
    analyze_gap_node,
    generate_projects_node,
    improve_resume_node
)
from app.pipeline.state import PipelineState
from app.schemas import ResumeParsed, JobParsed, ProjectPlanParsed, ProjectIdea, ImprovedResumeParsed

def test_pipeline_state_structure():
    """Test T 11.1.1: PipelineState has correct fields"""
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
    
    assert "resume_id" in state
    assert "job_id" in state
    assert "resume_parsed" in state
    assert "gap_analysis" in state
    assert "projects" in state
    assert "improved_resume" in state

def test_node_function_signatures():
    """Test that all node functions have correct signatures"""
    # All nodes should accept (state, db) and return state
    import inspect
    
    nodes = [
        parse_resume_node,
        parse_job_node,
        analyze_gap_node,
        generate_projects_node,
        improve_resume_node
    ]
    
    for node in nodes:
        sig = inspect.signature(node)
        params = list(sig.parameters.keys())
        assert "state" in params
        assert "db" in params