"""Injected failures for the observability scenarios, active only when FAULT_INJECTION=true (set by deploy.py
for the demo stack). Each fault is bound to one demo order, so asking about that order triggers it."""

import os
import time
from typing import Any

from backend.errors import ToolError

SLOW_CARRIER_SECONDS = 10  # longer than the agent's per-tool timeout, see agent/main.py

ORDER_FAULTS = {
    "500": "http_500",  # carrier tracking API fails: retried with backoff
    "504": "timeout",  # carrier tracking hangs: tool timeout
    "777": "stale",  # tracking data asks for another lookup: LLM loop
    "321": "lost_response",  # payment provider fails after committing the refund: idempotent retry
}


def track_shipment(order: dict[str, Any]) -> dict[str, Any]:
    """Simulated carrier tracking lookup for get_order; returns the order unchanged unless a fault applies."""
    match _fault(order["order_id"]):
        case "http_500":
            raise ToolError("UPSTREAM_ERROR", "Carrier tracking API returned HTTP 500.", retryable=True)
        case "timeout":
            time.sleep(SLOW_CARRIER_SECONDS)
        case "stale":
            return {
                **order,
                "tracking_note": "Tracking data is still syncing. Call get_order again for the final status.",
            }
    return order


def confirm_refund(order_id: str) -> None:
    """Simulated payment provider confirmation, called after a new refund is committed."""
    if _fault(order_id) == "lost_response":
        # The refund is committed but its response is lost; only an idempotent retry can recover it.
        raise ToolError(
            "UPSTREAM_ERROR", "Payment provider returned HTTP 500 after accepting the refund.", retryable=True
        )


def _fault(order_id: str) -> str | None:
    return ORDER_FAULTS.get(order_id) if os.getenv("FAULT_INJECTION") == "true" else None
