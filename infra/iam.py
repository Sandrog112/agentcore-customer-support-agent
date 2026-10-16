"""IAM roles of the stack: each is trusted by one service and allowed only what that service needs."""

import json
import time
from typing import Any

from backend.store import KEY, ORDER_PREFIX, REFUND_PREFIX
from infra.aws import account_id, arn, client, ignore_missing
from infra.config import AGENTCORE, FUNCTION_NAME, NAME, PREFIX, ROLE_POLICY, ROLES, TABLE_NAME


def deploy_roles(function_arn: str) -> dict[str, str]:
    """Create or update the Lambda, Gateway and Runtime roles; returns their ARNs keyed like ROLES."""
    agentcore = f"{AGENTCORE}.amazonaws.com"
    return {
        "lambda": ensure_role(ROLES["lambda"], "lambda.amazonaws.com", lambda_statements()),
        "gateway": ensure_role(
            ROLES["gateway"], agentcore, gateway_statements(function_arn), arn(AGENTCORE, f"gateway/{PREFIX}-*")
        ),
        "runtime": ensure_role(ROLES["runtime"], agentcore, runtime_statements(), arn(AGENTCORE, f"runtime/{NAME}-*")),
    }


def destroy_roles() -> None:
    for role in ROLES.values():
        ignore_missing(client("iam").delete_role_policy, RoleName=role, PolicyName=ROLE_POLICY)
        ignore_missing(client("iam").delete_role, RoleName=role)


def ensure_role(name: str, service: str, statements: list[dict], source_arn: str | None = None) -> str:
    """Create or update a role trusted by `service` with a single inline least-privilege policy.

    `source_arn` limits which of the service's resources may assume the role (confused-deputy protection).
    """
    trust: dict[str, Any] = {"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}
    if source_arn:
        trust["Condition"] = {
            "StringEquals": {"aws:SourceAccount": account_id()},
            "ArnLike": {"aws:SourceArn": source_arn},
        }
    trust_policy = json.dumps({"Version": "2012-10-17", "Statement": [trust]})
    iam = client("iam")
    try:
        iam.create_role(RoleName=name, AssumeRolePolicyDocument=trust_policy)
        created = True
    except iam.exceptions.EntityAlreadyExistsException:
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=trust_policy)
        created = False
    iam.put_role_policy(
        RoleName=name,
        PolicyName=ROLE_POLICY,
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements}),
    )
    if created:
        time.sleep(10)  # a new role takes a few seconds before services can assume it
    return arn("iam", f"role/{name}")


def lambda_statements() -> list[dict]:
    table = arn("dynamodb", f"table/{TABLE_NAME}")
    return [
        {"Effect": "Allow", "Action": "dynamodb:GetItem", "Resource": table},
        # Refunds are the only writes: new refund items, and the refunded_total of their order.
        {
            "Effect": "Allow",
            "Action": "dynamodb:PutItem",
            "Resource": table,
            "Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"{REFUND_PREFIX}*"]}},
        },
        {
            "Effect": "Allow",
            "Action": "dynamodb:UpdateItem",
            "Resource": table,
            "Condition": {
                "ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"{ORDER_PREFIX}*"]},
                "ForAllValues:StringEquals": {"dynamodb:Attributes": [KEY, "refunded_total"]},
            },
        },
        {
            "Effect": "Allow",
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": arn("logs", f"log-group:/aws/lambda/{FUNCTION_NAME}:*"),
        },
        {"Effect": "Allow", "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords"], "Resource": "*"},
    ]


def gateway_statements(function_arn: str) -> list[dict]:
    return [
        {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": function_arn},
        # Required by the attached policy engine to authorize tool calls and filter the tool list.
        {
            "Effect": "Allow",
            "Action": [
                f"{AGENTCORE}:AuthorizeAction",
                f"{AGENTCORE}:PartiallyAuthorizeActions",
                f"{AGENTCORE}:GetPolicyEngine",
            ],
            "Resource": [arn(AGENTCORE, f"policy-engine/{NAME}-*"), arn(AGENTCORE, f"gateway/{PREFIX}-*")],
        },
    ]


def runtime_statements() -> list[dict]:
    runtime_logs = "log-group:/aws/bedrock-agentcore/runtimes/*"
    return [
        {"Effect": "Allow", "Action": "bedrock-mantle:CreateInference", "Resource": arn("bedrock-mantle", "project/*")},
        {"Effect": "Allow", "Action": f"{AGENTCORE}:InvokeGateway", "Resource": arn(AGENTCORE, f"gateway/{PREFIX}-*")},
        {
            "Effect": "Allow",
            "Action": [
                f"{AGENTCORE}:{action}" for action in ("CreateEvent", "GetEvent", "ListEvents", "RetrieveMemoryRecords")
            ],
            "Resource": arn(AGENTCORE, f"memory/{NAME}-*"),
        },
        {
            "Effect": "Allow",
            "Action": [f"{AGENTCORE}:GetWorkloadAccessToken", f"{AGENTCORE}:GetWorkloadAccessTokenForUserId"],
            "Resource": [
                arn(AGENTCORE, "workload-identity-directory/default"),
                arn(AGENTCORE, f"workload-identity-directory/default/workload-identity/{NAME}-*"),
            ],
        },
        {
            "Effect": "Allow",
            "Action": ["logs:CreateLogGroup", "logs:DescribeLogStreams"],
            "Resource": arn("logs", runtime_logs),
        },
        {
            "Effect": "Allow",
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": arn("logs", f"{runtime_logs}:log-stream:*"),
        },
        {"Effect": "Allow", "Action": "logs:DescribeLogGroups", "Resource": arn("logs", "log-group:*")},
        {
            "Effect": "Allow",
            "Action": [
                "xray:PutTraceSegments",
                "xray:PutTelemetryRecords",
                "xray:GetSamplingRules",
                "xray:GetSamplingTargets",
            ],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": "cloudwatch:PutMetricData",
            "Resource": "*",
            "Condition": {"StringEquals": {"cloudwatch:namespace": AGENTCORE}},
        },
    ]
