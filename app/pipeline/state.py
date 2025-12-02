"""
LangGraph state definition for the FirstPlay Coach pipeline.
"""
from typing import TypedDict, Optional
from app.schemas import ResumeParsed, JobParsed, ProjectPlanParsed, ImprovedResumeParsed

class PipelineState(TypedDict):
    """
    State that flows through the LangGraph pipeline.
    Each node reads from and writes to this state.
    """
    # Input
    resume_id: int
    job_id: int
    
    # Intermediate data
    resume_parsed: Optional[ResumeParsed]
    job_parsed: Optional[JobParsed]
    gap_analysis: Optional[dict]
    
    # Output
    projects: Optional[ProjectPlanParsed]
    improved_resume: Optional[ImprovedResumeParsed]
    
    # Metadata
    analysis_id: Optional[int]
    project_plan_id: Optional[int]
    improved_resume_id: Optional[int]
    error: Optional[str]