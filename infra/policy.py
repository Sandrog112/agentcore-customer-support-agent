"""AgentCore Policy: a Cedar policy engine attached to the Gateway in ENFORCE mode, one policy per policy/*.cedar."""

import functools

from infra.aws import arn, caller_principal, client, find, list_all, wait_for, wait_gone
from infra.config import NAME, REFUND_LIMIT, ROLES, ROOT

POLICY_DIR = ROOT / "policy"


def cedar_policies(placeholders: dict[str, str]) -> dict[str, str]:
    """Cedar statements with their {{PLACEHOLDERS}} filled in, keyed by policy name (the file name)."""
    return {path.stem: _statement(path.read_text(), placeholders) for path in sorted(POLICY_DIR.glob("*.cedar"))}


def _statement(text: str, placeholders: dict[str, str]) -> str:
    for name, value in placeholders.items():
        text = text.replace(f"{{{{{name}}}}}", value)
    # The comments document the files; only the statement itself is stored in the policy engine.
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//")).strip()


def deploy_policies(gateway: dict) -> str:
    control = client("bedrock-agentcore-control")
    engine = find(list_all(control, "list_policy_engines", "policyEngines"), NAME)
    if engine is None:
        engine = control.create_policy_engine(name=NAME, description="Authorization rules for the support tools")
    engine_id = engine["policyEngineId"]
    wait_for(functools.partial(control.get_policy_engine, policyEngineId=engine_id), ready="ACTIVE")

    existing = {
        policy["name"]: policy for policy in list_all(control, "list_policies", "policies", policyEngineId=engine_id)
    }
    statements = cedar_policies(
        {
            "GATEWAY_ARN": gateway["gatewayArn"],
            "REFUND_LIMIT": REFUND_LIMIT,
            "RUNTIME_PRINCIPAL": arn("sts", f"assumed-role/{ROLES['runtime']}"),
            "OPERATOR_PRINCIPAL": caller_principal(),
        }
    )
    for name in existing.keys() - statements.keys():  # its policy/*.cedar file was renamed or removed
        control.delete_policy(policyEngineId=engine_id, policyId=existing[name]["policyId"])
    for name, statement in statements.items():
        settings = {"definition": {"cedar": {"statement": statement}}, "validationMode": "FAIL_ON_ANY_FINDINGS"}
        if name in existing:
            policy = control.update_policy(policyEngineId=engine_id, policyId=existing[name]["policyId"], **settings)
        else:
            policy = control.create_policy(policyEngineId=engine_id, name=name, **settings)
        wait_for(
            functools.partial(control.get_policy, policyEngineId=engine_id, policyId=policy["policyId"]), ready="ACTIVE"
        )

    # ENFORCE: every tool call is authorized at the Gateway; anything not permitted is denied.
    control.update_gateway(
        gatewayIdentifier=gateway["gatewayId"],
        name=gateway["name"],
        roleArn=gateway["roleArn"],
        protocolType="MCP",
        authorizerType="AWS_IAM",
        policyEngineConfiguration={"arn": engine["policyEngineArn"], "mode": "ENFORCE"},
    )
    wait_for(functools.partial(control.get_gateway, gatewayIdentifier=gateway["gatewayId"]))
    return engine_id


def destroy_policies() -> None:
    control = client("bedrock-agentcore-control")
    engine = find(list_all(control, "list_policy_engines", "policyEngines"), NAME)
    if engine is None:
        return
    engine_id = engine["policyEngineId"]
    for policy in list_all(control, "list_policies", "policies", policyEngineId=engine_id):
        control.delete_policy(policyEngineId=engine_id, policyId=policy["policyId"])
    wait_gone(lambda: list_all(control, "list_policies", "policies", policyEngineId=engine_id))
    control.delete_policy_engine(policyEngineId=engine_id)
