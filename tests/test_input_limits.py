"""
Tests for input size limits.

Both ingest paths were unbounded. pdfplumber loads a whole document into
memory, and the job fetcher streamed any URL's body in full — either could
exhaust a worker. raw_html additionally stored every scraped page forever.
"""
import io

from fastapi.testclient import TestClient

from app.main import app
from app.routers.job import MAX_HTML_BYTES, MAX_STORED_HTML_BYTES
from app.routers.resume import MAX_UPLOAD_BYTES

client = TestClient(app)


# --- uploads ----------------------------------------------------------------

def test_oversized_pdf_is_rejected():
    oversized = b"%PDF-1.4\n" + b"0" * (MAX_UPLOAD_BYTES + 1)
    files = {"file": ("big.pdf", io.BytesIO(oversized), "application/pdf")}

    response = client.post("/api/resume/upload", files=files)

    assert response.status_code == 413
    assert "too large" in response.json()["detail"].lower()


def test_empty_upload_is_rejected():
    files = {"file": ("empty.pdf", io.BytesIO(b""), "application/pdf")}

    response = client.post("/api/resume/upload", files=files)

    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()


def test_normal_sized_pdf_is_not_rejected_for_size():
    """The limit must not reject an ordinary resume."""
    files = {"file": ("small.pdf", io.BytesIO(b"%PDF-1.4\nnot really"), "application/pdf")}

    response = client.post("/api/resume/upload", files=files)

    # Fails on parsing, not on size.
    assert response.status_code == 400
    assert "too large" not in response.json()["detail"].lower()


# --- job page fetching ------------------------------------------------------

def test_page_over_the_cap_is_rejected(monkeypatch):
    """A body larger than MAX_HTML_BYTES answers 413. The cap counts bytes
    as they arrive, so it holds whatever content-length claims."""
    from tests import fetch_fakes

    body = b"<html>" + b"x" * (MAX_HTML_BYTES + 1) + b"</html>"
    fetch_fakes.install(monkeypatch, fetch_fakes.html(body))

    result = client.post("/api/job/url", json={"url": "https://example.com/job"})

    assert result.status_code == 413
    assert "too large" in result.json()["detail"].lower()


def test_page_rejected_when_content_length_understates_the_body(monkeypatch):
    """Servers may omit or lie about content-length, so measure it too."""
    from tests import fetch_fakes

    body = b"<html>" + b"x" * (MAX_HTML_BYTES + 1) + b"</html>"
    fetch_fakes.install(monkeypatch, fetch_fakes.html(body, headers={"content-length": "15"}))

    result = client.post("/api/job/url", json={"url": "https://example.com/job"})

    assert result.status_code == 413


def test_stored_html_is_capped(monkeypatch):
    """raw_html is kept only for debugging extraction, so a prefix suffices."""
    from tests import fetch_fakes

    body = b"<html><body>" + b"jobtext " * 20000 + b"</body></html>"
    assert len(body) > MAX_STORED_HTML_BYTES
    assert len(body) < MAX_HTML_BYTES

    fetch_fakes.install(monkeypatch, fetch_fakes.html(body))
    result = client.post("/api/job/url", json={"url": "https://example.com/job"})

    assert result.status_code == 200

    from app.db import SessionLocal
    from app.models import JobDescription

    session = SessionLocal()
    try:
        stored = (
            session.query(JobDescription)
            .filter(JobDescription.id == result.json()["job_id"])
            .first()
        )
        assert len(stored.raw_html) <= MAX_STORED_HTML_BYTES
        # The text actually used downstream is kept in full.
        assert len(stored.extracted_text) > 0
    finally:
        session.close()
