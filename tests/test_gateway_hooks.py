"""Tool-call hooks of the agent (agent/gateway.py): requester binding, retries with backoff and the loop guard."""

import asyncio
import json
from typing import Any

import pytest
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

from agent.gateway import RefundIdempotency, RequesterBinding, ToolReliabilityHooks, is_retryable

TRANSIENT = {"error": {"code": "UPSTREAM_ERROR", "message": "HTTP 500", "retryable": True}}
PERMANENT = {"error": {"code": "INVALID_ARGUMENT", "message": "bad order_id", "retryable": False}}


def tool_use(tool_use_id: str = "call-1", name: str = "get_order", **arguments: Any) -> dict:
    return {"toolUseId": tool_use_id, "name": name, "input": arguments}


def tool_result(payload: Any, status: str = "success") -> dict:
    return {"toolUseId": "call-1", "status": status, "content": [{"text": json.dumps(payload)}]}


def registry_with(hooks: HookProvider) -> HookRegistry:
    registry = HookRegistry()
    registry.add_hook(hooks)
    return registry


def before_call(registry: HookRegistry, use: dict) -> BeforeToolCallEvent:
    event = BeforeToolCallEvent(agent=None, selected_tool=None, tool_use=use, invocation_state={})
    registry.invoke_callbacks(event)
    return event


def after_call(registry: HookRegistry, use: dict, result: dict) -> AfterToolCallEvent:
    event = AfterToolCallEvent(agent=None, selected_tool=None, tool_use=use, invocation_state={}, result=result)
    asyncio.run(registry.invoke_callbacks_async(event))
    return event


async def no_sleep(delay: float) -> None:
    pass


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (tool_result(TRANSIENT), True),
        (tool_result(PERMANENT), False),
        (tool_result({"order_id": "123"}), False),
        ({"status": "error", "content": [{"text": "Tool call timed out after 6s"}]}, True),
        ({"status": "error", "content": [{"text": "Internal server error"}]}, True),
        ({"status": "error", "content": [{"text": "Internal error: request denied by policy"}]}, False),
    ],
)
def test_is_retryable(result, expected):
    assert is_retryable(result) is expected


def test_requester_comes_from_the_signed_in_customer_not_the_model():
    use = tool_use(name="refund_customer", order_id="456", requester_id="CUST-002")  # injected through the prompt

    event = before_call(registry_with(RequesterBinding("CUST-001")), use)

    assert event.tool_use["input"]["requester_id"] == "CUST-001"
    assert use["input"]["requester_id"] == "CUST-002"  # the history keeps the model's own arguments


def refund_key(session_id: str = "session-1", name: str = "support-tools___refund_customer", **arguments: Any) -> str:
    arguments = {"order_id": "123", "amount": 10, **arguments}
    event = before_call(registry_with(RefundIdempotency(session_id)), tool_use(name=name, **arguments))
    return event.tool_use["input"].get("idempotency_key")


def test_refund_key_is_derived_from_the_request_not_chosen_by_the_model():
    key = refund_key(idempotency_key="made-up-1")

    assert key == refund_key(idempotency_key="made-up-2") == refund_key(amount=10.0)
    assert len({key, refund_key(amount=11), refund_key(order_id="321"), refund_key(session_id="session-2")}) == 4


def test_only_valid_refunds_get_a_key():
    assert refund_key(name="support-tools___get_order") is None
    assert refund_key(amount="ten") is None


def test_transient_failures_are_retried_with_exponential_backoff():
    delays = []

    async def record(delay: float) -> None:
        delays.append(delay)

    registry = registry_with(ToolReliabilityHooks(max_attempts=4, base_delay=1, max_delay=60, sleep=record))
    use = tool_use()

    retried = [after_call(registry, use, tool_result(TRANSIENT)).retry for _ in range(4)]

    assert retried == [True, True, True, False]  # gives up after max_attempts
    for attempt, delay in enumerate(delays):  # base_delay * 2**attempt, with 50-100 % jitter
        assert 2**attempt / 2 <= delay <= 2**attempt


def test_permanent_failures_are_not_retried_and_marked_as_failed():
    event = after_call(registry_with(ToolReliabilityHooks(sleep=no_sleep)), tool_use(), tool_result(PERMANENT))

    assert event.retry is False
    assert event.result["status"] == "error"  # so the tool span shows up as failed in traces


def test_loop_guard_cancels_repeated_identical_calls():
    registry = registry_with(ToolReliabilityHooks(max_identical_calls=2))

    events = [before_call(registry, tool_use(f"call-{n}", order_id="777")) for n in range(3)]

    assert [bool(event.cancel_tool) for event in events] == [False, False, True]


def test_retries_do_not_count_as_loop_iterations():
    registry = registry_with(ToolReliabilityHooks(max_identical_calls=1))

    events = [before_call(registry, tool_use("call-1", order_id="500")) for _ in range(3)]

    assert not any(event.cancel_tool for event in events)


def test_refund_recovered_by_a_retry_is_reported_as_new():
    registry = registry_with(ToolReliabilityHooks(sleep=no_sleep))
    use = tool_use(name="refund_customer", order_id="321", amount=20, idempotency_key="operation-123")

    lost = after_call(registry, use, tool_result(TRANSIENT))
    recovered = after_call(registry, use, tool_result({"refund_id": "RF-1", "replayed": True}))

    assert lost.retry is True
    assert json.loads(recovered.result["content"][0]["text"])["replayed"] is False
