"""Request validation of the agent entrypoint (agent/main.py): invalid requests never reach the model or tools."""

from types import SimpleNamespace

import pytest

from agent.main import _system_prompt, invoke

SESSION_ID = "session-00000000-0000-0000-0000-000000000000"


VALID = {"prompt": "Where is order 123?", "actor_id": "CUST-001", "operation_id": "operation-123"}


@pytest.mark.parametrize(
    ("payload", "session_id"),
    [
        ({**VALID, "prompt": None}, SESSION_ID),
        ({**VALID, "prompt": "   "}, SESSION_ID),
        ({**VALID, "actor_id": None}, SESSION_ID),
        ({**VALID, "actor_id": "../CUST-002"}, SESSION_ID),
        (VALID, None),
        ({**VALID, "operation_id": None}, SESSION_ID),
        ({**VALID, "operation_id": "../op"}, SESSION_ID),
    ],
)
def test_invalid_requests_are_rejected(payload, session_id):
    assert "error" in invoke(payload, SimpleNamespace(session_id=session_id))


def test_prompt_names_the_registered_gateway_tools():
    names = ("get_order", "get_customer", "refund_customer")
    tools = [SimpleNamespace(tool_name=f"support-tools___{name}") for name in names]

    prompt = _system_prompt("CUST-001", tools)

    for name in names:
        assert f"- support-tools___{name}:" in prompt
        assert f"- {name}:" not in prompt


def test_prompt_fails_when_a_tool_is_not_registered():
    with pytest.raises(KeyError):
        _system_prompt("CUST-001", [SimpleNamespace(tool_name="support-tools___get_order")])
