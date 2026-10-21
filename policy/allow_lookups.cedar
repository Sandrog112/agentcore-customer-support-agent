// Order and customer lookups are allowed; the backend limits them to the signed-in customer's own data.
// Only the agent runtime and the operator who deployed the stack (deploy.py call-tool) are permitted: the backend
// trusts their requester_id, and any other IAM identity in the account matches no permit, so it is denied.
permit (
  principal is AgentCore::IamEntity,
  action in [
    AgentCore::Action::"support-tools___get_order",
    AgentCore::Action::"support-tools___get_customer"
  ],
  resource == AgentCore::Gateway::"{{GATEWAY_ARN}}"
)
when { principal.id == "{{RUNTIME_PRINCIPAL}}" || principal.id == "{{OPERATOR_PRINCIPAL}}" };
