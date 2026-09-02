"""
LangGraph workflow for FirstPlay Coach.
Orchestrates the full pipeline from resume + job to improved resume + projects.
"""
import logging
from typing import Callable

from langgraph.graph import StateGraph, END
from sqlalchemy.orm import Session

from app.pipeline.state import PipelineState, initial_state
from app.pipeline.nodes import (
    parse_resume_node,
    parse_job_node,
    analyze_gap_node,
    generate_projects_node,
    improve_resume_node,
)

logger = logging.getLogger(__name__)

# The pipeline in order. Each step depends on every step before it, so a
# failure anywhere means nothing after it can produce a meaningful result.
NODE_SEQUENCE = [
    ("parse_resume", parse_resume_node),
    ("parse_job", parse_job_node),
    ("analyze_gap", analyze_gap_node),
    ("generate_projects", generate_projects_node),
    ("improve_resume", improve_resume_node),
]


def _halt_on_failure(next_node: str) -> Callable[[PipelineState], str]:
    """
    Build a router that proceeds to `next_node`, or stops the run if a node
    has already failed.

    Without this the graph's edges were unconditional: a node could fail,
    leave `gap_analysis` as None, and the next node would still run and crash
    on `None.get(...)` — replacing the real error with a confusing
    AttributeError from a node that was never the problem.
    """

    def route(state: PipelineState) -> str:
        if state.get("failures"):
            logger.info("Halting pipeline before %s; a node has failed", next_node)
            return END
        return next_node

    return route


def create_pipeline_graph(db: Session):
    """
    Create the LangGraph pipeline.

    Workflow:
    1. Parse Resume
    2. Parse Job Description
    3. Analyze Gap
    4. Generate Projects
    5. Improve Resume

    Each step is followed by a conditional edge that ends the run if the step
    failed, so partial results are preserved instead of being overwritten.

    Args:
        db: Database session

    Returns:
        Compiled LangGraph
    """
    workflow = StateGraph(PipelineState)

    # Add nodes with database session binding
    for name, node in NODE_SEQUENCE:
        workflow.add_node(name, (lambda n: lambda state: n(state, db))(node))

    workflow.set_entry_point(NODE_SEQUENCE[0][0])

    # Sequential edges, each guarded by the halt-on-failure router.
    for (name, _), (next_name, _) in zip(NODE_SEQUENCE, NODE_SEQUENCE[1:]):
        workflow.add_conditional_edges(
            name, _halt_on_failure(next_name), [next_name, END]
        )

    workflow.add_edge(NODE_SEQUENCE[-1][0], END)

    return workflow.compile()


def run_pipeline(resume_id: int, job_id: int, db: Session) -> PipelineState:
    """
    Run the complete FirstPlay Coach pipeline.

    Does not raise when a node fails. A failed node is recorded in
    `state["failures"]` and the run halts, leaving every artifact produced
    before the failure intact for the caller to return. Raising here was what
    turned any single node failure into a blank 500 with no partial results.

    Args:
        resume_id: ID of the resume
        job_id: ID of the job description
        db: Database session

    Returns:
        Final pipeline state. Inspect `failures` to see whether the run
        completed; `completed_nodes` lists the steps that succeeded.

    Raises:
        Exception: Only if the graph itself fails to execute, which indicates
            a bug rather than an expected node failure.
    """
    graph = create_pipeline_graph(db)
    final_state = graph.invoke(initial_state(resume_id, job_id))

    if final_state.get("failures"):
        logger.warning(
            "Pipeline finished with %d failure(s); completed: %s",
            len(final_state["failures"]),
            final_state.get("completed_nodes"),
        )

    return final_state
