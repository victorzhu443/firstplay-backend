from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_read_root():
    response = client.get("/")
    assert response.status_code == 200
    assert "message" in response.json()

def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

# --- F23: the OpenAPI UI is off unless asked for ----------------------------------

def test_openapi_ui_is_disabled_by_default():
    """The hosted /docs enumerated every legacy endpoint to anyone (audit
    2026-10-12, F23). FIRSTPLAY_DOCS is unset in the test environment."""
    import os
    assert not os.getenv("FIRSTPLAY_DOCS")
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/").json()["docs"] is None


def test_openapi_ui_can_be_switched_on(monkeypatch):
    monkeypatch.setenv("FIRSTPLAY_DOCS", "1")
    import importlib
    import app.main as main_module
    reloaded = importlib.reload(main_module)
    try:
        with TestClient(reloaded.app) as tc:
            assert tc.get("/openapi.json").status_code == 200
            assert tc.get("/").json()["docs"] == "/docs"
    finally:
        monkeypatch.delenv("FIRSTPLAY_DOCS")
        importlib.reload(main_module)


def test_malformed_input_from_any_route_is_a_400():
    """Every boundary validator raises `secprim.MalformedInput`; the app maps
    the type to 400 once, globally, so a new parser that forgets its own
    `except` still answers cleanly instead of 500. The handler is exercised
    directly: it is registered on the app and takes (request, error)."""
    import asyncio
    import json

    from secprim import MalformedInput

    from app.main import app as _app

    handler = _app.exception_handlers[MalformedInput]
    response = asyncio.run(handler(None, MalformedInput("form.questions[2]: expected an object, got str")))

    assert response.status_code == 400
    assert json.loads(response.body) == {"detail": "form.questions[2]: expected an object, got str"}
