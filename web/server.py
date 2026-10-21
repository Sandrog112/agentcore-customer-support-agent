"""Localhost test console for the deployed support agent:

    uv run --env-file .env python -m web            # then open http://127.0.0.1:8765

Serves web/static and a small JSON API: messages go to the agent through deploy.py's invoke_agent, direct tool
calls through its call_tool, and every exchange is kept in a run record that can be saved to evidence/.

The console acts with the operator's AWS credentials, which can issue refunds, so it listens on 127.0.0.1 only,
answers only requests addressed to localhost (no DNS rebinding) and only accepts same-origin JSON writes (no CSRF).
"""

import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent.config import PREFERENCES_NAMESPACE
from agent.main import ACTOR_ID
from deploy import call_tool, invoke_agent, load_outputs
from infra.aws import client, session
from infra.config import AGENTCORE, REFUND_LIMIT, ROOT
from web.activity import outcome, turn_tool_calls
from web.evidence import RunRecord
from web.scenarios import SCENARIOS

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 8765
STATIC_DIR = ROOT / "web" / "static"
LOCAL_HOSTS = ["127.0.0.1", "localhost"]
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{32,99}")  # runtime session ids are at least 33 characters
MAX_PROMPT_LENGTH = 4000
TOOLS = {
    "get_order": {"order_id": "123"},
    "get_customer": {"customer_id": "CUST-001"},
    "refund_customer": {"order_id": "789", "amount": 10, "reason": "Console test", "idempotency_key": "console-1"},
}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

record = RunRecord()


class ConsoleError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class SecurityHeaders:
    """No framing, no inline or third-party code, nothing cached."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def index(request: Request) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def status(request: Request) -> JSONResponse:
    stack = await run_in_threadpool(stack_status)
    customers = json.loads((ROOT / "backend" / "seed_data.json").read_text())
    return JSONResponse(
        {
            "stack": stack,
            "customers": _customer_cards(customers),
            "scenarios": SCENARIOS,
            "tools": TOOLS,
            "refund_limit": REFUND_LIMIT,
            "entries": record.entries,
            "record": _record_summary(),
        }
    )


async def chat(request: Request) -> JSONResponse:
    body = await _json_body(request)
    prompt = _required_text(body, "prompt", MAX_PROMPT_LENGTH)
    actor_id, session_id = _actor(body), _session(body)
    scenario = body.get("scenario") if isinstance(body.get("scenario"), str) else None
    stack = await run_in_threadpool(_ready_stack)

    started_at, started = _now(), time.perf_counter()
    entry: dict[str, Any] = {"kind": "chat", "at": started_at, "actor_id": actor_id, "session_id": session_id}
    entry |= {"scenario": scenario, "prompt": prompt, "response": None, "error": None, "tool_calls": {}}
    try:
        answer = await run_in_threadpool(invoke_agent, prompt, actor_id, session_id)
    except Exception as error:
        logger.exception("agent invocation failed")
        answer = {"error": f"The agent could not be invoked: {_describe(error)}"}
    entry["duration_ms"] = round((time.perf_counter() - started) * 1000)
    entry |= {key: answer.get(key) for key in ("response", "error", "trace_id")}
    entry["tool_calls"] = answer.get("tool_calls") or {}
    entry["trace_url"] = _trace_url(stack["region"], entry["trace_id"]) if entry["trace_id"] else None

    try:
        calls = await run_in_threadpool(
            turn_tool_calls, stack["memory_id"], stack["region"], actor_id, session_id, prompt
        )
        entry["tool_calls_detail"] = calls
        entry["tool_calls_note"] = None if calls is not None else "This turn was not stored in AgentCore Memory."
    except Exception as error:
        logger.exception("reading the turn from AgentCore Memory failed")
        entry["tool_calls_detail"] = None
        entry["tool_calls_note"] = f"Could not read the turn from AgentCore Memory: {_describe(error)}"
    return JSONResponse({"entry": record.add(entry), "record": _record_summary()})


async def tool(request: Request) -> JSONResponse:
    body = await _json_body(request)
    name, arguments, actor_id = body.get("tool"), body.get("arguments"), _actor(body)
    if name not in TOOLS:
        raise ConsoleError(400, f"tool must be one of {', '.join(TOOLS)}.")
    if not isinstance(arguments, dict):
        raise ConsoleError(400, "arguments must be a JSON object.")
    await run_in_threadpool(_ready_stack)

    started_at, started = _now(), time.perf_counter()
    entry: dict[str, Any] = {"kind": "tool", "at": started_at, "actor_id": actor_id, "tool": name}
    entry |= {"arguments": arguments, "result": None, "outcome": None, "error": None}
    try:
        entry["result"] = await run_in_threadpool(call_tool, name, arguments, actor_id)
        entry["outcome"] = outcome(entry["result"])
    except Exception as error:
        logger.exception("direct tool call failed")
        entry["error"] = f"The tool could not be called: {_describe(error)}"
    entry["duration_ms"] = round((time.perf_counter() - started) * 1000)
    return JSONResponse({"entry": record.add(entry), "record": _record_summary()})


async def memory(request: Request) -> JSONResponse:
    actor_id = _actor(dict(request.query_params))
    stack = await run_in_threadpool(_ready_stack)

    def records() -> list[dict[str, Any]]:
        response = client(AGENTCORE).list_memory_records(
            memoryId=stack["memory_id"], namespace=PREFERENCES_NAMESPACE.format(actorId=actor_id), maxResults=50
        )
        summaries = sorted(response["memoryRecordSummaries"], key=lambda item: item["createdAt"], reverse=True)
        return [{"text": item["content"]["text"], "created_at": item["createdAt"].isoformat()} for item in summaries]

    try:
        return JSONResponse({"actor_id": actor_id, "records": await run_in_threadpool(records)})
    except (BotoCoreError, ClientError) as error:
        raise ConsoleError(502, f"Could not read AgentCore Memory: {_describe(error)}") from error


async def save_evidence(request: Request) -> JSONResponse:
    await _json_body(request)
    if not record.entries:
        raise ConsoleError(400, "Nothing to save yet: send a message or call a tool first.")
    stack = await run_in_threadpool(stack_status)
    paths = await run_in_threadpool(record.save, stack)
    shown = [str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path) for path in paths]
    return JSONResponse({"paths": shown, "record": _record_summary()})


def stack_status() -> dict[str, Any]:
    """The deployment the console talks to, the operator's AWS identity, and the first problem that stops it."""
    stack: dict[str, Any] = {"deployed": False, "problem": None, "identity": None, "links": {}}
    try:
        outputs = load_outputs()
    except FileNotFoundError:
        stack["problem"] = (
            "No deployed stack found: deploy-outputs.json is missing. Run "
            "`uv run --env-file .env python deploy.py deploy`, then check again."
        )
        return stack
    stack |= {key: outputs[key] for key in ("region", "runtime_arn", "memory_id", "gateway_arn")}
    stack["deployed"] = True
    stack["links"] = {
        "runtime_logs": _log_group_url(outputs["region"], outputs["runtime_log_group"]),
        "tool_logs": _log_group_url(outputs["region"], f"/aws/lambda/{outputs['function_name']}"),
    }
    region = boto3.Session().region_name
    if not region:
        stack["problem"] = "AWS_REGION is not set. Set it in .env (see .env.example) and restart the console."
    elif region != outputs["region"]:
        stack["problem"] = (
            f"AWS_REGION is {region}, but the stack is deployed in {outputs['region']}. "
            f"Set AWS_REGION={outputs['region']} in .env and restart the console."
        )
    else:
        try:
            # Short timeouts: this check runs before every request and must fail fast when AWS is unreachable.
            quick = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2})
            stack["identity"] = session().client("sts", config=quick).get_caller_identity()["Arn"]
        except (BotoCoreError, ClientError) as error:
            stack["problem"] = f"Your AWS credentials don't work: {_describe(error)}"
    return stack


def _ready_stack() -> dict[str, Any]:
    stack = stack_status()
    if stack["problem"]:
        raise ConsoleError(409, stack["problem"])
    return stack


def _record_summary() -> dict[str, int]:
    return {"entries": len(record.entries), "unsaved": len(record.entries) - record.saved_entries}


def _customer_cards(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    orders = {item["order_id"]: item for item in items if "order_id" in item}
    return [
        {
            "id": item["customer_id"],
            "name": item["name"],
            "tier": item["tier"],
            "orders": [
                {key: orders[order_id][key] for key in ("order_id", "status", "items", "total")}
                for order_id in item["order_ids"]
            ],
        }
        for item in items
        if "customer_id" in item and "order_ids" in item
    ]


async def _json_body(request: Request) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise ConsoleError(415, "Send JSON with Content-Type: application/json.")
    origin = request.headers.get("origin")
    if origin is not None and origin not in {f"http://{host}:{request.url.port}" for host in LOCAL_HOSTS}:
        raise ConsoleError(403, "Cross-site requests are not allowed.")
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        raise ConsoleError(403, "Cross-site requests are not allowed.")
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError as error:
        raise ConsoleError(400, "The request body is not valid JSON.") from error
    if not isinstance(body, dict):
        raise ConsoleError(400, "The request body must be a JSON object.")
    return body


def _required_text(body: dict[str, Any], name: str, max_length: int) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConsoleError(400, f"{name} must be a non-empty string.")
    if len(value) > max_length:
        raise ConsoleError(400, f"{name} must be at most {max_length} characters.")
    return value.strip()


def _actor(body: dict[str, Any]) -> str:
    actor_id = body.get("actor_id")
    if not isinstance(actor_id, str) or not ACTOR_ID.fullmatch(actor_id):
        raise ConsoleError(400, "The customer id must be 1-64 letters, digits, '-' or '_', e.g. CUST-001.")
    return actor_id


def _session(body: dict[str, Any]) -> str:
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise ConsoleError(400, "session_id must be 33-100 letters, digits, '-' or '_'.")
    return session_id


def _describe(error: Exception) -> str:
    if isinstance(error, ClientError):
        details = error.response.get("Error", {})
        return f"{details.get('Code')}: {details.get('Message')}"
    return str(error) or type(error).__name__


def _cloudwatch_url(region: str, fragment: str) -> str:
    return f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#{fragment}"


def _trace_url(region: str, trace_id: str) -> str:
    return _cloudwatch_url(region, f"xray:traces/{trace_id}")


def _log_group_url(region: str, log_group: str) -> str:
    # The CloudWatch console expects the log group name URL-encoded twice, with '$' in place of '%'.
    encoded = quote(quote(log_group, safe=""), safe="").replace("%", "$")
    return _cloudwatch_url(region, f"logsV2:log-groups/log-group/{encoded}")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


async def _console_error(request: Request, error: ConsoleError) -> JSONResponse:
    return JSONResponse({"error": error.message}, status_code=error.status)


app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/status", status),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/tool", tool, methods=["POST"]),
        Route("/api/memory", memory),
        Route("/api/evidence", save_evidence, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ],
    middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS), Middleware(SecurityHeaders)],
    exception_handlers={ConsoleError: _console_error},
)
