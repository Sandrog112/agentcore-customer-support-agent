"""Customer support agent: Strands on AgentCore Runtime, business tools through AgentCore Gateway (MCP),
conversation history and customer preferences in AgentCore Memory."""

import logging
import os
import re
from typing import Any

import boto3
from anthropic import AsyncAnthropicBedrockMantle
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from strands import Agent
from strands.models.anthropic import AnthropicModel
from strands.tools.mcp import MCPAgentTool

from agent.config import PREFERENCES_NAMESPACE
from agent.gateway import (
    TOOL_SEPARATOR,
    RequesterBinding,
    ToolReliabilityHooks,
    gateway_client,
    list_gateway_tools,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "anthropic.claude-opus-5-5"
TOOL_TIMEOUT_SECONDS = 6
ACTOR_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

# Access and refund limits are deliberately absent: the backend binds every tool call to the signed-in customer,
# and AgentCore Policy at the Gateway decides refunds, so neither depends on the prompt.
SYSTEM_PROMPT = """You are the customer support agent of an online electronics store.
You are talking to the signed-in customer {actor_id}; tools only return this customer's own account and orders.

Tools:
- get_order: anything about an order (status, shipping, delays). Order ids are numeric, e.g. "123".
- get_customer: the customer's account details. Customer ids look like "CUST-001".
- refund_customer: issue a refund. Look up the order first. Always pass an idempotency_key: use the one the
  customer gives you, otherwise "refund-<order_id>-<amount>". Reuse the same key when repeating a refund.
  If the result says replayed=true, that refund had already been issued: tell the customer so instead of
  reporting a new refund, and use a new key only if they confirm they want an additional refund.

Rules:
- Only state facts returned by tools; never invent order, customer or refund details.
- Failed tool calls have already been retried for you. If a tool still fails, explain the problem briefly
  instead of calling it again with the same arguments.
- Refund approval is decided by the platform. If a refund is denied, tell the customer that it needs review
  by a human agent.
- Use what you remember about the customer (inside <user_context>) when it is relevant.

Answer concisely."""

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict[str, Any], context: RequestContext) -> dict[str, Any]:
    """Answer one customer message. Payload: {"prompt": str, "actor_id": str}.

    The runtime only accepts SigV4-authenticated callers (the support front end), which authenticate the
    customer and pass their id as actor_id. It scopes the conversation, the remembered preferences and, through
    RequesterBinding, every tool call.
    """
    prompt = payload.get("prompt")
    actor_id = payload.get("actor_id")
    if not isinstance(prompt, str) or not prompt.strip():
        return {"error": "'prompt' must be a non-empty string."}
    if not isinstance(actor_id, str) or not ACTOR_ID.fullmatch(actor_id):
        return {"error": "'actor_id' must be 1-64 letters, digits, '-' or '_'."}
    if not context.session_id:
        return {"error": "A runtime session id is required."}

    trace_id = _current_trace_id()
    try:
        with gateway_client(os.environ["GATEWAY_URL"], _region()) as gateway:
            tools = list_gateway_tools(gateway, TOOL_TIMEOUT_SECONDS)
            result = _build_agent(context.session_id, actor_id, tools)(prompt)
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


def _build_agent(session_id: str, actor_id: str, tools: list[MCPAgentTool]) -> Agent:
    model = AnthropicModel(
        model_id=os.getenv("BEDROCK_MODEL_ID") or DEFAULT_MODEL_ID,
        max_tokens=16000,
        params={"output_config": {"effort": "medium"}},
    )
    # Claude Opus 5.5 on Bedrock is served by the Messages API endpoint (bedrock-mantle), signed with the runtime role.
    model.client = AsyncAnthropicBedrockMantle(aws_region=_region())

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
        system_prompt=SYSTEM_PROMPT.format(actor_id=actor_id),
        tools=tools,
        session_manager=memory,
        hooks=[RequesterBinding(actor_id), ToolReliabilityHooks()],
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
