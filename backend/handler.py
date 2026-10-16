"""AgentCore Gateway Lambda target implementing the support tools.

The Gateway passes the tool arguments as the event and the tool name ("<target>___<tool>") in the client
context. Business errors are returned as {"error": {...}} so the model can react to them; `retryable` tells
the agent whether repeating the identical call may succeed.

Every call carries `requester_id`, the signed-in customer. The agent runtime sets it in code from the
authenticated invocation, overwriting anything the model sends, and the Gateway only accepts IAM-signed calls,
so a tool only ever reads or refunds the requester's own data, whatever the prompt says.
"""

import logging
import os
import re
import time
from collections.abc import Callable
from decimal import Decimal
from functools import cache
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from backend import faults
from backend.errors import ToolError
from backend.store import IdempotencyConflict, RefundLimitExceeded, Store

logger = logging.getLogger(__name__)

ORDER_ID = re.compile(r"\d{1,12}")
CUSTOMER_ID = re.compile(r"CUST-\d{3,9}")
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
MAX_REASON_LENGTH = 500


@cache
def _store() -> Store:
    return Store()


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    custom = context.client_context.custom if context.client_context else {}
    tool = custom.get("bedrockAgentCoreToolName", "").split("___")[-1]
    started = time.perf_counter()
    try:
        response, outcome = _dispatch(tool, event), "OK"
    except ToolError as error:
        response = {"error": {"code": error.code, "message": error.message, "retryable": error.retryable}}
        outcome = error.code
    logger.info(
        "tool_call",
        extra={
            "tool": tool,
            "outcome": outcome,
            "requester_id": event.get("requester_id"),
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "gateway_request_id": custom.get("bedrockAgentCoreAwsRequestId"),
            "mcp_message_id": custom.get("bedrockAgentCoreMcpMessageId"),
        },
    )
    return response


def _dispatch(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = TOOLS.get(tool)
    if handler is None:
        raise ToolError("UNKNOWN_TOOL", f"Unknown tool {tool!r}.")
    try:
        return handler(arguments)
    except (BotoCoreError, ClientError) as error:
        logger.exception("Data store call failed", extra={"tool": tool})
        raise ToolError("BACKEND_UNAVAILABLE", "Order data is temporarily unavailable.", retryable=True) from error


def get_order(args: dict[str, Any]) -> dict[str, Any]:
    return faults.track_shipment(_owned_order(args))


def get_customer(args: dict[str, Any]) -> dict[str, Any]:
    requester = _requester(args)
    customer_id = _required_str(args, "customer_id")
    if ORDER_ID.fullmatch(customer_id):
        raise ToolError("INVALID_ARGUMENT", f"{customer_id!r} is an order id, not a customer id. Use get_order.")
    if not CUSTOMER_ID.fullmatch(customer_id):
        raise ToolError("INVALID_ARGUMENT", "customer_id must look like 'CUST-001'.")
    if customer_id != requester:
        raise ToolError("FORBIDDEN", f"Customer {customer_id} is not the signed-in customer.")
    customer = _store().get_customer(customer_id)
    if customer is None:
        raise ToolError("NOT_FOUND", f"Customer {customer_id} does not exist.")
    return customer


def refund_customer(args: dict[str, Any]) -> dict[str, Any]:
    order = _owned_order(args)
    total = Decimal(str(order["total"]))
    amount = _amount(args, limit=total)
    reason = _required_str(args, "reason")
    if len(reason) > MAX_REASON_LENGTH:
        raise ToolError("INVALID_ARGUMENT", f"reason must be at most {MAX_REASON_LENGTH} characters.")
    key = _required_str(args, "idempotency_key")
    if not IDEMPOTENCY_KEY.fullmatch(key):
        raise ToolError("INVALID_ARGUMENT", "idempotency_key must be 1-64 letters, digits or . _ : - characters.")
    # REFUND_LIMIT is the limit AgentCore Policy applies to a single refund (set by deploy.py from the same
    # value); capping the order's refunded total at it stops a large refund from being split into allowed ones.
    refund_cap = min(total, Decimal(os.environ["REFUND_LIMIT"]))
    try:
        refund, created = _store().create_refund(order["order_id"], amount, reason, key, refund_cap)
    except IdempotencyConflict as error:
        raise ToolError("IDEMPOTENCY_CONFLICT", str(error)) from error
    except RefundLimitExceeded as error:
        raise ToolError(
            "REFUND_LIMIT_EXCEEDED",
            f"Order {order['order_id']} can be refunded at most {refund_cap:.2f} USD automatically and "
            f"{order.get('refunded_total', 0):.2f} USD was already refunded. A human agent must review this refund.",
        ) from error
    if created:
        faults.confirm_refund(order["order_id"])
    return {**refund, "replayed": not created}


TOOLS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "get_order": get_order,
    "get_customer": get_customer,
    "refund_customer": refund_customer,
}


def _requester(args: dict[str, Any]) -> str:
    requester = args.get("requester_id")
    if not isinstance(requester, str) or not CUSTOMER_ID.fullmatch(requester):
        raise ToolError("FORBIDDEN", "The request is not bound to a signed-in customer.")
    return requester


def _owned_order(args: dict[str, Any]) -> dict[str, Any]:
    requester = _requester(args)
    order = _load_order(_order_id(args))
    if order["customer_id"] != requester:
        raise ToolError("FORBIDDEN", f"Order {order['order_id']} does not belong to the signed-in customer.")
    return order


def _required_str(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolError("INVALID_ARGUMENT", f"{name} is required and must be a non-empty string.")
    return value.strip()


def _order_id(args: dict[str, Any]) -> str:
    order_id = _required_str(args, "order_id")
    if CUSTOMER_ID.fullmatch(order_id):
        raise ToolError("INVALID_ARGUMENT", f"{order_id!r} is a customer id, not an order id. Use get_customer.")
    if not ORDER_ID.fullmatch(order_id):
        raise ToolError("INVALID_ARGUMENT", "order_id must be numeric, for example '123'.")
    return order_id


def _load_order(order_id: str) -> dict[str, Any]:
    order = _store().get_order(order_id)
    if order is None:
        raise ToolError("NOT_FOUND", f"Order {order_id} does not exist.")
    return order


def _amount(args: dict[str, Any], limit: Decimal) -> Decimal:
    raw = args.get("amount")
    amount = Decimal(str(raw)) if isinstance(raw, int | float) and not isinstance(raw, bool) else None
    if amount is None or not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        raise ToolError("INVALID_ARGUMENT", "amount must be a positive number with at most 2 decimal places.")
    if amount > limit:
        raise ToolError("INVALID_ARGUMENT", f"amount exceeds the order total of {limit}.")
    return amount
