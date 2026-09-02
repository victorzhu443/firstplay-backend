"""
Tests for the liveness/readiness split.

The platform's health check gates a deploy: if it passes, the new build
replaces the running one. Reporting healthy while the database is unusable
therefore promotes a broken build over a working one — which is how a
deployment with no schema went live and returned 500 on every request that
touched a table, while /health answered 200 throughout.

/health answers whenever the process is up, which is what makes a broken
deployment diagnosable from outside. /ready answers whether the app can
actually serve, and is what the platform should check.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_is_liveness_only():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_stays_up_when_the_database_is_gone():
    """This is the property that made the outage diagnosable remotely."""
    with patch("app.main._missing_tables", side_effect=OSError("database is gone")):
        response = client.get("/health")

    assert response.status_code == 200


def test_ready_is_200_when_the_schema_is_present():
    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["tables"] == 5


def test_ready_is_503_when_tables_are_missing():
    """The exact production state: process up, schema absent."""
    with patch("app.main._missing_tables", return_value={"resumes", "job_descriptions"}):
        response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["reason"] == "schema incomplete"
    assert body["missing_tables"] == ["job_descriptions", "resumes"]


def test_ready_is_503_when_the_database_is_unreachable():
    with patch("app.main._missing_tables", side_effect=OSError("connection refused")):
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["reason"] == "database unreachable"


def test_ready_names_what_is_missing():
    """A 503 that doesn't say why costs a dashboard round-trip to diagnose."""
    with patch("app.main._missing_tables", return_value={"gap_analyses"}):
        body = client.get("/ready").json()

    assert "gap_analyses" in body["missing_tables"]


def test_readiness_does_not_leak_the_connection_string():
    """A managed Postgres URL carries the password."""
    with patch(
        "app.main._missing_tables",
        side_effect=OSError("could not connect to postgresql://user:hunter2@host/db"),
    ):
        body = client.get("/ready").json()

    assert "hunter2" not in str(body)
    assert "postgresql://" not in str(body)


def test_ready_is_not_rate_limited():
    """The platform polls it; limiting it would fail deploys spuriously."""
    statuses = {client.get("/ready").status_code for _ in range(70)}

    assert statuses == {200}
