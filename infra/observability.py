"""CloudWatch Transaction Search for the agent's spans, and log and trace delivery for the Gateway and Memory."""

import contextlib
import json
from typing import Any

from infra.aws import account_id, arn, client, ensure_log_group, ignore_missing, list_all
from infra.config import PREFIX

OBSERVED_RESOURCES = ("gateway", "memory")


def enable_observability(resources: dict[str, str]) -> None:
    """Send spans to CloudWatch Transaction Search, and the logs and traces of `resources` to CloudWatch."""
    logs, xray = client("logs"), client("xray")
    xray_to_logs = {
        "Effect": "Allow",
        "Principal": {"Service": "xray.amazonaws.com"},
        "Action": "logs:PutLogEvents",
        "Resource": [arn("logs", "log-group:aws/spans:*"), arn("logs", "log-group:/aws/application-signals/data:*")],
        "Condition": {
            "ArnLike": {"aws:SourceArn": arn("xray", "*")},
            "StringEquals": {"aws:SourceAccount": account_id()},
        },
    }
    logs.put_resource_policy(
        policyName="AgentCoreTransactionSearch",
        policyDocument=json.dumps({"Version": "2012-10-17", "Statement": [xray_to_logs]}),
    )
    if xray.get_trace_segment_destination()["Destination"] != "CloudWatchLogs":
        xray.update_trace_segment_destination(Destination="CloudWatchLogs")

    for kind, resource_arn in resources.items():
        log_group_arn = ensure_log_group(f"/aws/vendedlogs/bedrock-agentcore/{PREFIX}-{kind}")
        _deliver(
            f"{PREFIX}-{kind}-logs",
            resource_arn,
            "APPLICATION_LOGS",
            deliveryDestinationConfiguration={"destinationResourceArn": log_group_arn},
        )
        _deliver(f"{PREFIX}-{kind}-traces", resource_arn, "TRACES", deliveryDestinationType="XRAY")


def destroy_deliveries() -> None:
    """Remove the log and trace deliveries; the log groups are kept so logs and traces remain as evidence."""
    logs = client("logs")
    deliveries = {f"{PREFIX}-{kind}-{signal}" for kind in OBSERVED_RESOURCES for signal in ("logs", "traces")}
    for delivery in list_all(logs, "describe_deliveries", "deliveries"):
        if delivery["deliverySourceName"] in deliveries:
            logs.delete_delivery(id=delivery["id"])
    for name in deliveries:
        ignore_missing(logs.delete_delivery_source, name=name)
        ignore_missing(logs.delete_delivery_destination, name=name)


def _deliver(name: str, resource_arn: str, log_type: str, **destination: Any) -> None:
    logs = client("logs")
    logs.put_delivery_source(name=name, resourceArn=resource_arn, logType=log_type)
    destination_arn = logs.put_delivery_destination(name=name, **destination)["deliveryDestination"]["arn"]
    with contextlib.suppress(logs.exceptions.ConflictException):  # already delivering
        logs.create_delivery(deliverySourceName=name, deliveryDestinationArn=destination_arn)
