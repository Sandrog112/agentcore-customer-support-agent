"""AgentCore Memory: conversation events per session plus long-term customer preferences per actor."""

from bedrock_agentcore.memory import MemoryClient

from agent.config import PREFERENCES_NAMESPACE
from infra.aws import client, list_all, session
from infra.config import NAME


def deploy_memory() -> dict:
    return MemoryClient(region_name=session().region_name).create_or_get_memory(
        name=NAME,
        description="Support conversations and long-term customer preferences",
        strategies=[
            {
                "userPreferenceMemoryStrategy": {
                    "name": "CustomerPreferences",
                    "namespaceTemplates": [PREFERENCES_NAMESPACE],
                }
            }
        ],
        event_expiry_days=30,
    )


def destroy_memory() -> None:
    control = client("bedrock-agentcore-control")
    for memory in list_all(control, "list_memories", "memories"):
        if memory["id"].startswith(f"{NAME}-"):
            control.delete_memory(memoryId=memory["id"])
