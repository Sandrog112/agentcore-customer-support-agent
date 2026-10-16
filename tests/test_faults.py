"""Injected failures of the observability scenarios (backend/faults.py), active only with FAULT_INJECTION=true."""

import pytest

from agent.main import TOOL_TIMEOUT_SECONDS
from backend import faults

ALICE = "CUST-001"
BOB = "CUST-002"


@pytest.fixture
def fault_injection(table, monkeypatch):
    monkeypatch.setenv("FAULT_INJECTION", "true")  # after `table`, which clears it


def test_faults_are_off_by_default(call_tool):
    assert call_tool("get_order", requester_id=BOB, order_id="500")["order_id"] == "500"


@pytest.mark.usefixtures("fault_injection")
def test_http_500_is_reported_as_retryable(call_tool):
    error = call_tool("get_order", requester_id=BOB, order_id="500")["error"]

    assert error["code"] == "UPSTREAM_ERROR"
    assert error["retryable"] is True


@pytest.mark.usefixtures("fault_injection")
def test_slow_carrier_outlasts_the_agent_tool_timeout(call_tool, monkeypatch):
    sleeps = []
    monkeypatch.setattr(faults.time, "sleep", sleeps.append)

    call_tool("get_order", requester_id=BOB, order_id="504")

    assert sleeps == [faults.SLOW_CARRIER_SECONDS]
    assert faults.SLOW_CARRIER_SECONDS > TOOL_TIMEOUT_SECONDS


@pytest.mark.usefixtures("fault_injection")
def test_stale_tracking_data_asks_for_another_lookup(call_tool):
    assert "tracking_note" in call_tool("get_order", requester_id=BOB, order_id="777")


@pytest.mark.usefixtures("fault_injection")
def test_lost_refund_response_is_recovered_by_an_idempotent_retry(call_tool, refund_count):
    refund = {"order_id": "321", "amount": 20, "reason": "Damaged box", "idempotency_key": "operation-123"}

    lost = call_tool("refund_customer", requester_id=ALICE, **refund)
    retry = call_tool("refund_customer", requester_id=ALICE, **refund)

    assert lost["error"]["retryable"] is True  # committed, but the response was lost
    assert retry["replayed"] is True
    assert refund_count() == 1
