"""DynamoDB data access for orders, customers and idempotent refunds (single table keyed by `pk`)."""

import contextlib
import hashlib
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

KEY = "pk"
ORDER_PREFIX = "ORDER#"
CUSTOMER_PREFIX = "CUSTOMER#"
REFUND_PREFIX = "REFUND#"
_INTERNAL_FIELDS = {KEY, "request_fingerprint"}


class IdempotencyConflict(Exception):
    """The idempotency key was already used for a different refund request."""


class RefundLimitExceeded(Exception):
    """The refund would take the order's refunded total above its refund cap."""


class Store:
    def __init__(self, table_name: str | None = None, dynamodb: Any = None) -> None:
        dynamodb = dynamodb or boto3.resource(
            "dynamodb", config=Config(retries={"mode": "standard", "max_attempts": 5})
        )
        self._table = dynamodb.Table(table_name or os.environ["TABLE_NAME"])

    def get_order(self, order_id: str) -> dict | None:
        return self._get(f"{ORDER_PREFIX}{order_id}")

    def get_customer(self, customer_id: str) -> dict | None:
        return self._get(f"{CUSTOMER_PREFIX}{customer_id}")

    def get_refund(self, order_id: str, idempotency_key: str) -> dict | None:
        return self._get(_refund_key(order_id, idempotency_key))

    def create_refund(
        self, order_id: str, amount: Decimal, reason: str, idempotency_key: str, refund_cap: Decimal
    ) -> tuple[dict, bool]:
        """Create a refund at most once per order and idempotency key, keeping the order within `refund_cap`.

        Returns (refund, created). Repeating the same request returns the stored refund with created=False;
        reusing the key for a different amount raises IdempotencyConflict, and a refund that would take the
        order's refunded_total above refund_cap raises RefundLimitExceeded.
        """
        fingerprint = _fingerprint(order_id, amount)
        refund = {
            KEY: _refund_key(order_id, idempotency_key),
            "refund_id": f"RF-{uuid.uuid4().hex[:12].upper()}",
            "idempotency_key": idempotency_key,
            "order_id": order_id,
            "amount": amount,
            "currency": "USD",
            "reason": reason,
            "status": "PROCESSED",
            "created_at": datetime.now(UTC).isoformat(),
            "request_fingerprint": fingerprint,
        }
        # One transaction creates the refund and adds it to the order's running total, so retries, concurrent
        # calls and a large refund split into several small ones can never refund the order beyond its cap.
        try:
            self._table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table.name,
                            "Item": refund,
                            "ConditionExpression": f"attribute_not_exists({KEY})",
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table.name,
                            "Key": {KEY: f"{ORDER_PREFIX}{order_id}"},
                            "UpdateExpression": "SET refunded_total = refunded_total + :amount",
                            "ConditionExpression": f"attribute_exists({KEY}) AND refunded_total <= :max_before",
                            "ExpressionAttributeValues": {":amount": amount, ":max_before": refund_cap - amount},
                        }
                    },
                ]
            )
            return _public(refund), True
        except ClientError as error:
            if error.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            refund_check, order_check = (check.get("Code") for check in error.response["CancellationReasons"])
            if "ConditionalCheckFailed" not in (refund_check, order_check):
                raise  # e.g. a TransactionConflict with a concurrent write, which is retryable
        if refund_check == "ConditionalCheckFailed":
            existing = self._item(refund[KEY])
            if existing["request_fingerprint"] != fingerprint:
                raise IdempotencyConflict(
                    f"Idempotency key {idempotency_key!r} was already used for a different refund."
                )
            return _public(existing), False
        raise RefundLimitExceeded(f"Refund of {amount} would exceed the refund cap of order {order_id}.")

    def seed(self, items: list[dict[str, Any]]) -> None:
        """Add the items that do not exist yet; existing orders keep their refunded_total, matching their refunds."""
        for item in items:
            with contextlib.suppress(self._table.meta.client.exceptions.ConditionalCheckFailedException):
                self._table.put_item(Item=item, ConditionExpression=f"attribute_not_exists({KEY})")

    def _item(self, pk: str) -> dict | None:
        return self._table.get_item(Key={KEY: pk}, ConsistentRead=True).get("Item")

    def _get(self, pk: str) -> dict | None:
        item = self._item(pk)
        return _public(item) if item else None


def create_table(client: Any, table_name: str) -> None:
    """Create the on-demand table if it does not exist and wait until it is active."""
    with contextlib.suppress(client.exceptions.ResourceInUseException):
        client.create_table(
            TableName=table_name,
            AttributeDefinitions=[{"AttributeName": KEY, "AttributeType": "S"}],
            KeySchema=[{"AttributeName": KEY, "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
    client.get_waiter("table_exists").wait(TableName=table_name)


def _refund_key(order_id: str, idempotency_key: str) -> str:
    """Idempotency keys are scoped to their order, so customers cannot collide on the same key."""
    return f"{REFUND_PREFIX}{order_id}#{idempotency_key}"


def _fingerprint(order_id: str, amount: Decimal) -> str:
    return hashlib.sha256(f"{order_id}|{amount:.2f}".encode()).hexdigest()


def _public(item: dict) -> dict:
    """JSON-serialisable copy of an item without storage-internal attributes."""
    return {key: _plain(value) for key, value in item.items() if key not in _INTERNAL_FIELDS}


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value
