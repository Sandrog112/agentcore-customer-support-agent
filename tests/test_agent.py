"""Request validation of the agent entrypoint (agent/main.py): invalid requests never reach the model or tools."""

from types import SimpleNamespace

import pytest

from agent.main import invoke

SESSION_ID = "session-00000000-0000-0000-0000-000000000000"


@pytest.mark.parametrize(
    ("payload", "session_id"),
    [
        ({"actor_id": "CUST-001"}, SESSION_ID),
        ({"prompt": "   ", "actor_id": "CUST-001"}, SESSION_ID),
        ({"prompt": "Where is order 123?"}, SESSION_ID),
        ({"prompt": "Where is order 123?", "actor_id": "../CUST-002"}, SESSION_ID),
        ({"prompt": "Where is order 123?", "actor_id": "CUST-001"}, None),
    ],
)
def test_invalid_requests_are_rejected(payload, session_id):
    assert "error" in invoke(payload, SimpleNamespace(session_id=session_id))
