"""Business tools through AgentCore Gateway (MCP), and the reliability rules for calling them.

The Gateway uses AWS_IAM inbound auth: every MCP request is SigV4-signed with the caller's temporary IAM
credentials (the runtime execution role on AgentCore Runtime), so no secrets or tokens are stored anywhere.
"""

import asyncio
import hashlib
import json
import logging
import random
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Generator
from datetime import timedelta
from typing import Any

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from opentelemetry import trace
from strands.hooks import AfterToolCallEvent, BeforeInvocationEvent, BeforeToolCallEvent, HookProvider, HookRegistry
from strands.tools.mcp import MCPAgentTool, MCPClient

logger = logging.getLogger(__name__)

TOOL_SEPARATOR = "___"  # Gateway tool names are "<target>___<tool>"
REQUESTER_FIELD = "requester_id"  # tool argument the backend authorizes against, see backend/handler.py
IDEMPOTENCY_FIELD = "idempotency_key"  # tool argument that makes refunds idempotent, see backend/store.py
_NOT_RETRYABLE = re.compile(r"denied|not authori[sz]ed|unauthori[sz]ed|forbidden|policy", re.IGNORECASE)
_TRANSIENT = re.compile(
    r"timed? ?out|throttl|too many requests|temporarily unavailable|service unavailable|internal (server )?error",
    re.IGNORECASE,
)


class IamAuth(httpx.Auth):
    """httpx auth flow that SigV4-signs each request with the current IAM credentials."""

    requires_request_body = True

    def __init__(self, region: str, service: str = "bedrock-agentcore") -> None:
        credentials = boto3.Session().get_credentials()
        if credentials is None:
            raise RuntimeError("No AWS credentials available to sign Gateway requests.")
        self._credentials = credentials
        self._region = region
        self._service = service

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        # The Connection header may be rewritten in transit, so it must not be part of the signature.
        headers = {name: value for name, value in request.headers.items() if name.lower() != "connection"}
        aws_request = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=headers)
        SigV4Auth(self._credentials.get_frozen_credentials(), self._service, self._region).add_auth(aws_request)
        request.headers.update(dict(aws_request.headers.items()))
        yield request


def gateway_client(url: str, region: str) -> MCPClient:
    """MCP client for the Gateway endpoint; use it as a context manager."""
    return MCPClient(url=url, auth_provider=IamAuth(region), startup_timeout=30)


def list_gateway_tools(client: MCPClient, timeout_seconds: float) -> list[MCPAgentTool]:
    """Gateway tools with a per-call timeout, so a hung backend cannot stall the agent."""
    tools = list(client.list_tools_sync())
    for tool in tools:
        tool.timeout = timedelta(seconds=timeout_seconds)
    return tools


def is_retryable(result: dict[str, Any]) -> bool:
    """Whether repeating the identical tool call may succeed.

    The backend flags its own errors with `retryable`; transport failures (timeouts, 5xx, throttling) are
    recognised by their message. Authorization denials are never retried.
    """
    error = backend_error(result)
    if error is not None:
        return error.get("retryable") is True
    if result.get("status") != "error":
        return False
    text = " ".join(block.get("text") or "" for block in result.get("content", []))
    return bool(_TRANSIENT.search(text)) and not _NOT_RETRYABLE.search(text)


def backend_error(result: dict[str, Any]) -> dict[str, Any] | None:
    """The {"error": {...}} object the backend returned as the tool result, if any."""
    for block in result.get("content", []):
        error = _block_error(block)
        if error is not None:
            return error
    return None


def _block_error(block: dict[str, Any]) -> dict[str, Any] | None:
    payload = _block_payload(block)
    error = payload.get("error") if isinstance(payload, dict) else None
    return error if isinstance(error, dict) else None


def _block_payload(block: dict[str, Any]) -> Any:
    """The JSON value of a tool result block, sent as structured content or as text."""
    payload = block.get("json")
    if payload is None:
        try:
            payload = json.loads(block.get("text") or "")
        except ValueError:
            return None
    return payload


def _unmark_replay(block: dict[str, Any]) -> dict[str, Any]:
    payload = _block_payload(block)
    if not isinstance(payload, dict) or payload.get("replayed") is not True:
        return block
    payload = {**payload, "replayed": False}
    return {"json": payload} if "json" in block else {"text": json.dumps(payload)}


class RequesterBinding(HookProvider):
    """Binds every tool call to the signed-in customer.

    The requester comes from the authenticated invocation and overwrites whatever the model sent, so no prompt
    can make a tool act for another customer; the backend checks ownership against it.
    """

    def __init__(self, actor_id: str) -> None:
        self._actor_id = actor_id

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._bind_requester)

    def _bind_requester(self, event: BeforeToolCallEvent) -> None:
        # A new dict, so the conversation history keeps the model's own arguments.
        arguments = event.tool_use["input"] if isinstance(event.tool_use["input"], dict) else {}
        event.tool_use = {**event.tool_use, "input": {**arguments, REQUESTER_FIELD: self._actor_id}}


class RefundIdempotency(HookProvider):
    """Derives the refund idempotency key from the session, order and amount instead of trusting the model.

    Repeating the same refund request in a session, however the model words it, always carries the same key,
    so the backend returns the existing refund instead of creating a second one.
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._bind_key)

    def _bind_key(self, event: BeforeToolCallEvent) -> None:
        tool_use = event.tool_use
        arguments = tool_use["input"]
        amount = arguments.get("amount") if isinstance(arguments, dict) else None
        if not tool_use["name"].endswith(f"{TOOL_SEPARATOR}refund_customer") or not isinstance(amount, int | float):
            return  # other tools have no key; an invalid amount is rejected by the backend
        request = f"{self._session_id}|{arguments.get('order_id')}|{amount:.2f}"
        key = f"refund-{hashlib.sha256(request.encode()).hexdigest()[:32]}"
        event.tool_use = {**tool_use, "input": {**arguments, IDEMPOTENCY_FIELD: key}}


class ToolReliabilityHooks(HookProvider):
    """Retries transient tool failures with exponential backoff, stops runaway tool-call loops and reports
    backend errors as failed tool calls.

    A retry re-sends the identical tool call, including the refund idempotency_key, so the backend returns
    the original refund instead of creating a second one, and that refund is reported as the call's own result.
    """

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        max_delay: float = 4.0,
        max_tool_calls: int = 10,
        max_identical_calls: int = 2,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_tool_calls = max_tool_calls
        self.max_identical_calls = max_identical_calls
        self._sleep = sleep
        self._admitted: set[str] = set()
        self._signatures: Counter[str] = Counter()
        self._retries: Counter[str] = Counter()

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._reset)
        registry.add_callback(BeforeToolCallEvent, self._stop_loops)
        registry.add_callback(AfterToolCallEvent, self._retry_transient_failures)
        registry.add_callback(AfterToolCallEvent, self._flag_backend_errors)
        registry.add_callback(AfterToolCallEvent, self._report_recovered_replays)

    def _reset(self, event: BeforeInvocationEvent) -> None:
        self._admitted.clear()
        self._signatures.clear()
        self._retries.clear()

    def _stop_loops(self, event: BeforeToolCallEvent) -> None:
        tool_use = event.tool_use
        if tool_use["toolUseId"] in self._admitted:
            return  # a retry of a call that was already admitted
        self._admitted.add(tool_use["toolUseId"])
        signature = f"{tool_use['name']}:{json.dumps(tool_use['input'], sort_keys=True)}"
        self._signatures[signature] += 1
        if len(self._admitted) > self.max_tool_calls or self._signatures[signature] > self.max_identical_calls:
            logger.warning(
                "tool=<%s>, calls=<%d> | loop guard cancelled tool call", tool_use["name"], len(self._admitted)
            )
            trace.get_current_span().add_event("tool.loop_guard", {"tool.name": tool_use["name"]})
            event.cancel_tool = (
                "Tool call limit reached for this request. Do not call tools again; "
                "answer with the information you already have."
            )

    async def _retry_transient_failures(self, event: AfterToolCallEvent) -> None:
        tool_use = event.tool_use
        if event.cancel_message or not is_retryable(event.result):
            return
        retries = self._retries[tool_use["toolUseId"]]
        if retries + 1 >= self.max_attempts:
            logger.warning("tool=<%s>, attempts=<%d> | giving up on transient failure", tool_use["name"], retries + 1)
            return
        self._retries[tool_use["toolUseId"]] = retries + 1
        delay = min(self.max_delay, self.base_delay * 2**retries) * random.uniform(0.5, 1.0)
        logger.warning(
            "tool=<%s>, retry=<%d>, delay=<%.2fs> | retrying transient failure", tool_use["name"], retries + 1, delay
        )
        trace.get_current_span().add_event(
            "tool.retry", {"tool.name": tool_use["name"], "retry.attempt": retries + 1, "retry.delay_seconds": delay}
        )
        await self._sleep(delay)
        event.retry = True

    def _flag_backend_errors(self, event: AfterToolCallEvent) -> None:
        """Mark backend errors as failed results; the backend returns them as normal MCP responses, so the
        tool span would otherwise end with status OK and the failure would not show up in trace searches."""
        error = backend_error(event.result)
        if error is None:
            return
        event.result = {**event.result, "status": "error"}
        trace.get_current_span().add_event(
            "tool.backend_error",
            {
                "tool.name": event.tool_use["name"],
                "error.code": str(error.get("code")),
                "error.retryable": error.get("retryable") is True,
            },
        )

    def _report_recovered_replays(self, event: AfterToolCallEvent) -> None:
        """Report replayed=false when a retry gets back the refund that an earlier attempt of the same call
        committed (e.g. its response was lost), so the model does not tell the customer it existed before."""
        if not self._retries[event.tool_use["toolUseId"]]:
            return
        content = event.result.get("content", [])
        recovered = [_unmark_replay(block) for block in content]
        if recovered != content:
            event.result = {**event.result, "content": recovered}
            trace.get_current_span().add_event("tool.replay_recovered", {"tool.name": event.tool_use["name"]})
