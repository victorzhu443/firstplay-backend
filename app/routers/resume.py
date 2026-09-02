from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
import os
import pdfplumber
from app.db import get_db
from app.rate_limit import ingest_limit, llm_limit
from app.models import Resume, JobDescription, GapAnalysis, ImprovedResume
from app.chains.resume_parser import parse_resume_text
from app.chains.resume_improver import improve_resume
from app.schemas import ResumeParsed, JobParsed
import json

router = APIRouter(prefix="/api/resume", tags=["resume"])

# pdfplumber loads the whole document into memory, and the upload was
# previously unbounded: a large file could exhaust the worker. A resume that
# does not fit in this is not a resume.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def _upload_size(file: UploadFile) -> int:
    """Size of an upload without reading it into memory."""
    file.file.seek(0, os.SEEK_END)
    size = file.file.tell()
    file.file.seek(0)
    return size

# NOTE: these handlers are deliberately sync (`def`, not `async def`).
# They do blocking work — PDF extraction, synchronous SQLAlchemy queries, and
# LangChain's blocking `.invoke()` — and FastAPI runs sync handlers in a
# threadpool. Declared `async def`, the same code would run *on the event
# loop*, so one 30s LLM call would stall every other request in the worker,
# including /health.
@router.post("/upload", dependencies=[Depends(ingest_limit)])
def upload_resume(
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

    size = _upload_size(file)
    if size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File is too large ({size // 1024} KB). "
                f"Maximum is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
            )
        )

    if size == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")


    # Extract text from PDF
    try:
        with pdfplumber.open(file.file) as pdf:
            text = ""
            for page in pdf.pages:
                text += page.extract_text() or ""
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error processing PDF: {str(e)}"
        )

    # Checked outside the try: raised inside, this HTTPException was caught by
    # the `except Exception` above and re-wrapped, so the client saw the
    # doubly-nested "Error processing PDF: 400: Could not extract text...".
    if not text.strip():
        raise HTTPException(
            status_code=400,
            detail="Could not extract text from PDF. File may be corrupted or empty."
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

@router.post("/parse", dependencies=[Depends(llm_limit)])
def parse_resume(
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

@router.post("/improve", dependencies=[Depends(llm_limit)])
def improve_resume_endpoint(
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
    
    # Load gap analysis. Ordered explicitly: POST /api/analyze inserts a new
    # row every time it runs, so a re-analysed pair has several. Without an
    # order_by, SQLite returns the lowest rowid — the *oldest* analysis — so
    # improving a resume after re-running the analysis silently used stale
    # gap data.
    gap_analysis = db.query(GapAnalysis).filter(
        GapAnalysis.resume_id == resume_id,
        GapAnalysis.job_id == job_id
    ).order_by(GapAnalysis.created_at.desc(), GapAnalysis.id.desc()).first()
    
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