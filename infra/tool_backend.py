"""DynamoDB table with the demo data, and the Lambda function that implements the Gateway tools."""

import io
import json
import zipfile
from decimal import Decimal

from backend.store import Store, create_table
from infra.aws import client, ensure_log_group, ignore_missing, session
from infra.config import FUNCTION_NAME, PYTHON_VERSION, REFUND_LIMIT, ROOT, TABLE_NAME

BACKEND_DIR = ROOT / "backend"


def deploy_table() -> None:
    create_table(client("dynamodb"), TABLE_NAME)
    items = json.loads((BACKEND_DIR / "seed_data.json").read_text(), parse_float=Decimal)
    Store(TABLE_NAME, session().resource("dynamodb")).seed(items)


def deploy_lambda(role_arn: str) -> None:
    ensure_log_group(f"/aws/lambda/{FUNCTION_NAME}")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        for module in sorted(BACKEND_DIR.glob("*.py")):
            package.write(module, f"backend/{module.name}")
    settings = {
        "Role": role_arn,
        "Handler": "backend.handler.lambda_handler",
        "Runtime": f"python{PYTHON_VERSION}",
        "Timeout": 15,
        "MemorySize": 256,
        "Environment": {
            "Variables": {
                "TABLE_NAME": TABLE_NAME,
                "REFUND_LIMIT": REFUND_LIMIT,
                # This is the demo stack, so the failure scenarios of backend/faults.py are switched on.
                "FAULT_INJECTION": "true",
            }
        },
        "TracingConfig": {"Mode": "Active"},
        "LoggingConfig": {"LogFormat": "JSON", "ApplicationLogLevel": "INFO", "SystemLogLevel": "WARN"},
    }
    lambda_client = client("lambda")
    try:
        lambda_client.create_function(
            FunctionName=FUNCTION_NAME, Code={"ZipFile": archive.getvalue()}, Architectures=["arm64"], **settings
        )
    except lambda_client.exceptions.ResourceConflictException:
        lambda_client.update_function_code(
            FunctionName=FUNCTION_NAME, ZipFile=archive.getvalue(), Architectures=["arm64"]
        )
        lambda_client.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)
        lambda_client.update_function_configuration(FunctionName=FUNCTION_NAME, **settings)
    lambda_client.get_waiter("function_active_v2").wait(FunctionName=FUNCTION_NAME)
    lambda_client.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)


def destroy_tool_backend() -> None:
    ignore_missing(client("lambda").delete_function, FunctionName=FUNCTION_NAME)
    ignore_missing(client("dynamodb").delete_table, TableName=TABLE_NAME)
