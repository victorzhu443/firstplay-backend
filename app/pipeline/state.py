"""
LangGraph state definition for the FirstPlay Coach pipeline.
"""
from typing import List, Optional, TypedDict

from app.schemas import ResumeParsed, JobParsed, ProjectPlanParsed, ImprovedResumeParsed


class NodeFailure(TypedDict):
    """Why a single node failed.

    Recorded in an append-only list rather than a single `error` string. With
    one shared string, each node that ran after a failure overwrote the
    previous message, so the reported error was always the *last* node's
    complaint about missing upstream data and the root cause was unrecoverable.
    """

    node: str
    error_type: str
    message: str


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

    # Progress tracking. `completed_nodes` is what the caller can safely render;
    # `failures` is append-only so the first (root) cause is never overwritten.
    completed_nodes: List[str]
    failures: List[NodeFailure]

    # Retained for callers that check a single error field. Derived from
    # `failures`: the message of the first failure, or None.
    error: Optional[str]


def initial_state(resume_id: int, job_id: int) -> PipelineState:
    """Build the starting state for a pipeline run."""
    return {
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
        "completed_nodes": [],
        "failures": [],
        "error": None,
    }
