"""Customer support agent: Strands on AgentCore Runtime, business tools through AgentCore Gateway (MCP),
conversation history and customer preferences in AgentCore Memory."""

import logging
import os
import re
from typing import Any

import boto3
import httpx
from anthropic import AsyncAnthropicBedrockMantle
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext
from openai import AsyncOpenAI
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from strands import Agent
from strands.models import Model
from strands.models.anthropic import AnthropicModel
from strands.models.openai import OpenAIModel
from strands.tools.mcp import MCPAgentTool

from agent.config import PREFERENCES_NAMESPACE
from agent.gateway import (
    TOOL_SEPARATOR,
    IamAuth,
    RefundIdempotency,
    RequesterBinding,
    ToolReliabilityHooks,
    gateway_client,
    list_gateway_tools,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "anthropic.claude-opus-5-5"
TOOL_TIMEOUT_SECONDS = 6
ACTOR_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")

# Access and refund limits are deliberately absent: the backend binds every tool call to the signed-in customer,
# and AgentCore Policy at the Gateway decides refunds, so neither depends on the prompt. The tool names are filled
# in from the discovered Gateway tools, which are registered as "<target>___<tool>".
SYSTEM_PROMPT = """You are the customer support agent of an online electronics store.
You are talking to the signed-in customer {actor_id}; tools only return this customer's own account and orders.

Tools (use these exact names):
- {get_order}: anything about an order (status, shipping, delays). Order ids are numeric, e.g. "123".
- {get_customer}: the customer's account details. Customer ids look like "CUST-001".
- {refund_customer}: issue a refund. Look up the order first. Repeated requests for the same refund are
  recognised by the platform. If the result says replayed=true, that refund had already been issued: tell the
  customer so instead of reporting a new refund.

Rules:
- Only state facts returned by tools; never invent order, customer or refund details.
- Failed tool calls have already been retried for you. If a tool still fails, explain the problem briefly
  instead of calling it again with the same arguments.
- Refund approval is decided by the platform. If a refund is denied, tell the customer that it needs review
  by a human agent.
- Preferences the customer tells you (such as a preferred AWS region or contact method) are remembered
  automatically for future conversations: acknowledge them and say you will remember them.
- Use what you remember about the customer (inside <user_context>) when it is relevant.

Answer concisely."""

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict[str, Any], context: RequestContext) -> dict[str, Any]:
    """Answer one customer message. Payload: {"prompt": str, "actor_id": str, "operation_id": str}.

    The runtime only accepts SigV4-authenticated callers (the support front end), which authenticate the
    customer and pass their id as actor_id. It scopes the conversation, the remembered preferences and, through
    RequesterBinding, every tool call.

    operation_id identifies the customer's request and is the refund idempotency key: the caller reuses it when it
    retries the request, in any session, and uses a new one for a new request. A refund repeated under the same
    operation_id returns the original refund; under another one it is a separate refund.
    """
    prompt = payload.get("prompt")
    actor_id = payload.get("actor_id")
    operation_id = payload.get("operation_id")
    if not isinstance(prompt, str) or not prompt.strip():
        return {"error": "'prompt' must be a non-empty string."}
    if not isinstance(actor_id, str) or not ACTOR_ID.fullmatch(actor_id):
        return {"error": "'actor_id' must be 1-64 letters, digits, '-' or '_'."}
    if not context.session_id:
        return {"error": "A runtime session id is required."}
    if not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(operation_id):
        return {"error": "'operation_id' must be 1-64 letters, digits or . _ : - characters."}

    trace_id = _current_trace_id()
    try:
        with gateway_client(os.environ["GATEWAY_URL"], _region()) as gateway:
            tools = list_gateway_tools(gateway, TOOL_TIMEOUT_SECONDS)
            result = _build_agent(context.session_id, actor_id, operation_id, tools)(prompt)
    except Exception as error:
        logger.exception("session=<%s>, trace=<%s> | invocation failed", context.session_id, trace_id)
        # The caller gets a normal response, so mark the invocation span failed to keep it findable in traces.
        span = trace.get_current_span()
        span.record_exception(error)
        span.set_status(StatusCode.ERROR, type(error).__name__)
        return {"error": "The support agent is temporarily unavailable. Please try again.", "trace_id": trace_id}

    return {
        "response": str(result).strip(),
        "tool_calls": {
            name.split(TOOL_SEPARATOR)[-1]: metrics.call_count for name, metrics in result.metrics.tool_metrics.items()
        },
        "session_id": context.session_id,
        "trace_id": trace_id,
    }


def _build_model(model_id: str) -> Model:
    """Claude through the Messages API, any other Bedrock model through the OpenAI-compatible Chat Completions API;
    both are served by the bedrock-mantle endpoint and signed with the runtime role."""
    if model_id.startswith("anthropic."):
        model = AnthropicModel(model_id=model_id, max_tokens=16000, params={"output_config": {"effort": "medium"}})
        model.client = AsyncAnthropicBedrockMantle(aws_region=_region())
        return model
    # Open-weight models such as openai.gpt-oss-120b are served from /v1; the api_key is a placeholder because
    # IamAuth replaces the Authorization header with a SigV4 signature.
    client = AsyncOpenAI(
        base_url=f"https://bedrock-mantle.{_region()}.api.aws/v1",
        api_key="sigv4",
        http_client=httpx.AsyncClient(auth=IamAuth(_region(), service="bedrock-mantle"), timeout=120),
    )
    return OpenAIModel(client=client, model_id=model_id, params={"max_tokens": 16000})


def _system_prompt(actor_id: str, tools: list[MCPAgentTool]) -> str:
    """The prompt with each tool's registered name; a missing tool fails the invocation, not the model."""
    names = {tool.tool_name.split(TOOL_SEPARATOR)[-1]: tool.tool_name for tool in tools}
    return SYSTEM_PROMPT.format(actor_id=actor_id, **names)


def _build_agent(session_id: str, actor_id: str, operation_id: str, tools: list[MCPAgentTool]) -> Agent:
    model = _build_model(os.getenv("BEDROCK_MODEL_ID") or DEFAULT_MODEL_ID)

    memory = AgentCoreMemorySessionManager(
        AgentCoreMemoryConfig(
            memory_id=os.environ["MEMORY_ID"],
            session_id=session_id,
            actor_id=actor_id,
            retrieval_config={PREFERENCES_NAMESPACE: RetrievalConfig(top_k=5)},
        ),
        region_name=_region(),
    )
    agent = Agent(
        name="customer_support_agent",
        model=model,
        system_prompt=_system_prompt(actor_id, tools),
        tools=tools,
        session_manager=memory,
        hooks=[RequesterBinding(actor_id), RefundIdempotency(operation_id), ToolReliabilityHooks()],
        callback_handler=None,
        trace_attributes={"session.id": session_id, "actor.id": actor_id},
    )
    _drop_restored_reasoning(agent)
    return agent


def _drop_restored_reasoning(agent: Agent) -> None:
    """Remove thinking blocks from the history restored out of AgentCore Memory.

    Claude binds each thinking block to the exact transcript that produced it, and restored turns lack the
    memory context that was injected at the time. Dropping every earlier thinking block is the supported way
    to continue such a conversation.
    """
    for message in agent.messages:
        message["content"] = [block for block in message["content"] if "reasoningContent" not in block]


def _current_trace_id() -> str | None:
    """X-Ray formatted id of the active trace, used to find this invocation in CloudWatch."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    trace_id = f"{span_context.trace_id:032x}"
    return f"1-{trace_id[:8]}-{trace_id[8:]}"


def _region() -> str:
    return os.getenv("AWS_REGION") or boto3.Session().region_name


if __name__ == "__main__":
    app.run()
