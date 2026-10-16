"""Deploy, exercise and tear down the customer support stack.

    uv run --env-file .env python deploy.py deploy
    uv run --env-file .env python deploy.py invoke "Why is my order 123 delayed?" --actor CUST-001
    uv run --env-file .env python deploy.py call-tool get_order '{"order_id": "123"}' --actor CUST-001
    uv run --env-file .env python deploy.py destroy

`deploy` creates or updates a DynamoDB table, the Lambda tool backend, an AgentCore Gateway with a Cedar policy
engine, AgentCore Memory and the AgentCore Runtime agent, each service with its own least-privilege IAM role,
and writes the resource identifiers to deploy-outputs.json. Each step lives in infra/, one module per service.
"""

import argparse
import json
import os
import uuid
from typing import Any

from agent.gateway import REQUESTER_FIELD, TOOL_SEPARATOR, gateway_client
from infra.aws import arn, client, session, step
from infra.config import AGENTCORE, FUNCTION_NAME, OUTPUTS_FILE, TABLE_NAME, TARGET_NAME
from infra.gateway import deploy_gateway, destroy_gateway, mcp_url
from infra.iam import deploy_roles, destroy_roles
from infra.memory import deploy_memory, destroy_memory
from infra.observability import destroy_deliveries, enable_observability
from infra.policy import deploy_policies, destroy_policies
from infra.runtime import deploy_runtime, destroy_bucket, destroy_runtime
from infra.tool_backend import deploy_lambda, deploy_table, destroy_tool_backend

# Empty placeholders copied from .env.example would otherwise shadow the default AWS credential chain.
for _variable in [name for name, value in os.environ.items() if name.startswith("AWS_") and not value]:
    del os.environ[_variable]


def deploy() -> None:
    step("DynamoDB table with demo data")
    deploy_table()

    step("IAM roles")
    function_arn = arn("lambda", f"function:{FUNCTION_NAME}")
    roles = deploy_roles(function_arn)

    step("Lambda tool backend")
    deploy_lambda(roles["lambda"])

    step("AgentCore Gateway with the Lambda target")
    gateway = deploy_gateway(roles["gateway"], function_arn)

    step("AgentCore Policy (Cedar, ENFORCE mode)")
    policy_engine_id = deploy_policies(gateway)

    step("AgentCore Memory")
    memory = deploy_memory()

    step("Observability (Transaction Search, Gateway and Memory logs/traces)")
    enable_observability({"gateway": gateway["gatewayArn"], "memory": memory["arn"]})

    step("AgentCore Runtime (builds the package; takes a few minutes)")
    environment = {"GATEWAY_URL": mcp_url(gateway), "MEMORY_ID": memory["id"]}
    if os.getenv("BEDROCK_MODEL_ID"):
        environment["BEDROCK_MODEL_ID"] = os.environ["BEDROCK_MODEL_ID"]
    runtime = deploy_runtime(roles["runtime"], environment)

    outputs = {
        "region": session().region_name,
        "runtime_arn": runtime["agentRuntimeArn"],
        "runtime_log_group": f"/aws/bedrock-agentcore/runtimes/{runtime['agentRuntimeId']}-DEFAULT",
        "gateway_arn": gateway["gatewayArn"],
        "gateway_url": environment["GATEWAY_URL"],
        "policy_engine_id": policy_engine_id,
        "memory_id": memory["id"],
        "table_name": TABLE_NAME,
        "function_name": FUNCTION_NAME,
    }
    OUTPUTS_FILE.write_text(json.dumps(outputs, indent=2) + "\n")
    print(json.dumps(outputs, indent=2))


def destroy() -> None:
    """Delete the stack. CloudWatch log groups are kept so logs and traces remain available as evidence."""
    step("AgentCore Runtime")
    destroy_runtime()
    step("AgentCore Gateway")
    destroy_gateway()
    step("AgentCore Policy")
    destroy_policies()
    step("AgentCore Memory")
    destroy_memory()
    step("Log and trace deliveries")
    destroy_deliveries()
    step("Lambda, DynamoDB, IAM roles and S3 artifacts")
    destroy_tool_backend()
    destroy_roles()
    destroy_bucket()
    OUTPUTS_FILE.unlink(missing_ok=True)


def load_outputs() -> dict[str, str]:
    if not OUTPUTS_FILE.exists():
        raise FileNotFoundError(f"{OUTPUTS_FILE.name} not found; run `python deploy.py deploy` first.")
    return json.loads(OUTPUTS_FILE.read_text())


def invoke_agent(prompt: str, actor_id: str, session_id: str | None = None) -> dict[str, Any]:
    """Send one message to the deployed agent. Runtime session ids must be at least 33 characters."""
    response = client(AGENTCORE).invoke_agent_runtime(
        agentRuntimeArn=load_outputs()["runtime_arn"],
        runtimeSessionId=session_id or f"session-{uuid.uuid4()}",
        payload=json.dumps({"prompt": prompt, "actor_id": actor_id}).encode(),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(response["response"].read())


def call_tool(tool: str, arguments: dict[str, Any], actor_id: str) -> dict[str, Any]:
    """Call a Gateway tool directly over MCP, bypassing the model; Policy still authorizes the call.

    Like the agent runtime, this trusted caller binds the call to `actor_id` as the requester.
    """
    outputs = load_outputs()
    with gateway_client(outputs["gateway_url"], outputs["region"]) as mcp:
        return mcp.call_tool_sync(
            f"direct-{uuid.uuid4()}",
            f"{TARGET_NAME}{TOOL_SEPARATOR}{tool}",
            {**arguments, REQUESTER_FIELD: actor_id},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("deploy", help="create or update the whole stack")
    chat = commands.add_parser("invoke", help="send one message to the deployed agent")
    chat.add_argument("prompt")
    chat.add_argument("--actor", default="CUST-001", help="signed-in customer id (default: CUST-001)")
    chat.add_argument("--session", help="runtime session id (min. 33 characters); a new session if omitted")
    direct = commands.add_parser("call-tool", help="call a Gateway tool directly, without the model")
    direct.add_argument("tool", help="get_order, get_customer or refund_customer")
    direct.add_argument("arguments", type=json.loads, help="tool arguments as a JSON object")
    direct.add_argument("--actor", default="CUST-001", help="signed-in customer id (default: CUST-001)")
    commands.add_parser("destroy", help="delete the stack")
    args = parser.parse_args()

    if args.command == "deploy":
        deploy()
    elif args.command == "invoke":
        print(json.dumps(invoke_agent(args.prompt, args.actor, args.session), indent=2))
    elif args.command == "call-tool":
        print(json.dumps(call_tool(args.tool, args.arguments, args.actor), indent=2, default=str))
    else:
        destroy()


if __name__ == "__main__":
    main()
