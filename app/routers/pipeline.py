from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from app.db import get_db
from app.rate_limit import pipeline_limit
from app.pipeline.graph import run_pipeline

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


class PipelineRequest(BaseModel):
    """Request model for running the full pipeline"""
    resume_id: int
    job_id: int


# Sync by design: a pipeline run is five sequential nodes with up to four
# blocking LLM calls. FastAPI runs this in a threadpool, so a long run cannot
# stall the event loop and starve every other request in the worker.
@router.post("/run", dependencies=[Depends(pipeline_limit)])
def run_full_pipeline(
    request: PipelineRequest,
    db: Session = Depends(get_db)
):
    """
    Run the complete FirstPlay Coach pipeline.
    This orchestrates all steps: parse resume, parse job, analyze gap,
    generate projects, and improve resume.

    A node failure halts the run but does not discard the steps that already
    succeeded. The response always has the same shape; `status` says how far
    it got:

      - "complete": all five nodes succeeded
      - "partial":  some nodes succeeded, then one failed (HTTP 200, so the
                    frontend can render what exists)
      - "failed":   nothing was produced (HTTP 502)

    Args:
        request: PipelineRequest with resume_id and job_id

    Returns:
        Pipeline results, plus which steps completed and why any step failed
    """
    try:
        result = run_pipeline(request.resume_id, request.job_id, db)
    except Exception as e:
        # Reaching here means the graph itself failed to execute; an expected
        # node failure is reported in the state, not raised.
        raise HTTPException(
            status_code=500,
            detail=f"Pipeline execution failed: {str(e)}"
        )

    # .get() with defaults: callers (and tests) may supply a state that
    # predates the progress-tracking fields.
    failures = result.get("failures") or []
    completed = result.get("completed_nodes") or []

    if not failures:
        status = "complete"
    elif completed:
        status = "partial"
    else:
        status = "failed"

    payload = {
        "status": status,
        "resume_id": result["resume_id"],
        "job_id": result["job_id"],
        "completed_steps": completed,
        "failures": failures,
        "analysis_id": result["analysis_id"],
        "project_plan_id": result["project_plan_id"],
        "improved_resume_id": result["improved_resume_id"],
        "gap_analysis": result["gap_analysis"],
        "projects": (
            [p.model_dump() for p in result["projects"].projects]
            if result["projects"] else []
        ),
        "improved_resume": (
            result["improved_resume"].model_dump()
            if result["improved_resume"] else None
        ),
    }

    if status == "failed":
        # Nothing usable was produced, so this is not a success. Same body
        # shape, so the frontend keeps a single code path.
        raise HTTPException(status_code=502, detail=payload)

    return payload
