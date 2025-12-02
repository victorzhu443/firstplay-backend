"""
LangGraph workflow for FirstPlay Coach.
Orchestrates the full pipeline from resume + job to improved resume + projects.
"""
from langgraph.graph import StateGraph, END
from sqlalchemy.orm import Session
from app.pipeline.state import PipelineState
from app.pipeline.nodes import (
    parse_resume_node,
    parse_job_node,
    analyze_gap_node,
    generate_projects_node,
    improve_resume_node
)

def create_pipeline_graph(db: Session):
    """
    Create the LangGraph pipeline.
    
    Workflow:
    1. Parse Resume
    2. Parse Job Description
    3. Analyze Gap
    4. Generate Projects (parallel with step 5)
    5. Improve Resume (parallel with step 4)
    
    Args:
        db: Database session
    
    Returns:
        Compiled LangGraph
    """
    # Create the graph
    workflow = StateGraph(PipelineState)
    
    # Add nodes with database session binding
    workflow.add_node("parse_resume", lambda state: parse_resume_node(state, db))
    workflow.add_node("parse_job", lambda state: parse_job_node(state, db))
    workflow.add_node("analyze_gap", lambda state: analyze_gap_node(state, db))
    workflow.add_node("generate_projects", lambda state: generate_projects_node(state, db))
    workflow.add_node("improve_resume", lambda state: improve_resume_node(state, db))
    
    # Define the workflow edges
    workflow.set_entry_point("parse_resume")
    
    # Sequential flow for parsing and analysis
    workflow.add_edge("parse_resume", "parse_job")
    workflow.add_edge("parse_job", "analyze_gap")
    
    # After gap analysis, both generate_projects and improve_resume can run
    workflow.add_edge("analyze_gap", "generate_projects")
    workflow.add_edge("analyze_gap", "improve_resume")
    
    # Both parallel tasks end the workflow
    workflow.add_edge("generate_projects", END)
    workflow.add_edge("improve_resume", END)
    
    # Compile the graph
    return workflow.compile()

def run_pipeline(resume_id: int, job_id: int, db: Session) -> PipelineState:
    """
    Run the complete FirstPlay Coach pipeline.
    
    Args:
        resume_id: ID of the resume
        job_id: ID of the job description
        db: Database session
    
    Returns:
        Final pipeline state with all results
    
    Raises:
        Exception: If pipeline execution fails
    """
    # Create initial state
    initial_state: PipelineState = {
        "resume_id": resume_id,
        "job_id": job_id,
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
    
    # Create and run the graph
    graph = create_pipeline_graph(db)
    
    try:
        # Execute the pipeline
        final_state = graph.invoke(initial_state)
        
        # Check for errors
        if final_state.get("error"):
            raise Exception(final_state["error"])
        
        return final_state
    
    except Exception as e:
        raise Exception(f"Pipeline execution failed: {str(e)}")