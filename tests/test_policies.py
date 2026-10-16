"""AgentCore Policy (policy/*.cedar) evaluated locally with the Cedar engine, independent of any prompt."""

import cedarpy
import pytest
from cedarpy import Decision

from agent.gateway import TOOL_SEPARATOR
from infra.config import REFUND_LIMIT, TARGET_NAME
from infra.policy import cedar_policies

GATEWAY = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/support-agent-test"
RUNTIME = "arn:aws:sts::123456789012:assumed-role/support-agent-runtime"
OPERATOR = "arn:aws:sts::123456789012:assumed-role/operator"
POLICIES = cedar_policies(
    {"GATEWAY_ARN": GATEWAY, "REFUND_LIMIT": REFUND_LIMIT, "RUNTIME_PRINCIPAL": RUNTIME, "OPERATOR_PRINCIPAL": OPERATOR}
)


def decide(tool: str, principal: str = RUNTIME, **tool_input) -> Decision:
    request = {
        "principal": f'AgentCore::IamEntity::"{principal}"',
        "action": f'AgentCore::Action::"{TARGET_NAME}{TOOL_SEPARATOR}{tool}"',
        "resource": f'AgentCore::Gateway::"{GATEWAY}"',
        "context": {"input": tool_input},
    }
    entities = [{"uid": {"type": "AgentCore::IamEntity", "id": principal}, "attrs": {"id": principal}, "parents": []}]
    result = cedarpy.is_authorized(request, "\n".join(POLICIES.values()), entities)
    assert not result.diagnostics.errors  # an evaluation error would also deny, for the wrong reason
    return result.decision


def amount(value: str) -> dict:
    return {"__extn": {"fn": "decimal", "arg": value}}


def test_every_placeholder_is_filled():
    assert POLICIES
    assert not any("{{" in statement for statement in POLICIES.values())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.01", Decision.Allow),
        ("1000.00", Decision.Allow),
        ("1000.01", Decision.Deny),
        ("5000.00", Decision.Deny),
    ],
)
def test_refunds_are_allowed_up_to_the_limit(value, expected):
    assert decide("refund_customer", amount=amount(value)) == expected


@pytest.mark.parametrize("tool", ["get_order", "get_customer"])
def test_lookups_are_allowed(tool):
    assert decide(tool) == Decision.Allow


def test_operator_may_call_the_tools():
    assert decide("refund_customer", principal=OPERATOR, amount=amount("10.00")) == Decision.Allow


@pytest.mark.parametrize("tool", ["get_order", "refund_customer"])
def test_other_iam_identities_are_denied(tool):
    intruder = "arn:aws:sts::123456789012:assumed-role/some-other-role"

    assert decide(tool, principal=intruder, amount=amount("10.00")) == Decision.Deny


def test_unknown_tools_are_denied():
    assert decide("delete_order") == Decision.Deny
