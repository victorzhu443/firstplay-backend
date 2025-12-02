from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from app.db import get_db
from app.pipeline.graph import run_pipeline

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

class PipelineRequest(BaseModel):
    """Request model for running the full pipeline"""
    resume_id: int
    job_id: int

@router.post("/run")
async def run_full_pipeline(
    request: PipelineRequest,
    db: Session = Depends(get_db)
):
    """
    Run the complete FirstPlay Coach pipeline.
    This orchestrates all steps: parse resume, parse job, analyze gap, 
    generate projects, and improve resume.
    
    Args:
        request: PipelineRequest with resume_id and job_id
    
    Returns:
        Complete pipeline results including gap analysis, projects, and improved resume
    """
    try:
        # Run the pipeline
        result = run_pipeline(request.resume_id, request.job_id, db)
        
        # Format the response
        return {
            "resume_id": result["resume_id"],
            "job_id": result["job_id"],
            "analysis_id": result["analysis_id"],
            "project_plan_id": result["project_plan_id"],
            "improved_resume_id": result["improved_resume_id"],
            "gap_analysis": result["gap_analysis"],
            "projects": [p.model_dump() for p in result["projects"].projects] if result["projects"] else [],
            "improved_resume": result["improved_resume"].model_dump() if result["improved_resume"] else None
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Pipeline execution failed: {str(e)}"
        )