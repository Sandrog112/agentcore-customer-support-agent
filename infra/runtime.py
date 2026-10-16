"""AgentCore Runtime: the agent code is zipped with its Linux ARM64 dependencies and deployed from S3."""

import functools
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from botocore.exceptions import ClientError

from infra.aws import account_id, client, find, ignore_missing, list_all, session, wait_for
from infra.config import BUILD_DIR, NAME, PREFIX, PYTHON_VERSION, ROOT


def deploy_runtime(role_arn: str, environment: dict[str, str]) -> dict:
    archive = build_runtime_package()
    bucket, key = ensure_bucket(), f"runtime/{NAME}-{time.strftime('%Y%m%d%H%M%S')}.zip"
    client("s3").upload_file(str(archive), bucket, key)
    settings = {
        "agentRuntimeArtifact": {
            "codeConfiguration": {
                "code": {"s3": {"bucket": bucket, "prefix": key}},
                "runtime": f"PYTHON_{PYTHON_VERSION.replace('.', '_')}",
                # The ADOT launcher exports agent, model and tool spans to CloudWatch.
                "entryPoint": ["opentelemetry-instrument", "agent/main.py"],
            }
        },
        "roleArn": role_arn,
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "environmentVariables": environment,
    }
    control = client("bedrock-agentcore-control")
    existing = find(list_all(control, "list_agent_runtimes", "agentRuntimes"), NAME, key="agentRuntimeName")
    if existing:
        runtime = control.update_agent_runtime(agentRuntimeId=existing["agentRuntimeId"], **settings)
    else:
        runtime = control.create_agent_runtime(agentRuntimeName=NAME, description="Customer support agent", **settings)
    return wait_for(functools.partial(control.get_agent_runtime, agentRuntimeId=runtime["agentRuntimeId"]))


def build_runtime_package() -> Path:
    """Zip the agent code with Linux ARM64 wheels of its dependencies (AgentCore direct code deployment)."""
    if shutil.which("uv") is None:
        sys.exit("uv is required to build the runtime package: https://docs.astral.sh/uv/")
    shutil.rmtree(BUILD_DIR, ignore_errors=True)
    package_dir, requirements = BUILD_DIR / "package", BUILD_DIR / "requirements.txt"
    package_dir.mkdir(parents=True)
    export = ["uv", "export", "--frozen", "--no-dev", "--no-hashes", "--no-emit-project", "-o", str(requirements)]
    subprocess.run(export, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    # AgentCore Runtime runs on Linux ARM64, so install wheels for that platform whatever the host is.
    platform = [
        "--python-platform",
        "aarch64-manylinux2014",
        "--python-version",
        PYTHON_VERSION,
        "--only-binary",
        ":all:",
    ]
    install = ["uv", "pip", "install", "--quiet", *platform, "--target", str(package_dir), "-r", str(requirements)]
    subprocess.run(install, check=True)
    for script in (package_dir / "bin").glob("*"):  # console scripts must not point at the local interpreter
        lines = script.read_text().splitlines(keepends=True)
        if lines and lines[0].startswith("#!"):
            script.write_text("#!/usr/bin/env python3\n" + "".join(lines[1:]))
    shutil.copytree(ROOT / "agent", package_dir / "agent", ignore=shutil.ignore_patterns("__pycache__"))
    archive = BUILD_DIR / "runtime.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        for path in sorted(package_dir.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                package.write(path, path.relative_to(package_dir))
    return archive


def ensure_bucket() -> str:
    s3, name, region = client("s3"), _bucket_name(), session().region_name
    try:
        s3.head_bucket(Bucket=name)
    except ClientError:
        location = {} if region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": region}}
        s3.create_bucket(Bucket=name, **location)
    return name


def destroy_runtime() -> None:
    control = client("bedrock-agentcore-control")
    for runtime in list_all(control, "list_agent_runtimes", "agentRuntimes"):
        if runtime["agentRuntimeName"] == NAME:
            control.delete_agent_runtime(agentRuntimeId=runtime["agentRuntimeId"])


def destroy_bucket() -> None:
    bucket = session().resource("s3").Bucket(_bucket_name())
    ignore_missing(bucket.objects.all().delete)
    ignore_missing(bucket.delete)


def _bucket_name() -> str:
    return f"{PREFIX}-artifacts-{account_id()}-{session().region_name}"
