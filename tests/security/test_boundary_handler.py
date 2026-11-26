"""The global boundary-failure handler (RC-1 adoption in `app/main.py`).

Every boundary validator raises one type, `secprim.MalformedInput` (the
adapters' `MalformedPayload` is an alias of it), and `app.main` maps that
type to a 400 once. The per-router `except MalformedPayload` blocks add their
own context, but the global handler is the backstop for any parser or router
that forgets one: without it a malformed payload is a 500 with a traceback
in the log, the exact failure audit F11 recorded.

Found by mutation testing (`security/mutations.yaml`,
`P1-malformed-input-not-mapped-to-400`): unregistering the handler left every
test green, because each covered route also carried its own except block.
"""
import asyncio

from fastapi.testclient import TestClient
from secprim import MalformedInput

from app.autofill.greenhouse import MalformedPayload
from app.main import app

#: A route with no `except MalformedPayload` of its own, so what the caller
#: sees is decided by the global handler alone. Registered once, hidden from
#: the schema, under a path no real client uses.
_PATH = "/__tests__/security/malformed-boundary"


async def _parser_that_forgot_its_except_block(path: str = "form.questions[2].fields"):
    raise MalformedPayload(path)


if not any(getattr(r, "path", None) == _PATH for r in app.routes):
    app.add_api_route(_PATH, _parser_that_forgot_its_except_block, include_in_schema=False)

client = TestClient(app, raise_server_exceptions=False)


def test_malformed_input_handler_is_registered_for_the_shared_type():
    """The handler must be keyed on `secprim.MalformedInput` itself: that is
    the type `as_dict`/`as_list`/`finite_float` raise, and the alias the
    adapters use is the same class."""
    assert MalformedPayload is MalformedInput
    assert MalformedInput in app.exception_handlers, "no global MalformedInput -> 400 handler"


def test_malformed_input_from_a_route_without_its_own_except_is_a_400_not_a_500():
    response = client.get(_PATH)

    assert response.status_code == 400, response.text
    body = response.json()
    assert body == {"detail": "form.questions[2].fields"}, body


def test_handler_returns_the_node_path_and_nothing_from_the_payload():
    """The message names the offending node, which is safe to return; it
    never echoes payload content, so a hostile value cannot be reflected."""
    handler = app.exception_handlers[MalformedInput]

    response = asyncio.run(handler(None, MalformedInput("payload.controls[0]")))

    assert response.status_code == 400
    assert response.body == b'{"detail":"payload.controls[0]"}'
