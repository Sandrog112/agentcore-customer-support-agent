"""Fixtures for the tool backend: an in-memory DynamoDB table (moto) seeded with backend/seed_data.json."""

import json
from collections.abc import Callable, Iterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from moto import mock_aws

from agent.gateway import TOOL_SEPARATOR
from backend import handler
from backend.store import REFUND_PREFIX, Store, create_table
from infra.config import REFUND_LIMIT, ROOT, TARGET_NAME

TABLE_NAME = "test-support-data"


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    # Fake credentials, so a unit test can never reach a real AWS account.
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("FAULT_INJECTION", raising=False)
    monkeypatch.setenv("TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("REFUND_LIMIT", REFUND_LIMIT)

    with mock_aws():
        dynamodb = boto3.resource("dynamodb")
        create_table(dynamodb.meta.client, TABLE_NAME)
        items = json.loads((ROOT / "backend" / "seed_data.json").read_text(), parse_float=Decimal)
        Store(TABLE_NAME, dynamodb).seed(items)
        handler._store.cache_clear()
        yield dynamodb.Table(TABLE_NAME)
        handler._store.cache_clear()


@pytest.fixture
def call_tool(table: Any) -> Callable[..., dict[str, Any]]:
    """Invoke the Lambda handler the way AgentCore Gateway does: arguments as the event, tool name in the context."""

    def call(tool: str, **arguments: Any) -> dict[str, Any]:
        custom = {"bedrockAgentCoreToolName": f"{TARGET_NAME}{TOOL_SEPARATOR}{tool}"}
        return handler.lambda_handler(arguments, SimpleNamespace(client_context=SimpleNamespace(custom=custom)))

    return call


@pytest.fixture
def refund_count(table: Any) -> Callable[[], int]:
    """Number of stored refunds, to prove that a retry never creates a second one."""
    return lambda: sum(item["pk"].startswith(REFUND_PREFIX) for item in table.scan()["Items"])
