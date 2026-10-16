"""Names and paths of the support stack's resources."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"
OUTPUTS_FILE = ROOT / "deploy-outputs.json"

PREFIX = "support-agent"  # for names that allow hyphens
NAME = "support_agent"  # runtime, memory and policy names allow underscores only
TARGET_NAME = "support-tools"  # Gateway tool names become "support-tools___<tool>", see policy/*.cedar
TABLE_NAME = f"{PREFIX}-data"
FUNCTION_NAME = f"{PREFIX}-tools"
ROLES = {"lambda": f"{PREFIX}-tools", "gateway": f"{PREFIX}-gateway", "runtime": f"{PREFIX}-runtime"}
ROLE_POLICY = "least-privilege"
PYTHON_VERSION = "3.13"
AGENTCORE = "bedrock-agentcore"
# USD: AgentCore Policy denies any refund above it (policy/*.cedar), and the backend caps each order's refunded
# total at it (backend/handler.py), so a large refund cannot be split into allowed ones.
REFUND_LIMIT = "1000.00"
