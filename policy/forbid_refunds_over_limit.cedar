// Refunds over the refund limit ($1,000, REFUND_LIMIT in infra/config.py) are denied whatever the model is told;
// a matching forbid always wins over a permit. The backend also caps each order's refunded total at the same
// limit, so a refund cannot be split to get around this.
forbid (
  principal,
  action == AgentCore::Action::"support-tools___refund_customer",
  resource == AgentCore::Gateway::"{{GATEWAY_ARN}}"
)
when { context.input.amount.greaterThan(decimal("{{REFUND_LIMIT}}")) };
