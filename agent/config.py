"""Settings shared by the agent and the deployment (infra/), kept free of heavy imports."""

# Long-term memory namespace of the extracted customer preferences; {actorId} is the customer id.
PREFERENCES_NAMESPACE = "/users/{actorId}/preferences/"
