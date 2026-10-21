"""The localhost test console (web/): tool call outcomes, turns read back from Memory, the request guards and a
chat round trip, with AWS replaced by stubs."""

import json

import pytest
from starlette.testclient import TestClient

from web import server
from web.activity import last_turn_tool_calls, outcome
from web.evidence import RunRecord
from web.scenarios import SCENARIOS

LOCAL = "http://127.0.0.1:8765"
STACK = {
    "deployed": True,
    "problem": None,
    "region": "us-east-1",
    "runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/support_agent-abc",
    "memory_id": "support_agent-xyz",
    "identity": "arn:aws:sts::123456789012:assumed-role/operator/me",
    "links": {},
}
SESSION_ID = "session-00000000-0000-0000-0000-000000000000"


def text_result(payload, status="success"):
    return {"toolUseId": "t1", "status": status, "content": [{"text": json.dumps(payload)}]}


@pytest.mark.parametrize(
    ("result", "label", "tone"),
    [
        (text_result({"order_id": "123"}), "Succeeded", "ok"),
        (text_result({"refund_id": "RF-1", "replayed": True}), "Replayed", "info"),
        (text_result({"error": {"code": "FORBIDDEN", "message": "Not yours.", "retryable": False}}, "error"),
         "Access refused", "denied"),
        (text_result({"error": {"code": "REFUND_LIMIT_EXCEEDED", "message": "Cap.", "retryable": False}}, "error"),
         "Over refund cap", "denied"),
        ({"status": "error", "content": [{"text": "Tool call limit reached for this request."}]},
         "Stopped by loop guard", "failed"),
        ({"status": "error", "content": [{"text": "Tool execution timed out"}]}, "Timed out", "failed"),
        ({"status": "error", "content": [{"text": "Tool call denied by policy"}]}, "Denied at Gateway", "denied"),
        (None, "No result", "failed"),
    ],
)  # fmt: skip
def test_tool_call_outcomes(result, label, tone):
    assert (outcome(result)["label"], outcome(result)["tone"]) == (label, tone)


def conversation():
    def use(tool_id, name, arguments):
        return {"toolUse": {"toolUseId": tool_id, "name": f"support-tools___{name}", "input": arguments}}

    def result(tool_id, payload):
        return {"toolResult": {**text_result(payload), "toolUseId": tool_id}}

    return [
        {"role": "user", "content": [{"text": "Where is order 123?"}]},
        {"role": "assistant", "content": [use("a", "get_order", {"order_id": "123"})]},
        {"role": "user", "content": [result("a", {"order_id": "123"})]},
        {"role": "assistant", "content": [{"text": "It is delayed."}]},
        {"role": "user", "content": [{"text": "<user_context>Prefers email</user_context>"}, {"text": "Refund it"}]},
        {"role": "assistant", "content": [use("b", "refund_customer", {"order_id": "123", "amount": 10})]},
        {"role": "user", "content": [result("b", {"refund_id": "RF-1", "replayed": False})]},
        {"role": "assistant", "content": [{"text": "Refunded."}]},
    ]


def test_only_the_latest_turn_is_reported():
    calls = last_turn_tool_calls(conversation(), "Refund it")

    assert [(call["tool"], call["outcome"]["label"]) for call in calls] == [("refund_customer", "Succeeded")]
    assert calls[0]["input"] == {"order_id": "123", "amount": 10}


def test_a_turn_that_was_not_stored_is_not_attributed_to_another():
    assert last_turn_tool_calls(conversation(), "A message Memory never stored") is None


def test_scenarios_sign_in_as_seeded_customers():
    customers = {item["customer_id"] for item in json.loads((server.ROOT / "backend/seed_data.json").read_text())
                 if "customer_id" in item}  # fmt: skip

    for scenario in SCENARIOS:
        assert scenario["actor"] is None or scenario["actor"] in customers
        assert all(step["prompt"].strip() for step in scenario["steps"])


@pytest.fixture
def console(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "stack_status", lambda: dict(STACK))
    monkeypatch.setattr(server, "record", RunRecord(tmp_path))
    return TestClient(server.app, base_url=LOCAL)


def test_requests_for_other_hosts_are_refused(console):
    assert TestClient(server.app, base_url="http://attacker.example").get("/api/status").status_code == 400


def test_writes_must_be_same_origin_json(console):
    body = {"prompt": "Hi", "actor_id": "CUST-001", "session_id": SESSION_ID}

    assert console.post("/api/chat", content=json.dumps(body)).status_code == 415
    cross_site = console.post("/api/chat", json=body, headers={"Origin": "http://attacker.example"})
    assert cross_site.status_code == 403


def test_pages_forbid_inline_and_third_party_code(console):
    response = console.get("/")

    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert 'id="composer"' in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"prompt": " ", "actor_id": "CUST-001", "session_id": SESSION_ID},
        {"prompt": "Hi", "actor_id": "../CUST-002", "session_id": SESSION_ID},
        {"prompt": "Hi", "actor_id": "CUST-001", "session_id": "too-short"},
    ],
)
def test_invalid_messages_are_rejected(console, body):
    assert console.post("/api/chat", json=body).status_code == 400


def test_chat_records_the_answer_and_its_tool_calls(console, monkeypatch):
    answer = {"response": "Refunded.", "tool_calls": {"refund_customer": 1}, "trace_id": "1-abc-def"}
    monkeypatch.setattr(server, "invoke_agent", lambda prompt, actor, session: answer)
    monkeypatch.setattr(
        server,
        "turn_tool_calls",
        lambda memory, region, actor, session, prompt: last_turn_tool_calls(conversation(), prompt),
    )
    body = {"prompt": "Refund it", "actor_id": "CUST-001", "session_id": SESSION_ID, "scenario": "Refund"}

    entry = console.post("/api/chat", json=body).json()["entry"]

    assert entry["response"] == "Refunded."
    assert entry["tool_calls_detail"][0]["tool"] == "refund_customer"
    assert entry["trace_url"].endswith("#xray:traces/1-abc-def")


def test_evidence_report_lists_each_turn_and_its_tool_calls(console, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "invoke_agent", lambda prompt, actor, session: {"response": "Refunded."})
    monkeypatch.setattr(server, "turn_tool_calls", lambda *args: last_turn_tool_calls(conversation(), "Refund it"))
    body = {"prompt": "Refund it", "actor_id": "CUST-001", "session_id": SESSION_ID, "scenario": "Refund"}
    assert console.post("/api/evidence", json={}).status_code == 400  # nothing recorded yet
    console.post("/api/chat", json=body)

    saved = console.post("/api/evidence", json={}).json()

    report, raw = (tmp_path / path.rsplit("/", 1)[-1] for path in saved["paths"])
    assert "## 1. Agent turn: Refund" in report.read_text()
    assert "| 1 | refund_customer |" in report.read_text()
    assert json.loads(raw.read_text())["entries"][0]["response"] == "Refunded."
    assert saved["record"] == {"entries": 1, "unsaved": 0}


def test_a_failed_invocation_is_recorded_as_an_error(console, monkeypatch):
    def unreachable(prompt, actor, session):
        raise TimeoutError("Read timed out")

    monkeypatch.setattr(server, "invoke_agent", unreachable)
    monkeypatch.setattr(server, "turn_tool_calls", lambda *args: None)
    body = {"prompt": "Hi", "actor_id": "CUST-001", "session_id": SESSION_ID}

    entry = console.post("/api/chat", json=body).json()["entry"]

    assert entry["error"] == "The agent could not be invoked: Read timed out"
    assert entry["tool_calls_note"] == "This turn was not stored in AgentCore Memory."
