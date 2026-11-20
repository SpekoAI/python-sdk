"""Resource namespaces mounted on the ``Speko`` / ``AsyncSpeko`` clients."""

from spekoai.resources.agents import (
    AgentsResource,
    AgentToolsResource,
    AsyncAgentsResource,
    AsyncAgentToolsResource,
)
from spekoai.resources.callbacks import AsyncCallbacksResource, CallbacksResource
from spekoai.resources.calls import AsyncCallsResource, CallsResource
from spekoai.resources.credits import AsyncCreditsResource, CreditsResource
from spekoai.resources.knowledge_bases import (
    AsyncKnowledgeBasesResource,
    KnowledgeBasesResource,
)
from spekoai.resources.phone_numbers import (
    AsyncPhoneNumbersResource,
    PhoneNumbersResource,
)
from spekoai.resources.sessions import AsyncSessionsResource, SessionsResource
from spekoai.resources.sms import AsyncSmsResource, SmsResource
from spekoai.resources.usage import AsyncUsageResource, UsageResource
from spekoai.resources.voice import AsyncVoiceResource, VoiceResource
from spekoai.resources.voices import AsyncVoicesResource, VoicesResource
from spekoai.resources.webhooks import (
    AsyncWebhookDeliveriesResource,
    AsyncWebhooksResource,
    WebhookDeliveriesResource,
    WebhooksResource,
)

__all__ = [
    "AgentsResource",
    "AgentToolsResource",
    "AsyncAgentsResource",
    "AsyncAgentToolsResource",
    "AsyncCallbacksResource",
    "CallbacksResource",
    "AsyncCallsResource",
    "CallsResource",
    "AsyncCreditsResource",
    "CreditsResource",
    "AsyncKnowledgeBasesResource",
    "KnowledgeBasesResource",
    "AsyncPhoneNumbersResource",
    "PhoneNumbersResource",
    "AsyncSessionsResource",
    "SessionsResource",
    "AsyncSmsResource",
    "SmsResource",
    "AsyncUsageResource",
    "UsageResource",
    "AsyncVoiceResource",
    "VoiceResource",
    "AsyncVoicesResource",
    "VoicesResource",
    "AsyncWebhookDeliveriesResource",
    "AsyncWebhooksResource",
    "WebhookDeliveriesResource",
    "WebhooksResource",
]
