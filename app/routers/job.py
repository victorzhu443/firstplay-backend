from app.chains.job_parser import parse_jd_text
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
from app.db import get_db
from app.rate_limit import ingest_limit, llm_limit
from app.models import JobDescription
import httpx
import ipaddress
import socket
from bs4 import BeautifulSoup
from secprim import MalformedInput, TooLarge
from secprim.safe_fetch import TooManyRedirects, UnsafeDestination, safe_fetch
from starlette.concurrency import run_in_threadpool
from typing import Optional
from urllib.parse import urlparse

router = APIRouter(prefix="/api/job", tags=["job"])

# The fetch was unbounded: any URL could stream arbitrarily much into the
# worker's memory. A job posting page is far smaller than this.
MAX_HTML_BYTES = 2 * 1024 * 1024

# raw_html stored the entire scraped page, forever, for every submission. It
# is kept only for debugging extraction, so a prefix is enough; the full text
# that actually gets used is stored separately in extracted_text.
MAX_STORED_HTML_BYTES = 64 * 1024

# This endpoint fetches a URL chosen by the caller, which makes the server a
# proxy into whatever it can reach: localhost, the private network, and the
# cloud metadata service on 169.254.169.254 that hands out credentials.
MAX_REDIRECTS = 5

# The two seams the fetch has to the outside world, kept as module attributes
# so the test suite can run the real fetch path offline: a getaddrinfo-
# compatible resolver and an httpx transport (None means the default one).
# `safe_fetch` and the pre-check below both read these at call time.
RESOLVER = socket.getaddrinfo
TRANSPORT = None


def _resolved_addresses(hostname: str):
    """Every address `hostname` resolves to, as ip_address objects."""
    try:
        infos = RESOLVER(hostname, None)
    except (socket.gaierror, OSError) as e:
        raise HTTPException(
            status_code=400, detail=f"Could not resolve host: {hostname}"
        ) from e

    return [ipaddress.ip_address(info[4][0]) for info in infos]


def assert_public_url(url: str) -> None:
    """
    Reject a URL that resolves to anything other than a public address.

    This is the fast pre-check on the submitted URL, so the common mistake
    (a localhost or intranet link) gets a specific message. It is not what
    keeps the fetch safe: resolution here and connection in the HTTP client
    would be separate lookups, and a DNS answer that changes between them
    would get through. The fetch itself goes through `secprim.safe_fetch`,
    which resolves once, connects to the address it validated, and repeats
    that check on every redirect hop.

    Args:
        url: The absolute URL about to be fetched

    Raises:
        HTTPException: If the host resolves to a non-public address
    """
    hostname = urlparse(url).hostname

    if not hostname:
        raise HTTPException(status_code=400, detail="URL has no host")

    for address in _resolved_addresses(hostname):
        if not address.is_global or address.is_multicast:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Job posting URL must point to a public address; "
                    f"{hostname} resolves to {address}."
                ),
            )

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

def _fetch_pinned(url: str, timeout: float):
    """The blocking fetch. Resolves once, connects to the validated address
    with the real Host header and SNI, re-checks each redirect hop, and reads
    the body under MAX_HTML_BYTES. Runs in the threadpool from the handler."""
    return safe_fetch(
        url,
        max_bytes=MAX_HTML_BYTES,
        max_redirects=MAX_REDIRECTS,
        timeout=timeout,
        resolver=RESOLVER,
        transport=TRANSPORT,
    )


async def fetch_html(url: str, timeout: int = 10) -> str:
    """
    Fetch HTML content from a URL.

    The connection is pinned to the address that was validated (see
    `_fetch_pinned`), which is what closes the gap between checking a host
    and connecting to it. Failures map to the same statuses as before:
    400 for a destination the fetcher must not reach, 413 for an oversized
    body, 502 for a broken redirect chain, 504 for a timeout, and the
    upstream status for an HTTP error.

    Args:
        url: The URL to fetch
        timeout: Request timeout in seconds

    Returns:
        str: HTML content

    Raises:
        HTTPException: If fetch fails
    """
    # The pre-check gives the common case (a link into localhost or the
    # intranet) its specific message before any connection is attempted.
    assert_public_url(url)

    try:
        # safe_fetch is synchronous; the handler stays async, so the blocking
        # fetch is pushed to the threadpool the same way the parse is.
        response = await run_in_threadpool(_fetch_pinned, url, timeout)
    except UnsafeDestination as e:
        if "without a Location" in str(e):
            raise HTTPException(
                status_code=502,
                detail="Job posting redirected without a destination"
            )
        # A redirect hop that resolved to a non-public address.
        raise HTTPException(
            status_code=400,
            detail=f"Job posting URL must point to a public address; {e}.",
        )
    except TooManyRedirects:
        raise HTTPException(
            status_code=502,
            detail=f"Job posting exceeded {MAX_REDIRECTS} redirects"
        )
    except TooLarge:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Job posting page is too large. "
                f"Maximum is {MAX_HTML_BYTES // 1024} KB."
            )
        )
    except MalformedInput as e:
        raise HTTPException(status_code=400, detail=str(e))
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Request timed out while fetching the job posting"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching job posting: {str(e)}"
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Failed to fetch job posting: HTTP {response.status_code}"
        )

    return response.text

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

@router.post("/url", dependencies=[Depends(ingest_limit)])
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

@router.post("/description/manual", dependencies=[Depends(ingest_limit)])
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


@router.post("/parse", dependencies=[Depends(llm_limit)])
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