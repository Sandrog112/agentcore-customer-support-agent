"""AgentCore Gateway (MCP protocol, IAM inbound auth) with the Lambda tool backend as its target."""

import functools
import json

from infra.aws import client, find, list_all, wait_for, wait_gone
from infra.config import PREFIX, ROOT, TARGET_NAME


def deploy_gateway(role_arn: str, function_arn: str) -> dict:
    control = client("bedrock-agentcore-control")
    gateway = find(list_all(control, "list_gateways", "items"), PREFIX)
    if gateway is None:
        gateway = control.create_gateway(
            name=PREFIX,
            description="Customer support business tools",
            roleArn=role_arn,
            protocolType="MCP",
            authorizerType="AWS_IAM",
        )
    gateway_id = gateway["gatewayId"]
    gateway = wait_for(functools.partial(control.get_gateway, gatewayIdentifier=gateway_id))

    target = {
        "targetConfiguration": {
            "mcp": {
                "lambda": {
                    "lambdaArn": function_arn,
                    "toolSchema": {"inlinePayload": json.loads((ROOT / "backend" / "tools.json").read_text())},
                }
            }
        },
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    }
    existing = find(list_all(control, "list_gateway_targets", "items", gatewayIdentifier=gateway_id), TARGET_NAME)
    if existing is None:
        created = control.create_gateway_target(gatewayIdentifier=gateway_id, name=TARGET_NAME, **target)
    else:
        created = control.update_gateway_target(
            gatewayIdentifier=gateway_id, targetId=existing["targetId"], name=TARGET_NAME, **target
        )
    wait_for(functools.partial(control.get_gateway_target, gatewayIdentifier=gateway_id, targetId=created["targetId"]))
    return gateway


def mcp_url(gateway: dict) -> str:
    url = gateway["gatewayUrl"].rstrip("/")
    return url if url.endswith("/mcp") else f"{url}/mcp"


def destroy_gateway() -> None:
    control = client("bedrock-agentcore-control")
    gateway = find(list_all(control, "list_gateways", "items"), PREFIX)
    if gateway is None:
        return
    gateway_id = gateway["gatewayId"]
    for target in list_all(control, "list_gateway_targets", "items", gatewayIdentifier=gateway_id):
        control.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target["targetId"])
    wait_gone(lambda: list_all(control, "list_gateway_targets", "items", gatewayIdentifier=gateway_id))
    control.delete_gateway(gatewayIdentifier=gateway_id)
    wait_gone(lambda: find(list_all(control, "list_gateways", "items"), PREFIX))
