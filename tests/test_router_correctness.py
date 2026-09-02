"""
Regression cover for three independent router defects.

All three were silent: the CORS wildcard matched nothing, the PDF error
handler garbled its own message, and the gap-analysis lookup returned the
wrong row. None raised, so none was visible without looking for it.
"""
import io
import json

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models import GapAnalysis

client = TestClient(app)


# --- A4: CORS wildcards ------------------------------------------------------

@pytest.mark.parametrize(
    "origin",
    [
        "https://firstplay-frontend.vercel.app",
        # Preview deployments: <project>-<hash>-<scope>.vercel.app. The old
        # literal "https://*.vercel.app" entry matched none of these, because
        # allow_origins is an exact string comparison.
        "https://firstplay-frontend-git-main-victor.vercel.app",
        "https://firstplay-abc123.vercel.app",
        "http://localhost:3000",
    ],
)
def test_allowed_origins_get_cors_headers(origin):
    response = client.get("/health", headers={"Origin": origin})

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == origin


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.com",
        "https://vercel.app.evil.com",
        "https://notvercel.app",
    ],
)
def test_unrelated_origins_are_not_allowed(origin):
    """The regex must not be so loose that it admits lookalike domains."""
    response = client.get("/health", headers={"Origin": origin})

    assert response.headers.get("access-control-allow-origin") != origin


# --- A5: the error handler that swallowed its own HTTPException --------------

def test_unreadable_pdf_reports_a_processing_error():
    files = {"file": ("broken.pdf", io.BytesIO(b"not a pdf at all"), "application/pdf")}

    response = client.post("/api/resume/upload", files=files)

    assert response.status_code == 400
    assert "Error processing PDF" in response.json()["detail"]


def test_empty_pdf_message_is_not_double_wrapped():
    """A valid PDF with no extractable text.

    Raised inside the try block, this HTTPException was caught by the
    surrounding `except Exception` and re-wrapped, so the client received
    "Error processing PDF: 400: Could not extract text from PDF...".
    """
    # Minimal valid PDF with one empty page.
    empty_pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
    )
    files = {"file": ("empty.pdf", io.BytesIO(empty_pdf), "application/pdf")}

    response = client.post("/api/resume/upload", files=files)
    detail = response.json()["detail"]

    assert response.status_code == 400
    assert "Could not extract text from PDF" in detail
    assert "Error processing PDF" not in detail, f"message was re-wrapped: {detail}"
    assert "400:" not in detail, f"status code leaked into the message: {detail}"


# --- A7: the stale gap analysis ---------------------------------------------

class _StubResume:
    id = 1
    parsed_json = '{"name":"U","skills":["Python"],"experience":[],"projects":[],"education":[]}'


class _StubJob:
    id = 2
    parsed_json = (
        '{"job_title":"Dev","required_skills":["React"],"preferred_skills":[],'
        '"keywords":[],"responsibilities":[],"qualifications":[]}'
    )


class _OrderRecordingQuery:
    """Records whether the gap-analysis lookup ordered its results."""

    def __init__(self, model, recorder):
        self.model = model
        self.recorder = recorder
        self.ordered = False

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        self.ordered = True
        self.recorder["ordered"] = True
        self.recorder["clauses"] = [str(a) for a in args]
        return self

    def first(self):
        if self.model is GapAnalysis:
            self.recorder["gap_lookup_ordered"] = self.ordered
            gap = type("G", (), {})()
            gap.analysis_json = json.dumps({"overlapping_skills": [], "missing_required_skills": []})
            return gap
        return _StubResume() if self.model.__name__ == "Resume" else _StubJob()


def test_gap_analysis_lookup_is_ordered_newest_first():
    """POST /api/analyze inserts a new row per run, so the pair has several.

    Without an explicit order_by, SQLite returns the lowest rowid — the
    oldest analysis — so /improve silently used stale gap data after a
    re-analysis.
    """
    recorder = {}

    class _Session:
        def query(self, model):
            return _OrderRecordingQuery(model, recorder)

        def add(self, obj):
            pass

        def commit(self):
            pass

        def refresh(self, obj):
            obj.id = 99

    app.dependency_overrides[get_db] = lambda: _Session()
    try:
        client.post("/api/resume/improve", params={"resume_id": 1, "job_id": 2})
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert recorder.get("gap_lookup_ordered") is True, (
        "the gap-analysis lookup ran without order_by, so it returns the "
        "oldest row rather than the newest"
    )
    assert any("DESC" in c.upper() for c in recorder.get("clauses", [])), (
        f"ordering is not newest-first: {recorder.get('clauses')}"
    )
