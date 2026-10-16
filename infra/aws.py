"""boto3 session and the small helpers shared by the deployment steps."""

import contextlib
import functools
import sys
import time
from collections.abc import Callable
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

_MISSING = {"ResourceNotFoundException", "NoSuchEntity", "NoSuchBucket", "NotFoundException"}


@functools.cache
def session() -> boto3.Session:
    aws = boto3.Session()
    if not aws.region_name:
        sys.exit("Set AWS_REGION, see .env.example.")
    return aws


@functools.cache
def client(service: str) -> Any:
    # Agent invocations can take minutes when tools time out and are retried.
    return session().client(service, config=Config(read_timeout=600, retries={"mode": "standard", "max_attempts": 8}))


@functools.cache
def account_id() -> str:
    return client("sts").get_caller_identity()["Account"]


@functools.cache
def caller_principal() -> str:
    """ARN of the deploying identity as AgentCore Policy sees it: an assumed role without its session name."""
    caller = client("sts").get_caller_identity()["Arn"]
    return caller.rsplit("/", 1)[0] if ":assumed-role/" in caller else caller


def arn(service: str, resource: str) -> str:
    region = "" if service in ("iam", "sts") else session().region_name
    return f"arn:aws:{service}:{region}:{account_id()}:{resource}"


def list_all(service_client: Any, operation: str, key: str, **kwargs: Any) -> list[dict]:
    return [item for page in service_client.get_paginator(operation).paginate(**kwargs) for item in page[key]]


def find(items: list[dict], value: str, key: str = "name") -> dict | None:
    return next((item for item in items if item.get(key) == value), None)


def wait_for(describe: Callable[[], dict], ready: str = "READY", timeout: int = 900) -> dict:
    """Poll a resource until it reaches `ready`, failing fast on a *FAILED status."""
    deadline = time.monotonic() + timeout
    while True:
        resource = describe()
        status = resource.get("status", "")
        if status == ready:
            return resource
        if status.endswith("FAILED"):
            raise RuntimeError(f"Resource failed: {resource.get('statusReasons') or resource.get('failureReason')}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"Timed out waiting for status {ready}; last status was {status}.")
        time.sleep(5)


def wait_gone(probe: Callable[[], Any], timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    while probe():
        if time.monotonic() > deadline:
            raise TimeoutError("Timed out waiting for deletion.")
        time.sleep(5)


def ignore_missing(call: Callable[..., Any], **kwargs: Any) -> None:
    try:
        call(**kwargs)
    except ClientError as error:
        if error.response["Error"]["Code"] not in _MISSING:
            raise


def ensure_log_group(name: str, retention_days: int = 14) -> str:
    logs = client("logs")
    with contextlib.suppress(logs.exceptions.ResourceAlreadyExistsException):
        logs.create_log_group(logGroupName=name)
    logs.put_retention_policy(logGroupName=name, retentionInDays=retention_days)
    return arn("logs", f"log-group:{name}")


def step(message: str) -> None:
    print(f"==> {message}", flush=True)
