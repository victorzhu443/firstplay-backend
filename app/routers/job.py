from app.chains.job_parser import parse_jd_text
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
from app.db import get_db
from app.models import JobDescription
import httpx
from bs4 import BeautifulSoup
from starlette.concurrency import run_in_threadpool
from typing import Optional

router = APIRouter(prefix="/api/job", tags=["job"])

# The fetch was unbounded: any URL could stream arbitrarily much into the
# worker's memory. A job posting page is far smaller than this.
MAX_HTML_BYTES = 2 * 1024 * 1024

# raw_html stored the entire scraped page, forever, for every submission. It
# is kept only for debugging extraction, so a prefix is enough; the full text
# that actually gets used is stored separately in extracted_text.
MAX_STORED_HTML_BYTES = 64 * 1024

class JobUrlRequest(BaseModel):
    """Request model for job URL submission"""
    url: str
    
    @field_validator('url')
    @classmethod
    def validate_url(cls, v):
        """Validate that the URL is properly formatted"""
        if not v.startswith(('http://', 'https://')):
            raise ValueError('URL must start with http:// or https://')
        
        # Basic URL validation
        if len(v) < 10:
            raise ValueError('URL is too short to be valid')
        
        return v

async def fetch_html(url: str, timeout: int = 10) -> str:
    """
    Fetch HTML content from a URL using httpx.
    
    Args:
        url: The URL to fetch
        timeout: Request timeout in seconds
    
    Returns:
        str: HTML content
    
    Raises:
        HTTPException: If fetch fails
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=timeout, follow_redirects=True)
            response.raise_for_status()

            # Reject an oversized body before touching response.text, which
            # would decode the whole thing into memory first.
            declared = response.headers.get("content-length")
            if declared and int(declared) > MAX_HTML_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"Job posting page is too large "
                        f"({int(declared) // 1024} KB). "
                        f"Maximum is {MAX_HTML_BYTES // 1024} KB."
                    )
                )

            # Servers may omit or understate content-length, so cap regardless.
            if len(response.content) > MAX_HTML_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Job posting page exceeds {MAX_HTML_BYTES // 1024} KB."
                )

            return response.text
    except HTTPException:
        # Ours, raised above — must not be recaught and relabelled below.
        raise
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Request timed out while fetching the job posting"
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"Failed to fetch job posting: HTTP {e.response.status_code}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching job posting: {str(e)}"
        )

def extract_job_text(html: str) -> str:
    """
    Extract visible text from HTML, removing scripts, styles, and other noise.
    
    Args:
        html: Raw HTML content
    
    Returns:
        str: Extracted visible text
    """
    soup = BeautifulSoup(html, 'html.parser')
    
    # Remove script and style elements
    for script in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        script.decompose()
    
    # Get text
    text = soup.get_text(separator='\n')
    
    # Clean up whitespace
    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    text = '\n'.join(chunk for chunk in chunks if chunk)
    
    return text

@router.post("/url")
async def submit_job_url(
    request: JobUrlRequest,
    db: Session = Depends(get_db)
):
    """
    Submit a job posting URL.
    Fetches the page and extracts job description text.
    
    Args:
        request: JobUrlRequest with URL
    
    Returns:
        job_id and text preview
    """
    # Step 5.2: Fetch HTML
    html = await fetch_html(request.url)

    # Step 5.3: Extract job description text.
    # This handler stays `async def` because fetch_html genuinely awaits, but
    # BeautifulSoup parsing of a full page is CPU-bound and would block the
    # event loop, so it is pushed to the threadpool explicitly.
    extracted_text = await run_in_threadpool(extract_job_text, html)
    
    if not extracted_text.strip():
        raise HTTPException(
            status_code=400,
            detail="Could not extract any text from the job posting page"
        )
    
    # Save to database
    job_desc = JobDescription(
        url=request.url,
        raw_html=html[:MAX_STORED_HTML_BYTES],
        extracted_text=extracted_text
    )
    db.add(job_desc)
    db.commit()
    db.refresh(job_desc)
    
    # Return preview (first 200 chars)
    preview = extracted_text[:200] if len(extracted_text) > 200 else extracted_text
    
    return {
        "job_id": job_desc.id,
        "text_preview": preview
    }
class ManualJdRequest(BaseModel):
    """Request model for manual job description submission"""
    jd_text: str
    
    @field_validator('jd_text')
    @classmethod
    def validate_jd_text(cls, v):
        """Validate that JD text is not empty"""
        if not v or not v.strip():
            raise ValueError('Job description text cannot be empty')
        
        if len(v.strip()) < 50:
            raise ValueError('Job description text is too short (minimum 50 characters)')
        
        return v.strip()

@router.post("/description/manual")
def submit_manual_jd(
    request: ManualJdRequest,
    db: Session = Depends(get_db)
):
    """
    Submit a manually entered job description.
    Use this when you have the JD text but no URL.
    
    Args:
        request: ManualJdRequest with jd_text
    
    Returns:
        job_id and text preview
    """
    # Step 6.2: Store manual JD with url=null, raw_html=null
    job_desc = JobDescription(
        url=None,
        raw_html=None,
        extracted_text=request.jd_text
    )
    db.add(job_desc)
    db.commit()
    db.refresh(job_desc)
    
    # Return preview (first 200 chars)
    preview = request.jd_text[:200] if len(request.jd_text) > 200 else request.jd_text
    
    return {
        "job_id": job_desc.id,
        "text_preview": preview
    }


@router.post("/parse")
def parse_job(
    job_id: int,
    db: Session = Depends(get_db)
):
    """
    Parse a job description using LangChain.
    Extracts structured data from the job description text.
    
    Args:
        job_id: ID of the job description to parse
    
    Returns:
        Parsed job data in structured format
    """
    # Load the job description from database
    job = db.query(JobDescription).filter(JobDescription.id == job_id).first()
    
    if not job:
        raise HTTPException(
            status_code=404,
            detail=f"Job description with id {job_id} not found"
        )
    
    if not job.extracted_text:
        raise HTTPException(
            status_code=400,
            detail="Job description has no text to parse"
        )
    
    # Parse the job description text using LangChain
    try:
        parsed = parse_jd_text(job.extracted_text)
        
        # Save parsed JSON to database
        job.parsed_json = parsed.model_dump_json()
        db.commit()
        db.refresh(job)
        
        return {
            "job_id": job.id,
            "parsed_data": parsed.model_dump()
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error parsing job description: {str(e)}"
        )