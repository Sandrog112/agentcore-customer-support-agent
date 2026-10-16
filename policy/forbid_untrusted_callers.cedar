// Only the agent runtime and the operator who deployed the stack (deploy.py call-tool) may call the tools: the
// backend trusts their requester_id, so no other IAM identity in the account may reach it.
forbid (
  principal is AgentCore::IamEntity,
  action,
  resource is AgentCore::Gateway
)
unless { principal.id == "{{RUNTIME_PRINCIPAL}}" || principal.id == "{{OPERATOR_PRINCIPAL}}" };
