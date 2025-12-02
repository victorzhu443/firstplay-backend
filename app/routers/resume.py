from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
import pdfplumber
from app.db import get_db
from app.models import Resume, JobDescription, GapAnalysis, ImprovedResume
from app.chains.resume_parser import parse_resume_text
from app.chains.resume_improver import improve_resume
from app.schemas import ResumeParsed, JobParsed
import json

router = APIRouter(prefix="/api/resume", tags=["resume"])

@router.post("/upload")
async def upload_resume(
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """
    Upload a PDF resume and extract raw text.
    
    Args:
        file: PDF file upload
    
    Returns:
        resume_id and preview of extracted text
    """
    # Validate file type
    if not file.filename.endswith('.pdf'):
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are supported"
        )
    
    if file.content_type != 'application/pdf':
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are supported"
        )
    
    # Extract text from PDF
    try:
        with pdfplumber.open(file.file) as pdf:
            text = ""
            for page in pdf.pages:
                text += page.extract_text() or ""
        
        if not text.strip():
            raise HTTPException(
                status_code=400,
                detail="Could not extract text from PDF. File may be corrupted or empty."
            )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error processing PDF: {str(e)}"
        )
    
    # Save to database
    resume = Resume(
        original_filename=file.filename,
        raw_text=text
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)
    
    # Return preview
    preview = text[:200] if len(text) > 200 else text
    
    return {
        "resume_id": resume.id,
        "raw_text_preview": preview
    }

@router.post("/parse")
async def parse_resume(
    resume_id: int,
    db: Session = Depends(get_db)
):
    """
    Parse resume text into structured format using LangChain.
    
    Args:
        resume_id: ID of the resume to parse
    
    Returns:
        Parsed resume data
    """
    # Load resume from database
    resume = db.query(Resume).filter(Resume.id == resume_id).first()
    
    if not resume:
        raise HTTPException(
            status_code=404,
            detail=f"Resume with id {resume_id} not found"
        )
    
    if not resume.raw_text:
        raise HTTPException(
            status_code=400,
            detail="Resume has no text to parse"
        )
    
    # Parse using LangChain
    try:
        parsed = parse_resume_text(resume.raw_text)
        
        # Save parsed JSON to database
        resume.parsed_json = parsed.model_dump_json()
        db.commit()
        db.refresh(resume)
        
        return {
            "resume_id": resume.id,
            "parsed_data": parsed.model_dump()
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error parsing resume: {str(e)}"
        )

@router.post("/improve")
async def improve_resume_endpoint(
    resume_id: int,
    job_id: int,
    db: Session = Depends(get_db)
):
    """
    Improve a resume by tailoring it to a specific job using Jake's template.
    Requires that both resume and job have been parsed, and gap analysis completed.
    
    Args:
        resume_id: ID of the resume
        job_id: ID of the job description
    
    Returns:
        Improved resume with Jake-style bullets (action verb + tech + metrics)
    """
    # Load resume
    resume = db.query(Resume).filter(Resume.id == resume_id).first()
    if not resume:
        raise HTTPException(
            status_code=404,
            detail=f"Resume with id {resume_id} not found"
        )
    
    if not resume.parsed_json:
        raise HTTPException(
            status_code=400,
            detail="Resume must be parsed first. Call POST /api/resume/parse"
        )
    
    # Load job description
    job = db.query(JobDescription).filter(JobDescription.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=404,
            detail=f"Job description with id {job_id} not found"
        )
    
    if not job.parsed_json:
        raise HTTPException(
            status_code=400,
            detail="Job description must be parsed first. Call POST /api/job/parse"
        )
    
    # Load gap analysis
    gap_analysis = db.query(GapAnalysis).filter(
        GapAnalysis.resume_id == resume_id,
        GapAnalysis.job_id == job_id
    ).first()
    
    if not gap_analysis:
        raise HTTPException(
            status_code=400,
            detail="Gap analysis must be completed first. Call POST /api/analyze"
        )
    
    # Parse JSON data
    try:
        resume_parsed = ResumeParsed.model_validate_json(resume.parsed_json)
        job_parsed = JobParsed.model_validate_json(job.parsed_json)
        gap_data = json.loads(gap_analysis.analysis_json)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error parsing stored JSON: {str(e)}"
        )
    
    # Improve the resume using LangChain
    try:
        improved = improve_resume(resume_parsed, job_parsed, gap_data)
        
        # Save to database
        improved_resume = ImprovedResume(
            resume_id=resume_id,
            job_id=job_id,
            improved_json=improved.model_dump_json()
        )
        db.add(improved_resume)
        db.commit()
        db.refresh(improved_resume)
        
        return {
            "improved_resume_id": improved_resume.id,
            "resume_id": resume_id,
            "job_id": job_id,
            "improved_resume": improved.model_dump()
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error improving resume: {str(e)}"
        )