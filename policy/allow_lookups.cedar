// Order and customer lookups are allowed; the backend limits them to the signed-in customer's own data.
permit (
  principal is AgentCore::IamEntity,
  action in [
    AgentCore::Action::"support-tools___get_order",
    AgentCore::Action::"support-tools___get_customer"
  ],
  resource == AgentCore::Gateway::"{{GATEWAY_ARN}}"
);
