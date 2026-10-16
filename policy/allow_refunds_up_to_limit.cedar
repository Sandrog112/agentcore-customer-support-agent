// Refunds up to the refund limit ($1,000, REFUND_LIMIT in infra/config.py) per call are allowed. Without a
// matching permit every tool call is denied.
permit (
  principal is AgentCore::IamEntity,
  action == AgentCore::Action::"support-tools___refund_customer",
  resource == AgentCore::Gateway::"{{GATEWAY_ARN}}"
)
when { context.input.amount.lessThanOrEqual(decimal("{{REFUND_LIMIT}}")) };
