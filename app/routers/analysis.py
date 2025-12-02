from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from app.db import get_db
from app.models import Resume, JobDescription, GapAnalysis, ProjectPlan
from app.schemas import ResumeParsed, JobParsed
from app.analysis.gap_analysis import compute_gap
from app.chains.project_generator import generate_projects
import json

router = APIRouter(prefix="/api", tags=["analysis"])

class AnalyzeRequest(BaseModel):
    """Request model for gap analysis"""
    resume_id: int
    job_id: int

@router.post("/analyze")
async def analyze(
    request: AnalyzeRequest,
    db: Session = Depends(get_db)
):
    """
    Run gap analysis between a resume and job description.
    Both must be parsed first (have parsed_json).
    
    Args:
        request: AnalyzeRequest with resume_id and job_id
    
    Returns:
        Gap analysis results (overlapping/missing skills)
    """
    # Load resume
    resume = db.query(Resume).filter(Resume.id == request.resume_id).first()
    if not resume:
        raise HTTPException(
            status_code=404,
            detail=f"Resume with id {request.resume_id} not found"
        )
    
    if not resume.parsed_json:
        raise HTTPException(
            status_code=400,
            detail="Resume must be parsed first. Call POST /api/resume/parse"
        )
    
    # Load job description
    job = db.query(JobDescription).filter(JobDescription.id == request.job_id).first()
    if not job:
        raise HTTPException(
            status_code=404,
            detail=f"Job description with id {request.job_id} not found"
        )
    
    if not job.parsed_json:
        raise HTTPException(
            status_code=400,
            detail="Job description must be parsed first. Call POST /api/job/parse"
        )
    
    # Parse the JSON strings into Pydantic models
    try:
        resume_parsed = ResumeParsed.model_validate_json(resume.parsed_json)
        job_parsed = JobParsed.model_validate_json(job.parsed_json)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error parsing stored JSON: {str(e)}"
        )
    
    # Compute gap analysis
    gap_result = compute_gap(resume_parsed, job_parsed)
    
    # Save to database
    gap_analysis = GapAnalysis(
        resume_id=request.resume_id,
        job_id=request.job_id,
        analysis_json=json.dumps(gap_result)
    )
    db.add(gap_analysis)
    db.commit()
    db.refresh(gap_analysis)
    
    return {
        "analysis_id": gap_analysis.id,
        "resume_id": request.resume_id,
        "job_id": request.job_id,
        "gap_analysis": gap_result
    }
@router.post("/projects")
async def generate_project_ideas(
    analysis_id: int,
    db: Session = Depends(get_db)
):
    """
    Generate project ideas based on gap analysis.
    The gap analysis must be completed first.
    
    Args:
        analysis_id: ID of the gap analysis
    
    Returns:
        List of project ideas
    """
    # Load the gap analysis
    analysis = db.query(GapAnalysis).filter(GapAnalysis.id == analysis_id).first()
    
    if not analysis:
        raise HTTPException(
            status_code=404,
            detail=f"Gap analysis with id {analysis_id} not found"
        )
    
    # Parse the gap analysis JSON
    try:
        gap_data = json.loads(analysis.analysis_json)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error parsing gap analysis: {str(e)}"
        )
    
    # Generate project ideas using LangChain
    try:
        project_plan = generate_projects(gap_data)
        
        # Save to database
        project_plan_record = ProjectPlan(
            analysis_id=analysis_id,
            plan_json=project_plan.model_dump_json()
        )
        db.add(project_plan_record)
        db.commit()
        db.refresh(project_plan_record)
        
        return {
            "project_plan_id": project_plan_record.id,
            "analysis_id": analysis_id,
            "projects": [p.model_dump() for p in project_plan.projects]
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error generating projects: {str(e)}"
        )