"""
LangGraph nodes for the FirstPlay Coach pipeline.
Each node performs one step of the workflow.

Nodes raise on failure; the `pipeline_node` wrapper records it in state and
halts the run. Previously each node caught its own exception and assigned to a
single shared `state["error"]`, which every later node then overwrote — see
NodeFailure in state.py.
"""
import functools
import json
import logging
from typing import Callable

from sqlalchemy.orm import Session

from app.analysis.gap_analysis import compute_gap
from app.chains.job_parser import parse_jd_text
from app.chains.project_generator import generate_projects
from app.chains.resume_improver import improve_resume
from app.chains.resume_parser import parse_resume_text
from app.exceptions import PipelineNodeError
from app.models import GapAnalysis, ImprovedResume, JobDescription, ProjectPlan, Resume
from app.pipeline.state import NodeFailure, PipelineState
from app.schemas import JobParsed, ResumeParsed

logger = logging.getLogger(__name__)


def pipeline_node(name: str) -> Callable:
    """
    Wrap a node body with uniform progress and failure recording.

    The wrapped body may raise freely: the wrapper converts the exception into
    a NodeFailure, appends it, and returns the state so the graph's conditional
    edges can halt cleanly instead of running the next node on absent data.

    Args:
        name: Node name, as recorded in completed_nodes/failures

    Returns:
        A decorator for a `(state, db) -> state` node body
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(state: PipelineState, db: Session) -> PipelineState:
            # Defensive: conditional edges should already have halted the run.
            if state.get("failures"):
                logger.info("Skipping %s; an upstream node already failed", name)
                return state

            try:
                state = fn(state, db)
            except Exception as e:
                logger.error(
                    "Pipeline node %s failed: %s: %s", name, type(e).__name__, e
                )
                failure: NodeFailure = {
                    "node": name,
                    "error_type": type(e).__name__,
                    "message": str(e),
                }
                # Rebuilt rather than appended in place, so the update is not
                # visible through any other reference to the previous list.
                state["failures"] = list(state.get("failures") or []) + [failure]
                # First failure wins; never overwritten by a later node.
                if not state.get("error"):
                    state["error"] = str(e)
                return state

            logger.info("Pipeline node %s completed", name)
            state["completed_nodes"] = list(state.get("completed_nodes") or []) + [name]
            return state

        return wrapper

    return decorator


@pipeline_node("parse_resume")
def parse_resume_node(state: PipelineState, db: Session) -> PipelineState:
    """
    Node 1: Parse resume from database
    """
    resume = db.query(Resume).filter(Resume.id == state["resume_id"]).first()

    if not resume:
        raise PipelineNodeError(f"Resume {state['resume_id']} not found")

    if not resume.raw_text:
        raise PipelineNodeError(
            f"Resume {state['resume_id']} has no text to parse"
        )

    if resume.parsed_json:
        state["resume_parsed"] = ResumeParsed.model_validate_json(resume.parsed_json)
    else:
        parsed = parse_resume_text(resume.raw_text)
        resume.parsed_json = parsed.model_dump_json()
        db.commit()
        db.refresh(resume)
        state["resume_parsed"] = parsed

    return state


@pipeline_node("parse_job")
def parse_job_node(state: PipelineState, db: Session) -> PipelineState:
    """
    Node 2: Parse job description from database
    """
    job = db.query(JobDescription).filter(JobDescription.id == state["job_id"]).first()

    if not job:
        raise PipelineNodeError(f"Job {state['job_id']} not found")

    if not job.extracted_text:
        raise PipelineNodeError(
            f"Job {state['job_id']} has no text to parse"
        )

    if job.parsed_json:
        state["job_parsed"] = JobParsed.model_validate_json(job.parsed_json)
    else:
        parsed = parse_jd_text(job.extracted_text)
        job.parsed_json = parsed.model_dump_json()
        db.commit()
        db.refresh(job)
        state["job_parsed"] = parsed

    return state


@pipeline_node("analyze_gap")
def analyze_gap_node(state: PipelineState, db: Session) -> PipelineState:
    """
    Node 3: Compute gap analysis
    """
    gap_result = compute_gap(state["resume_parsed"], state["job_parsed"])
    state["gap_analysis"] = gap_result

    gap_analysis = GapAnalysis(
        resume_id=state["resume_id"],
        job_id=state["job_id"],
        analysis_json=json.dumps(gap_result),
    )
    db.add(gap_analysis)
    db.commit()
    db.refresh(gap_analysis)

    state["analysis_id"] = gap_analysis.id

    return state


@pipeline_node("generate_projects")
def generate_projects_node(state: PipelineState, db: Session) -> PipelineState:
    """
    Node 4: Generate project ideas
    """
    project_plan = generate_projects(state["gap_analysis"])
    state["projects"] = project_plan

    project_plan_record = ProjectPlan(
        analysis_id=state["analysis_id"],
        plan_json=project_plan.model_dump_json(),
    )
    db.add(project_plan_record)
    db.commit()
    db.refresh(project_plan_record)

    state["project_plan_id"] = project_plan_record.id

    return state


@pipeline_node("improve_resume")
def improve_resume_node(state: PipelineState, db: Session) -> PipelineState:
    """
    Node 5: Improve resume
    """
    improved = improve_resume(
        state["resume_parsed"], state["job_parsed"], state["gap_analysis"]
    )
    state["improved_resume"] = improved

    improved_resume = ImprovedResume(
        resume_id=state["resume_id"],
        job_id=state["job_id"],
        improved_json=improved.model_dump_json(),
    )
    db.add(improved_resume)
    db.commit()
    db.refresh(improved_resume)

    state["improved_resume_id"] = improved_resume.id

    return state
