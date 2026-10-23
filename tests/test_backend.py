"""Tool backend (backend/handler.py, backend/store.py): validation, ownership checks and idempotent refunds."""

import pytest
from botocore.exceptions import ClientError

from backend.store import Store

LALO = "CUST-001"  # owns orders 123 (249.99 USD), 321 and 789
SANDRO = "CUST-002"  # owns orders 456 (6000 USD), 500, 504 and 777
REFUND = {"order_id": "123", "amount": 50, "reason": "Arrived late", "idempotency_key": "operation-123"}


def error_code(response: dict) -> str | None:
    return response.get("error", {}).get("code")


def test_get_order_returns_the_requesters_order(call_tool):
    order = call_tool("get_order", requester_id=LALO, order_id="123")

    assert order["status"] == "DELAYED"
    assert "pk" not in order  # storage internals are not exposed


def test_get_customer_returns_the_requesters_account(call_tool):
    assert call_tool("get_customer", requester_id=LALO, customer_id=LALO)["email"] == "lalo@gmail.com"


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_order", {"requester_id": LALO, "order_id": "456"}),
        ("get_customer", {"requester_id": LALO, "customer_id": SANDRO}),
        ("refund_customer", {"requester_id": LALO, **REFUND, "order_id": "456"}),
        ("get_order", {"order_id": "123"}),  # not bound to a signed-in customer
    ],
)
def test_customers_cannot_access_other_customers_data(call_tool, tool, arguments):
    assert error_code(call_tool(tool, **arguments)) == "FORBIDDEN"


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_order", {"order_id": "abc"}),
        ("get_order", {"order_id": ""}),
        ("get_order", {"order_id": LALO}),  # wrong tool: a customer id
        ("get_customer", {"customer_id": "123"}),  # wrong tool: an order id
        ("refund_customer", {**REFUND, "amount": -5}),
        ("refund_customer", {**REFUND, "amount": 0}),
        ("refund_customer", {**REFUND, "amount": 10.005}),
        ("refund_customer", {**REFUND, "amount": "50"}),
        ("refund_customer", {**REFUND, "amount": True}),
        ("refund_customer", {**REFUND, "amount": 300}),  # more than the order total
        ("refund_customer", {**REFUND, "reason": " "}),
        ("refund_customer", {**REFUND, "idempotency_key": "not a valid key"}),
    ],
)
def test_invalid_arguments_are_rejected_as_not_retryable(call_tool, tool, arguments):
    response = call_tool(tool, requester_id=LALO, **arguments)

    assert error_code(response) == "INVALID_ARGUMENT"
    assert response["error"]["retryable"] is False


def test_unknown_order_and_unknown_tool(call_tool):
    assert error_code(call_tool("get_order", requester_id=LALO, order_id="999")) == "NOT_FOUND"
    assert error_code(call_tool("delete_order", requester_id=LALO, order_id="123")) == "UNKNOWN_TOOL"


def test_retried_refund_returns_the_existing_refund(call_tool, refund_count, table):
    first = call_tool("refund_customer", requester_id=LALO, **REFUND)
    retry = call_tool("refund_customer", requester_id=LALO, **REFUND)

    assert first["replayed"] is False
    assert retry["replayed"] is True
    assert retry["refund_id"] == first["refund_id"]
    assert refund_count() == 1
    assert table.get_item(Key={"pk": "ORDER#123"})["Item"]["refunded_total"] == 50


def test_idempotency_key_cannot_be_reused_for_a_different_refund(call_tool, refund_count):
    call_tool("refund_customer", requester_id=LALO, **REFUND)
    response = call_tool("refund_customer", requester_id=LALO, **{**REFUND, "amount": 60})

    assert error_code(response) == "IDEMPOTENCY_CONFLICT"
    assert refund_count() == 1


def test_refund_limit_cannot_be_bypassed_by_splitting_a_refund(call_tool):
    refund = {"order_id": "456", "reason": "Faulty laptop"}

    assert "refund_id" in call_tool("refund_customer", requester_id=SANDRO, amount=600, idempotency_key="a", **refund)
    over = call_tool("refund_customer", requester_id=SANDRO, amount=600, idempotency_key="b", **refund)
    assert error_code(over) == "REFUND_LIMIT_EXCEEDED"
    assert "refund_id" in call_tool("refund_customer", requester_id=SANDRO, amount=400, idempotency_key="c", **refund)


def test_data_store_failures_are_reported_as_retryable(call_tool, monkeypatch):
    def throttled(*args, **kwargs):
        raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "GetItem")

    monkeypatch.setattr(Store, "get_order", throttled)
    response = call_tool("get_order", requester_id=LALO, order_id="123")

    assert error_code(response) == "BACKEND_UNAVAILABLE"
    assert response["error"]["retryable"] is True
