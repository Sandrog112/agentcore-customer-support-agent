"""The tool calls of an agent turn, read back from AgentCore Memory, and what happened to each of them.

The agent's session manager stores every message of a conversation in Memory (agent/main.py), including the
model's toolUse blocks and the toolResult blocks that answered them, so the console shows the tool calls without
any change to the agent. Retries happen inside a single tool call, so only the trace shows them.
"""

import functools
import json
import re
from typing import Any

from bedrock_agentcore.memory import MemoryClient
from bedrock_agentcore.memory.integrations.strands.bedrock_converter import AgentCoreMemoryConverter

from agent.gateway import TOOL_SEPARATOR, backend_error

MAX_EVENTS = 1000
CONTEXT_TAG = "<user_context>"  # remembered preferences the session manager puts in front of a prompt

# Labels for the backend's error codes (backend/handler.py); tone is ok, denied, failed or info.
BACKEND_ERRORS = {
    "FORBIDDEN": ("Access refused", "denied"),
    "REFUND_LIMIT_EXCEEDED": ("Over refund cap", "denied"),
    "INVALID_ARGUMENT": ("Invalid input", "failed"),
    "NOT_FOUND": ("Not found", "failed"),
    "UPSTREAM_ERROR": ("Upstream failure", "failed"),
    "BACKEND_UNAVAILABLE": ("Data store unavailable", "failed"),
    "IDEMPOTENCY_CONFLICT": ("Key already used", "failed"),
    "UNKNOWN_TOOL": ("Unknown tool", "failed"),
}
_LOOP_GUARD = re.compile(r"tool call limit reached", re.IGNORECASE)  # ToolReliabilityHooks' cancel message
_TIMEOUT = re.compile(r"timed? ?out", re.IGNORECASE)
_DENIED = re.compile(r"denied|not authori[sz]ed|unauthori[sz]ed|forbidden|policy", re.IGNORECASE)


@functools.cache
def _memory(region: str) -> MemoryClient:
    return MemoryClient(region_name=region)


def turn_tool_calls(memory_id: str, region: str, actor_id: str, session_id: str, prompt: str) -> list[dict] | None:
    """Tool calls of the turn that `prompt` started, the latest one of the session; None if it is not stored."""
    events = _memory(region).list_events(
        memory_id=memory_id, actor_id=actor_id, session_id=session_id, max_results=MAX_EVENTS
    )
    # The same conversion the agent uses to restore the conversation.
    messages = [message.to_message() for message in AgentCoreMemoryConverter.events_to_messages(events)]
    return last_turn_tool_calls(messages, prompt)


def last_turn_tool_calls(messages: list[dict[str, Any]], prompt: str) -> list[dict] | None:
    starts = [index for index, message in enumerate(messages) if _customer_text(message) is not None]
    if not starts or _customer_text(messages[starts[-1]]) != prompt.strip():
        return None
    turn = messages[starts[-1] :]
    results = {
        block["toolResult"]["toolUseId"]: block["toolResult"]
        for message in turn
        for block in message["content"]
        if "toolResult" in block
    }
    calls = []
    for message in turn:
        for block in message["content"]:
            if "toolUse" in block:
                tool_use = block["toolUse"]
                result = results.get(tool_use["toolUseId"])
                calls.append(
                    {
                        "tool": tool_use["name"].split(TOOL_SEPARATOR)[-1],
                        "input": tool_use["input"],
                        "result": result,
                        "outcome": outcome(result),
                    }
                )
    return calls


def outcome(result: dict[str, Any] | None) -> dict[str, str]:
    """{"label", "tone", "detail"} of a tool result, as the agent or a direct MCP call received it."""
    if result is None:
        return {"label": "No result", "tone": "failed", "detail": "No result was stored for this call."}
    error = backend_error(result)
    if error is not None:
        label, tone = BACKEND_ERRORS.get(str(error.get("code")), (str(error.get("code")), "failed"))
        return {"label": label, "tone": tone, "detail": f"{error.get('code')}: {error.get('message')}"}
    text = " ".join(block.get("text") or "" for block in result.get("content", [])).strip()
    if result.get("status") == "error":
        if _LOOP_GUARD.search(text):
            return {"label": "Stopped by loop guard", "tone": "failed", "detail": text}
        if _TIMEOUT.search(text):
            return {"label": "Timed out", "tone": "failed", "detail": text}
        if _DENIED.search(text):
            return {"label": "Denied at Gateway", "tone": "denied", "detail": text}
        return {"label": "Failed", "tone": "failed", "detail": text}
    payload = _payload(result)
    if isinstance(payload, dict) and payload.get("replayed") is True:
        return {"label": "Replayed", "tone": "info", "detail": "Returned the refund already issued with this key."}
    return {"label": "Succeeded", "tone": "ok", "detail": ""}


def _customer_text(message: dict[str, Any]) -> str | None:
    """The text the customer sent, for a user message that is not a tool result."""
    if message.get("role") != "user" or any("toolResult" in block for block in message["content"]):
        return None
    texts = [block["text"] for block in message["content"] if "text" in block]
    return "\n".join(text for text in texts if not text.startswith(CONTEXT_TAG)).strip()


def _payload(result: dict[str, Any]) -> Any:
    for block in result.get("content", []):
        if "json" in block:
            return block["json"]
        try:
            return json.loads(block.get("text") or "")
        except ValueError:
            continue
    return result.get("structuredContent")
