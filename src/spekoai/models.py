"""Pydantic models and literal types for the Speko Python SDK.

Most models serialize/validate using camelCase aliases to match the wire
protocol, while exposing snake_case attributes on the Python side
(``_SpekoModel``). A handful of endpoints (calls, callbacks, call events,
transfers) serialize snake_case on the wire — those models subclass
``_SpekoSnakeModel`` and their field names match the wire directly.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

OptimizeFor = Literal["balanced", "accuracy", "latency", "cost"]
ProviderModality = Literal["stt", "llm", "tts"]
ChatRole = Literal["system", "user", "assistant", "tool"]
KeySource = Literal["BYOK", "MANAGED"]
CreditLedgerKind = Literal["grant", "debit", "topup", "refund", "adjustment"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]


class _SpekoModel(BaseModel):
    """Base model: camelCase wire aliases, snake_case Python fields."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )


class _SpekoSnakeModel(BaseModel):
    """Base model for endpoints whose wire format is snake_case."""

    model_config = ConfigDict(extra="ignore")


class AllowedProviders(_SpekoModel):
    stt: Optional[list[str]] = None
    llm: Optional[list[str]] = None
    tts: Optional[list[str]] = None


class PipelineConstraints(_SpekoModel):
    """Optional allowlists layered on top of RoutingIntent.

    The router still ranks candidates by benchmark score — but if an
    ``allowed_providers`` list is set for a modality, only that subset
    is considered.
    """

    allowed_providers: Optional[AllowedProviders] = None


class RoutingIntent(_SpekoModel):
    """Routing signal for the Speko router.

    - ``language``: BCP-47 tag, e.g. ``"en"`` or ``"es-MX"``.
    - ``region``: optional region for streaming-provider rankings
      (e.g. ``"us-east4"``, ``"eu-west1"``). Defaults server-side to
      ``"global"``, which surfaces region-agnostic (batch) benchmark rows.
      Set this when latency to a specific geography matters — STT/TTS
      rankings differ per region.
    - ``optimize_for``: preset that biases the weighted score.
    """

    language: str
    region: Optional[str] = None
    optimize_for: Optional[OptimizeFor] = None


# --- Chat / tools -------------------------------------------------------------


class ChatToolCall(_SpekoModel):
    """One LLM-emitted tool invocation.

    ``args`` is a JSON-encoded string (LLMs may stream partial JSON; the
    proxy guarantees a complete, parseable string).
    """

    id: str
    name: str
    args: str


class ChatMessage(_SpekoModel):
    role: ChatRole
    content: str
    # Present on role='assistant' when the model invoked one or more tools.
    tool_calls: Optional[list[ChatToolCall]] = None
    # Required on role='tool' — pairs with the id from a prior assistant tool_calls[].
    tool_call_id: Optional[str] = None
    # Present on role='tool' when the tool execution failed. The proxy
    # translates to provider-native error signals so the LLM sees the failure.
    is_error: Optional[bool] = None


ChatToolExecutionMode = Literal["inline", "webhook", "builtin", "integration"]

# Spoken lead-in behavior before a server-executed tool runs. `auto` lets the
# gateway decide from the tool's recent execution durations; `always` forces a
# spoken lead-in; `never` runs the tool silently.
ChatToolPreToolSpeech = Literal["auto", "always", "never"]


class AgentWebhookAuthHeaderInput(_SpekoModel):
    """Outbound auth header input — ``value`` is the plaintext credential Speko
    encrypts at rest. Required on create; omit on update to keep the value
    already stored under this header's ref."""

    name: str
    value: Optional[str] = None


class AgentWebhookAuthHeader(_SpekoModel):
    """Outbound auth header as returned by the API — value stays server-side."""

    name: str
    secret_ref: str


class ChatToolSourceInline(_SpekoModel):
    kind: Literal["inline"] = "inline"


class ChatToolSourceWebhook(_SpekoModel):
    """Webhook source referenced from a ``ChatTool`` — carries the
    ``secret_ref`` pointer into Speko's secrets store, never the plaintext."""

    kind: Literal["webhook"] = "webhook"
    url: str
    secret_ref: str
    headers: Optional[dict[str, str]] = None
    auth_headers: Optional[list[AgentWebhookAuthHeader]] = None
    timeout_ms: Optional[int] = None
    # `async` returns `async_ack` immediately while Speko dispatches the
    # webhook in the background.
    response_mode: Optional[Literal["sync", "async"]] = None
    async_ack: Optional[str] = None


class ChatToolSourceBuiltin(_SpekoModel):
    kind: Literal["builtin"] = "builtin"
    name: str
    config: Optional[Any] = None


class ChatToolSourceIntegration(_SpekoModel):
    """Integration source — binds the tool to an org-installed Speko app
    action (e.g. Google Calendar ``create_event``)."""

    kind: Literal["integration"] = "integration"
    installation_id: str
    app_key: str
    action_key: str
    config: Optional[Any] = None


ChatToolSource = Union[
    ChatToolSourceInline,
    ChatToolSourceWebhook,
    ChatToolSourceBuiltin,
    ChatToolSourceIntegration,
]


class ChatTool(_SpekoModel):
    """Tool definition exposed to the LLM. ``parameters`` is a JSON Schema
    (draft-7) object.

    ``execution_mode`` and ``source`` are optional and back-compat: omitting
    both preserves inline behavior (the caller runs the tool). Set
    ``execution_mode='webhook'`` with a matching webhook/integration source
    to opt into server-managed execution.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    execution_mode: Optional[ChatToolExecutionMode] = None
    source: Optional[ChatToolSource] = None
    # Spoken lead-in behavior before this tool executes. Defaults to `auto`
    # for registered tools.
    pre_tool_speech: Optional[ChatToolPreToolSpeech] = None


ChatToolChoiceFunctionName = dict[str, Any]
# 'auto' | 'none' | 'required' | {'type': 'function', 'function': {'name': ...}}
ChatToolChoice = Union[Literal["auto", "none", "required"], dict[str, Any]]


# --- Transcribe -------------------------------------------------------------


class SttOptions(_SpekoModel):
    """Provider-facing STT overrides. Routing continues to use the intent
    language; ``language`` here only changes the provider's STT stream."""

    keywords: Optional[list[str]] = None
    # Free-text transcription context (domain, names, expected phrases), up to
    # 2000 chars. Honored only by prompt-capable STT models; others ignore it.
    # Same field as ``AgentSttOptions.prompt`` — per-call value overrides the
    # agent's on ``voice.dial``.
    prompt: Optional[str] = None
    language: Optional[str] = None


class TranscribeResult(_SpekoModel):
    text: str
    provider: str
    model: str
    confidence: Optional[float] = None
    failover_count: int = 0
    scores_run_id: Optional[str] = None


class TranscribeStreamMeta(_SpekoModel):
    type: Literal["meta"] = "meta"
    provider: str
    model: str
    failover_count: int = 0
    scores_run_id: Optional[str] = None


class TranscribeStreamTranscript(_SpekoModel):
    type: Literal["transcript"] = "transcript"
    text: str
    is_final: bool = False
    confidence: Optional[float] = None


class TranscribeStreamDone(TranscribeResult):
    type: Literal["done"] = "done"


class StreamError(_SpekoModel):
    type: Literal["error"] = "error"
    error: str
    code: str = "STREAM_ERROR"


TranscribeStreamEvent = Union[
    TranscribeStreamMeta,
    TranscribeStreamTranscript,
    TranscribeStreamDone,
    StreamError,
]


# --- Synthesize -------------------------------------------------------------


class SynthesizeResult(_SpekoModel):
    """Result of a synthesize call.

    ``audio`` holds the raw bytes. The format depends on the chosen
    provider — check ``content_type`` (ElevenLabs returns
    ``audio/mpeg``; Cartesia returns ``audio/pcm;rate=24000``).
    """

    audio: bytes
    content_type: str
    provider: str
    model: str
    failover_count: int = 0
    scores_run_id: Optional[str] = None


# --- Voices (TTS catalog) ---------------------------------------------------


class VoiceCatalogEntry(_SpekoModel):
    # Routing-key vendor (matches allowed_providers.tts entries).
    vendor: str
    # Voice id passed through to the provider's TTS API.
    id: str
    name: str


class VoicesProviderEntry(_SpekoModel):
    key: str
    name: str
    models: list[str]
    # True when the provider's voice library is account-scoped and must be
    # fetched live from the provider (currently only ElevenLabs).
    voices_fetched_live: bool


class VoicesListResult(_SpekoModel):
    voices: list[VoiceCatalogEntry]
    providers: list[VoicesProviderEntry]


# --- Complete (LLM) ---------------------------------------------------------


class CompleteUsage(_SpekoModel):
    prompt_tokens: int
    completion_tokens: int


class CompleteResult(_SpekoModel):
    text: str
    provider: str
    model: str
    usage: CompleteUsage
    failover_count: int = 0
    scores_run_id: Optional[str] = None
    # Present when the LLM invoked tools instead of (or in addition to)
    # emitting text.
    tool_calls: Optional[list[ChatToolCall]] = None


class CompleteStreamMeta(_SpekoModel):
    type: Literal["meta"] = "meta"
    provider: str
    model: str
    failover_count: int = 0
    total_failover_count: int = 0
    scores_run_id: Optional[str] = None
    hop: int = 0


class CompleteStreamDelta(_SpekoModel):
    type: Literal["delta"] = "delta"
    text: str


class CompleteStreamToolCall(ChatToolCall):
    type: Literal["tool_call"] = "tool_call"


class CompleteStreamServerToolCall(_SpekoModel):
    type: Literal["server_tool_call"] = "server_tool_call"
    id: str
    name: str
    status: Literal["started", "completed", "failed"]


class CompleteStreamDone(CompleteResult):
    type: Literal["done"] = "done"


CompleteStreamEvent = Union[
    CompleteStreamMeta,
    CompleteStreamDelta,
    CompleteStreamToolCall,
    CompleteStreamServerToolCall,
    CompleteStreamDone,
    StreamError,
]


# --- Usage ------------------------------------------------------------------


class UsageByProvider(_SpekoModel):
    provider: str
    type: ProviderModality
    metric: str
    key_source: KeySource
    quantity: float
    cost: float


class UsageSummary(_SpekoModel):
    total_sessions: int
    total_minutes: float
    total_cost: float
    breakdown: list[UsageByProvider]
    balance_usd: float
    currency: Literal["USD"]


# --- Credits ----------------------------------------------------------------


class OrganizationBalance(_SpekoModel):
    balance_usd: float
    currency: Literal["USD"]
    updated_at: str


class CreditLedgerEntry(_SpekoModel):
    id: str
    kind: CreditLedgerKind
    # Signed — positive for grants/topups/refunds, negative for debits.
    # Kept as string so >2**53 values survive JSON.
    amount_micro_usd: str
    metric: Optional[str] = None
    provider: Optional[str] = None
    session_id: Optional[str] = None
    created_at: str


class CreditLedgerPage(_SpekoModel):
    entries: list[CreditLedgerEntry]
    next_cursor: Optional[str] = None


# --- Realtime (S2S) ---------------------------------------------------------

RealtimeProvider = Literal["openai", "google", "xai"]


class RealtimeToolSpec(_SpekoModel):
    name: str
    description: str
    parameters: dict[str, Any]


class RealtimeConnectParams(_SpekoModel):
    """Parameters for opening an S2S realtime session.

    Unlike cascade sessions, realtime bypasses both LiveKit and the Speko
    media path. Speko mints a short-lived provider credential, then this SDK
    connects directly to OpenAI Realtime, Gemini Live, or xAI Grok Voice.
    """

    # Persisted agent whose workspace webhook routes should receive
    # lifecycle events.
    agent_id: Optional[str] = None
    provider: RealtimeProvider
    model: str
    voice: Optional[str] = None
    system_prompt: Optional[str] = None
    temperature: Optional[float] = None
    input_sample_rate: Optional[Literal[16000, 24000]] = None
    output_sample_rate: Optional[Literal[16000, 24000]] = None
    tools: Optional[list[RealtimeToolSpec]] = None
    # Exact-match attributes used only for workspace webhook routing.
    # Requires agent_id.
    webhook_tags: Optional[dict[str, str]] = None
    metadata: Optional[dict[str, object]] = None
    # Max session duration in seconds. Server-capped at 1800 (30 min).
    ttl_seconds: Optional[int] = None
    # Reuse this value when retrying an ambiguous POST /v1/sessions timeout.
    # The SDK generates one when omitted.
    idempotency_key: Optional[str] = None


class RealtimeCredential(_SpekoModel):
    kind: Literal["bearer"]
    value: str
    expires_at: str


class RealtimeTelemetry(_SpekoModel):
    endpoint: str
    token: str
    flush_interval_ms: int


class RealtimeBillingAuthorization(_SpekoModel):
    mode: Literal["direct_entitlement"]
    state: Literal["estimated"]
    maximum_amount_micros: str
    currency: str
    renewal_url: Optional[str] = None
    renewable_until: Optional[str] = None


class RealtimeReservation(_SpekoModel):
    id: str
    authorized_duration_seconds: int
    lease_expires_at: str
    billing: RealtimeBillingAuthorization


class RealtimeProviderSession(_SpekoModel):
    voice: Optional[str] = None
    instructions: Optional[str] = None
    temperature: Optional[float] = None
    tools: Optional[list[RealtimeToolSpec]] = None


class RealtimeSessionInfo(_SpekoModel):
    """Raw response from POST /v1/sessions when mode == 's2s'."""

    mode: Literal["s2s"]
    transport: Literal["provider_direct"]
    session_id: str
    plan_id: str
    attempt_id: str
    provider: RealtimeProvider
    model: str
    adapter: Literal["openai.realtime.v1", "xai.realtime.v1", "google.live.v1"]
    provider_transport: Literal["websocket", "webrtc"]
    endpoint: str
    sideband_url: Optional[str] = None
    credential: RealtimeCredential
    telemetry: RealtimeTelemetry
    reservation: RealtimeReservation
    session: Optional[RealtimeProviderSession] = None
    input_sample_rate: Optional[Literal[16000, 24000]] = None
    output_sample_rate: Optional[Literal[16000, 24000]] = None
    expires_at: str


# --- Voice (phone dial) -------------------------------------------------------


class VoiceDialLlmOptions(_SpekoModel):
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class VoiceDialTtsOptions(_SpekoModel):
    sample_rate: Optional[int] = None
    speed: Optional[float] = None


class TurnHandlingEndpointing(_SpekoModel):
    min_delay: Optional[float] = None
    max_delay: Optional[float] = None


class TurnHandlingInterruption(_SpekoModel):
    mode: Optional[Literal["adaptive", "vad"]] = None
    min_duration: Optional[float] = None
    min_words: Optional[int] = None


class TurnHandlingVad(_SpekoModel):
    """Local VAD for cascaded calls. Omit to use Silero."""

    provider: Literal["silero", "ai-coustics"]


class TurnHandlingNoiseCancellation(_SpekoModel):
    """Caller-leg input enhancement (ai-coustics). Omit for the platform
    default; ``enabled=False`` turns it off; ``model`` is plain Quail (default)
    or Quail Voice Focus (primary-speaker isolation, explicit opt-in)."""

    enabled: bool
    model: Optional[Literal["quail", "quail-voice-focus"]] = None


class TurnHandling(_SpekoModel):
    """Per-call turn-taking overrides. ``greet_first`` defaults ON for
    outbound: the greeting plays immediately while AMD classifies in the
    background. Pass False to hold the greeting for the AMD verdict."""

    vad: Optional[TurnHandlingVad] = None
    noise_cancellation: Optional[TurnHandlingNoiseCancellation] = None
    profile: Optional[Literal["conversational", "ivr", "ivr_patient"]] = None
    endpointing: Optional[TurnHandlingEndpointing] = None
    interruption: Optional[TurnHandlingInterruption] = None
    turn_detection: Optional[Union[bool, Literal["stt"]]] = None
    context_threshold: Optional[bool] = None
    greet_first: Optional[bool] = None


class TelephonyAmd(_SpekoModel):
    mode: Optional[Literal["agent", "carrier", "disabled"]] = None
    timeout_seconds: Optional[float] = None


class TelephonyOptions(_SpekoModel):
    """Per-call SIP routing hints. Carrier AMD requires trunk/provider
    support."""

    region: Optional[str] = None
    amd: Optional[TelephonyAmd] = None


class VoiceDialParams(_SpekoModel):
    """Parameters for ``speko.voice.dial`` — POST /v1/sessions/phone."""

    # Destination number in E.164 format (e.g. "+12015551234").
    to: str
    # Caller ID. Falls back to the org default if omitted server-side.
    from_: Optional[str] = Field(default=None, alias="from")
    # Persisted assistant to run for this call. When supplied, `intent` can
    # be omitted.
    agent_id: Optional[str] = None
    intent: Optional[RoutingIntent] = None
    constraints: Optional[PipelineConstraints] = None
    # TTS voice id passed through to the picked TTS provider.
    voice: Optional[str] = None
    system_prompt: Optional[str] = None
    # Optional first utterance. Omit to use the agent default.
    first_message: Optional[str] = None
    # Call-time values for template variables in system_prompt /
    # first_message. Sending this key (even {}) compiles both strings as
    # Liquid templates at call-create time. Unresolved names fail the request
    # with 400 MISSING_TEMPLATE_VARIABLES. Keys under `system.` are rejected.
    variables: Optional[dict[str, str]] = None
    # Per-call values for TOOLS ONLY (e.g. an access token scoped to the
    # callee, a per-tenant base URL). Never rendered into the prompt or the
    # model's context; stored encrypted and released only to tool execution
    # (`session.secrets.<name>` in custom-code tools, `{{name}}` in webhook
    # url/headers). Identifier-shaped names; up to 32 entries, 6 chars-4 KB each.
    tool_secrets: Optional[dict[str, str]] = None
    llm: Optional[VoiceDialLlmOptions] = None
    tts_options: Optional[VoiceDialTtsOptions] = None
    stt_options: Optional[SttOptions] = None
    # Server-side wall-clock cap in seconds. Clamped server-side to 30s-4h.
    max_duration_seconds: Optional[int] = None
    turn_handling: Optional[TurnHandling] = None
    telephony: Optional[TelephonyOptions] = None
    # Exact-match attributes used only for workspace webhook routing.
    # Requires agent_id.
    webhook_tags: Optional[dict[str, str]] = None
    # Free-form metadata round-tripped to your webhooks.
    metadata: Optional[dict[str, object]] = None


class VoiceDialResult(_SpekoModel):
    session_id: str
    call_control_id: str
    room_name: str
    # 'dialing' on a real call, 'dialing-stub' if managed telephony isn't
    # configured.
    status: Literal["dialing", "dialing-stub"]
    to: str
    from_: str = Field(alias="from")


# --- Sessions -----------------------------------------------------------------


class SessionToolCall(_SpekoModel):
    name: str
    args: str


class SessionTranscriptEntry(_SpekoModel):
    """One turn from GET /v1/sessions/:id/transcript — the lightweight live
    transcript poll. Note the camelCase wire keys: this endpoint's
    serialization differs from the snake_case ``CallTranscriptEntry``
    embedded in ``CallDetail``."""

    id: str
    index: int
    source: Literal["user", "agent", "system"]
    text: str
    started_at: str
    ended_at: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    # Per-stage latency legs (ms) — None on user/system turns.
    eou_ms: Optional[float] = None
    llm_ttft_ms: Optional[float] = None
    tts_ttfb_ms: Optional[float] = None
    latency_status: Optional[Literal["partial", "complete", "interrupted", "error"]] = None
    conversational_latency_ms: Optional[float] = None
    # Tool calls the agent made on this turn (empty when none).
    tool_calls: list[SessionToolCall] = Field(default_factory=list)


class SessionTranscript(_SpekoModel):
    entries: list[SessionTranscriptEntry]


class SessionStreamStatus(_SpekoModel):
    type: Literal["status"] = "status"
    status: str
    ended_at: Optional[str] = None


class SessionStreamTranscript(_SpekoModel):
    type: Literal["transcript"] = "transcript"
    turn: SessionTranscriptEntry


class SessionStreamCallEvent(_SpekoModel):
    type: Literal["event"] = "event"
    event: CallEvent


class SessionStreamEnd(_SpekoModel):
    type: Literal["end"] = "end"
    reason: Literal["session_ended"] = "session_ended"


SessionStreamEvent = Union[
    SessionStreamStatus,
    SessionStreamTranscript,
    SessionStreamCallEvent,
    SessionStreamEnd,
]


# --- Phone numbers ------------------------------------------------------------

PhoneNumberDirection = Literal["inbound", "outbound", "both"]
PhoneNumberSource = Literal["managed", "sip_trunk"]
PhoneNumberSmsAssignmentStatus = Literal[
    "FAILED_ASSIGNMENT",
    "PENDING_ASSIGNMENT",
    "ASSIGNED",
    "PENDING_UNASSIGNMENT",
    "FAILED_UNASSIGNMENT",
]


class PhoneNumberSetupStatus(_SpekoModel):
    status: Literal["ready", "action_required", "suspended"]
    inbound_ready: bool
    outbound_ready: bool
    agent_ready: bool
    forwarding_required: bool
    # ``sip:<E.164>@<host>`` the carrier forwards inbound calls to when
    # ``forwarding_required``; ``None`` otherwise (or on servers predating it).
    forwarding_sip_uri: str | None = None
    sip_connection_ready: bool
    issues: list[str]


class PhoneNumberRow(_SpekoModel):
    id: str
    organization_id: str
    e164: str
    source: PhoneNumberSource
    # Platform-neutral resource id for a platform-managed number.
    provider_resource_id: Optional[str] = None
    # Deprecated: use provider_resource_id.
    telnyx_phone_number_id: Optional[str] = None
    # Deprecated: LiveKit trunk IDs are internal and no longer exposed.
    sip_trunk_id: Optional[str] = None
    sip_connection_installation_id: Optional[str] = None
    sip_provider_name: Optional[str] = None
    direction: PhoneNumberDirection
    dispatch_metadata_template: Optional[dict[str, Any]] = None
    label: Optional[str] = None
    sms10dlc_profile_id: Optional[str] = Field(default=None, alias="sms10dlcProfileId")
    sms_campaign_id: Optional[str] = None
    sms_assignment_status: Optional[PhoneNumberSmsAssignmentStatus] = None
    sms_assignment_updated_at: Optional[str] = None
    telnyx_messaging_profile_id: Optional[str] = None
    sms_messaging_profile_status: Literal["pending", "ready", "failed"] = "pending"
    sms_messaging_profile_updated_at: Optional[str] = None
    sms_messaging_profile_error: Optional[str] = None
    sms_automation_enabled: bool = False
    # 1:1 link to a persisted agent. When set, inbound calls hydrate
    # pipeline config from the agent row.
    agent_id: Optional[str] = None
    setup_status: PhoneNumberSetupStatus
    next_charge_at: str
    last_charged_at: Optional[str] = None
    suspended_at: Optional[str] = None
    billing_suspended_at: Optional[str] = None
    compliance_suspended_at: Optional[str] = None
    suspension_reason: Optional[Literal["billing", "compliance"]] = None
    created_at: str
    updated_at: str


class PhoneNumberCreateParams(_SpekoModel):
    e164: str
    direction: Optional[PhoneNumberDirection] = None
    # Dispatch metadata template (variables {{var}} resolved at dial).
    dispatch_metadata_template: Optional[dict[str, Any]] = None
    label: Optional[str] = None
    # 1:1 link to an agent in the same org.
    agent_id: Optional[str] = None


class PhoneNumberImportSipTrunkParams(_SpekoModel):
    """Import a number carried on your own SIP trunk. Supply either
    ``sip_connection_installation_id`` (preferred, productized SIP
    connections) or the legacy ``sip_trunk_id``."""

    e164: str
    sip_connection_installation_id: Optional[str] = None
    # Legacy LiveKit outbound trunk id. Ignored when
    # sip_connection_installation_id is present.
    sip_trunk_id: Optional[str] = None
    # Optional provider/account label for display.
    sip_provider_name: Optional[str] = None
    direction: Optional[PhoneNumberDirection] = None
    dispatch_metadata_template: Optional[dict[str, Any]] = None
    label: Optional[str] = None
    agent_id: Optional[str] = None


class PhoneNumberUpdateParams(_SpekoModel):
    direction: Optional[PhoneNumberDirection] = None
    dispatch_metadata_template: Optional[dict[str, Any]] = None
    label: Optional[str] = None
    # Pass None explicitly to unlink, a string to relink.
    agent_id: Optional[str] = None
    sms_automation_enabled: Optional[bool] = None


class AvailablePhoneNumberRegion(_SpekoModel):
    state: Optional[str] = None
    locality: Optional[str] = None
    rate_center: Optional[str] = None


class AvailablePhoneNumber(_SpekoModel):
    e164: str
    friendly_name: str
    monthly_cost_usd: float
    upfront_cost_usd: float
    features: list[str]
    region: AvailablePhoneNumberRegion


PhoneNumberKybStatus = Literal["missing", "draft", "submitted", "approved", "rejected", "revoked"]
PhoneNumberKybSubmissionStatus = Literal["draft", "submitted", "approved", "rejected", "revoked"]
PhoneNumberKybSlackNotificationStatus = Literal["not_queued", "queued", "enqueue_failed"]


class PhoneNumberKybAddress(_SpekoModel):
    street: str
    city: str
    state: str
    postal_code: str
    country: str


class PhoneNumberKybBusinessProfile(_SpekoModel):
    legal_name: str
    display_name: str
    entity_type: str
    country: str
    registration_id: Optional[str] = None
    website: str
    address: PhoneNumberKybAddress
    use_case: str
    expected_usage: str


class PhoneNumberKybAuthorizedRepresentative(_SpekoModel):
    name: str
    title: str
    email: str
    phone: Optional[str] = None


class PhoneNumberKybDeclaration(_SpekoModel):
    business_name: str
    use_case: str


class PhoneNumberKybUserAttestor(_SpekoModel):
    kind: Literal["user"]
    user_id: str
    name: str
    email: str
    organization_role: Optional[str] = None


class PhoneNumberKybApiKeyAttestor(_SpekoModel):
    kind: Literal["api_key"]
    api_key_id: str


PhoneNumberKybAttestor = Union[PhoneNumberKybUserAttestor, PhoneNumberKybApiKeyAttestor]


class PhoneNumberKybAttestationContract(_SpekoModel):
    version: str
    text: str
    terms_version: str
    terms_url: str


class PhoneNumberKybDraftParams(_SpekoModel):
    business_profile: PhoneNumberKybBusinessProfile
    authorized_representative: PhoneNumberKybAuthorizedRepresentative
    attestation_accepted: Optional[bool] = None


class PhoneNumberKybSubmitParams(_SpekoModel):
    business_profile: PhoneNumberKybBusinessProfile
    authorized_representative: PhoneNumberKybAuthorizedRepresentative
    attestation_accepted: Literal[True]
    attestation_version: Optional[str] = None


class PhoneNumberKybMinimalSubmitParams(_SpekoModel):
    declaration: PhoneNumberKybDeclaration
    attestation_accepted: Literal[True]
    attestation_version: str


class PhoneNumberKybSubmission(_SpekoModel):
    id: str
    organization_id: str
    status: PhoneNumberKybSubmissionStatus
    business_profile: Optional[PhoneNumberKybBusinessProfile] = None
    authorized_representative: Optional[PhoneNumberKybAuthorizedRepresentative] = None
    declaration: Optional[PhoneNumberKybDeclaration] = None
    attestor: Optional[PhoneNumberKybAttestor] = None
    attestation_accepted: bool
    attestation_version: Optional[str] = None
    attestation_text: Optional[str] = None
    terms_version: Optional[str] = None
    attested_at: Optional[str] = None
    access_hold_at: Optional[str] = None
    access_hold_reason: Optional[Literal["rejected", "revoked"]] = None
    submitted_by_user_id: Optional[str] = None
    submitted_by_email: Optional[str] = None
    submitted_by_api_key_id: Optional[str] = None
    submitted_at: Optional[str] = None
    reviewer_user_id: Optional[str] = None
    reviewer_email: Optional[str] = None
    reviewed_at: Optional[str] = None
    rejection_reason: Optional[str] = None
    slack_notification_status: PhoneNumberKybSlackNotificationStatus
    slack_notification_job_id: Optional[str] = None
    slack_notification_error: Optional[str] = None
    created_at: str
    updated_at: str


class PhoneNumberKybPrefill(_SpekoModel):
    business_profile: PhoneNumberKybBusinessProfile
    authorized_representative: PhoneNumberKybAuthorizedRepresentative


class PhoneNumberKybOverview(_SpekoModel):
    status: PhoneNumberKybStatus
    submission: Optional[PhoneNumberKybSubmission] = None
    prefill: Optional[PhoneNumberKybPrefill] = None
    declaration_prefill: Optional[PhoneNumberKybDeclaration] = None
    required_attestation: Optional[PhoneNumberKybAttestationContract] = None
    attestation_required: Optional[bool] = None
    compliance_access: Optional[Literal["enabled", "awaiting_attestation", "suspended"]] = None


# --- Agents ---------------------------------------------------------------------


class AgentIntent(_SpekoModel):
    """Routing intent for an agent's voice pipeline. Narrower than the
    top-level ``RoutingIntent`` — the agents API specifically accepts
    ``latency``, ``quality``, or ``cost`` (no ``balanced`` / ``accuracy``)."""

    language: str
    optimize_for: Optional[Literal["latency", "quality", "cost"]] = None


class AgentLlmOptions(_SpekoModel):
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    model: Optional[str] = None


class AgentAllowedProviders(_SpekoModel):
    stt: Optional[list[str]] = None
    llm: Optional[list[str]] = None
    tts: Optional[list[str]] = None
    s2s: Optional[list[str]] = None


class AgentStackPreferences(_SpekoModel):
    allowed_providers: Optional[AgentAllowedProviders] = None


class AgentSttOptions(_SpekoModel):
    # Vocabulary keywords forwarded to whichever STT provider the router picks.
    keywords: Optional[list[str]] = None
    # Free-text transcription context (domain, names, expected phrases), max
    # 2000 chars. Honored only by prompt-capable STT models (OpenAI
    # gpt-4o-transcribe family, AssemblyAI Universal-3 Pro tiers).
    prompt: Optional[str] = None
    # STT stream-language override ('en', 'es-MX', Deepgram's 'multi', or
    # 'auto'). Never affects stack routing — that keeps the agent language.
    language: Optional[str] = None


AgentAmbientClip = Literal[
    "office-ambience",
    "city-ambience",
    "forest-ambience",
    "crowded-room",
    "keyboard-typing",
    "keyboard-typing2",
]


def _exactly_one_source(model: Any, *, required: bool) -> Any:
    """`clip` xor `sound_id`. The server rejects both-or-neither with a 422; the
    SDK refuses the same shapes at construction so the contract is enforced
    where the mistake is made, not two network hops later."""
    has_clip = getattr(model, "clip", None) is not None
    has_sound = getattr(model, "sound_id", None) is not None
    if has_clip and has_sound:
        raise ValueError("Provide at most one of `clip` or `sound_id`, not both.")
    if required and not (has_clip or has_sound):
        raise ValueError("Provide exactly one of `clip` or `sound_id`.")
    return model


class AgentAmbientAudio(_SpekoModel):
    # A built-in clip, or `sound_id` naming an audio file the organization
    # uploaded via `/v1/sounds`. Exactly one — validated below, matching the
    # server, which rejects a body with both or neither.
    clip: Optional[AgentAmbientClip] = None
    sound_id: Optional[str] = None
    # Linear gain in [0, 16], defaulting to 1.0 — the clip's own recorded level,
    # which is not the same as "full volume". The built-ins are mastered roughly
    # 30 dB apart: office-ambience (~-52 LUFS) needs ~5-10 to be audible under
    # speech, city-ambience is about right at 1, crowded-room distorts past ~1.6.
    # An uploaded sound has no measured level — start at 1 and adjust by ear.
    volume: Optional[float] = None

    @model_validator(mode="after")
    def _check_source(self) -> AgentAmbientAudio:
        return _exactly_one_source(self, required=True)


class AgentToolCallSound(_SpekoModel):
    """Plays while a tool call is in flight and stops when it returns — the
    audible sibling of a tool's ``pre_tool_speech``. A webhook that takes
    seconds otherwise leaves dead air a caller hears as a dropped line.
    Individual tools override or silence it via their own ``tool_sound``."""

    clip: Optional[AgentAmbientClip] = None
    sound_id: Optional[str] = None
    volume: Optional[float] = None
    # Floor on audible time (default 600). Tool latency is bimodal: a cached
    # lookup returns in tens of milliseconds, and a burst of sound that short
    # reads as a glitch rather than a cue.
    min_duration_ms: Optional[int] = None
    # Silence held first (default 0), so a spoken lead-in lands before the
    # sound. A tool that finishes inside this window plays nothing at all.
    start_delay_ms: Optional[int] = None

    @model_validator(mode="after")
    def _check_source(self) -> AgentToolCallSound:
        return _exactly_one_source(self, required=True)


class AgentToolSoundOverride(_SpekoModel):
    """One tool's departure from the agent-wide tool-call sound. Omit the field
    to inherit it; ``enabled=False`` runs that tool silently; supplying a source
    swaps the sound for this tool only. At most one source: ``enabled=True``
    with neither means "on, with the agent's own sound"."""

    enabled: bool
    clip: Optional[AgentAmbientClip] = None
    sound_id: Optional[str] = None
    volume: Optional[float] = None

    @model_validator(mode="after")
    def _check_source(self) -> AgentToolSoundOverride:
        return _exactly_one_source(self, required=False)


class AgentBackgroundAudio(_SpekoModel):
    """Per-agent background audio: a continuous ambient bed, a sound played
    during tool calls, or both. Either plays on a separate media track mixed
    server-side, so it reaches both browser (WebRTC) and phone (SIP) callers."""

    ambient: Optional[AgentAmbientAudio] = None
    tool_sound: Optional[AgentToolCallSound] = None


class AgentAudioOutput(_SpekoModel):
    """Shaping of the agent's own speech, applied before the audio reaches the
    call (and so before a phone leg), whichever TTS provider spoke.

    ``gain_db`` is a static gain in dB, -24 to 12. Negative values make the
    agent quieter (-6 roughly halves the amplitude); boost saturates at full
    scale, so it clips a voice that already peaks near 0 dBFS. 0 is a
    passthrough. Background audio is not affected."""

    gain_db: float


class AgentSpeechNormalization(_SpekoModel):
    pronunciation_dictionary: Optional[dict[str, str]] = None
    text_replacements: Optional[dict[str, str]] = None


class AgentExtractionField(_SpekoModel):
    """A caller-defined post-call extraction field. The call-analysis pass
    fills each from the transcript per ``description``, typed by ``type``;
    values are delivered under the webhook payload's top-level ``custom_data``
    object keyed by ``name``. ``options`` is required for ``enum`` fields."""

    name: str
    type: Literal["string", "number", "boolean", "enum"]
    description: str
    options: Optional[list[str]] = None


class AgentLifecycleWebhookCreate(_SpekoModel):
    url: str
    # Optional per-webhook signing secret. When supplied, this endpoint signs
    # with its own secret instead of the shared org-level secret.
    secret: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    auth_headers: Optional[list[AgentWebhookAuthHeaderInput]] = None
    timeout_ms: Optional[int] = None
    response_mode: Optional[Literal["sync", "async"]] = None
    async_ack: Optional[str] = None
    # Post-call data-extraction fields. Applies to the postCall webhook only.
    extraction_fields: Optional[list[AgentExtractionField]] = None


AgentLifecycleWebhookUpdate = AgentLifecycleWebhookCreate


class AgentLifecycleWebhookSerialized(_SpekoModel):
    url: str
    secret_ref: str
    headers: Optional[dict[str, str]] = None
    auth_headers: Optional[list[AgentWebhookAuthHeader]] = None
    timeout_ms: Optional[int] = None
    response_mode: Optional[Literal["sync", "async"]] = None
    async_ack: Optional[str] = None
    extraction_fields: Optional[list[AgentExtractionField]] = None


class AgentWebhooksCreate(_SpekoModel):
    pre_call: Optional[AgentLifecycleWebhookCreate] = None
    post_call: Optional[AgentLifecycleWebhookCreate] = None
    status: Optional[AgentLifecycleWebhookCreate] = None
    # Dedicated call.analysis webhook — LLM analysis results only.
    analysis: Optional[AgentLifecycleWebhookCreate] = None
    # Dedicated call.recording webhook — fires when the recording turns
    # terminal.
    recording: Optional[AgentLifecycleWebhookCreate] = None


AgentWebhooksUpdate = AgentWebhooksCreate


class AgentWebhooksSerialized(_SpekoModel):
    pre_call: Optional[AgentLifecycleWebhookSerialized] = None
    post_call: Optional[AgentLifecycleWebhookSerialized] = None
    status: Optional[AgentLifecycleWebhookSerialized] = None
    analysis: Optional[AgentLifecycleWebhookSerialized] = None
    recording: Optional[AgentLifecycleWebhookSerialized] = None


class AgentPromptVariable(_SpekoModel):
    """One prompt-variable registry entry. ``default_value`` fills the
    variable when a session/dial call omits it (empty string = declared
    optional). Without a default the variable is required per call. Names may
    not use the reserved ``system.`` namespace."""

    name: str
    default_value: Optional[str] = None
    description: Optional[str] = None


class AgentRow(_SpekoModel):
    id: str
    organization_id: str
    name: str
    system_prompt: str
    voice: Optional[str] = None
    intent: AgentIntent
    llm_options: Optional[AgentLlmOptions] = None
    stack_preferences: Optional[AgentStackPreferences] = None
    stt_options: Optional[AgentSttOptions] = None
    background_audio: Optional[AgentBackgroundAudio] = None
    audio_output: Optional[AgentAudioOutput] = None
    speech_normalization: Optional[AgentSpeechNormalization] = None
    # Deprecated: use organization-owned speko.webhooks endpoints.
    webhooks: Optional[AgentWebhooksSerialized] = None
    # Post-call extraction schema on the agent itself - no webhook required.
    # Merged with webhooks.postCall.extraction_fields; the agent-level
    # definition wins on a name collision.
    extraction_fields: list[AgentExtractionField] = Field(default_factory=list)
    # Prompt-variable registry. Returned on single-agent reads; None = empty.
    prompt_variables: Optional[list[AgentPromptVariable]] = None
    created_at: str
    updated_at: str


class AgentCreateParams(_SpekoModel):
    name: str
    system_prompt: str
    voice: Optional[str] = None
    intent: AgentIntent
    llm_options: Optional[AgentLlmOptions] = None
    stack_preferences: Optional[AgentStackPreferences] = None
    stt_options: Optional[AgentSttOptions] = None
    background_audio: Optional[AgentBackgroundAudio] = None
    audio_output: Optional[AgentAudioOutput] = None
    speech_normalization: Optional[AgentSpeechNormalization] = None
    # Deprecated: use speko.webhooks.create() after creating the agent.
    webhooks: Optional[AgentWebhooksCreate] = None
    # Post-call extraction schema on the agent itself - no webhook required.
    # Merged with webhooks.postCall.extraction_fields; the agent-level
    # definition wins on a name collision.
    extraction_fields: Optional[list[AgentExtractionField]] = None
    # Declare the prompt's {{variables}} with per-agent defaults/descriptions.
    prompt_variables: Optional[list[AgentPromptVariable]] = None


class AgentUpdateParams(_SpekoModel):
    name: Optional[str] = None
    system_prompt: Optional[str] = None
    voice: Optional[str] = None
    intent: Optional[AgentIntent] = None
    llm_options: Optional[AgentLlmOptions] = None
    stack_preferences: Optional[AgentStackPreferences] = None
    stt_options: Optional[AgentSttOptions] = None
    background_audio: Optional[AgentBackgroundAudio] = None
    audio_output: Optional[AgentAudioOutput] = None
    speech_normalization: Optional[AgentSpeechNormalization] = None
    webhooks: Optional[AgentWebhooksUpdate] = None
    # Post-call extraction schema on the agent itself - no webhook required.
    # Merged with webhooks.postCall.extraction_fields; the agent-level
    # definition wins on a name collision.
    # None leaves the schema untouched; [] clears it.
    extraction_fields: Optional[list[AgentExtractionField]] = None
    prompt_variables: Optional[list[AgentPromptVariable]] = None


# --- Agent tools ---------------------------------------------------------------


class AgentToolSourceWebhookCreate(_SpekoModel):
    """Webhook source as sent to ``agents.tools.create``. The plaintext
    ``secret`` is encrypted server-side; the returned row carries
    ``secret_ref`` instead."""

    kind: Literal["webhook"] = "webhook"
    url: str
    # Plaintext shared secret. Encrypted server-side at write time.
    secret: str
    headers: Optional[dict[str, str]] = None
    auth_headers: Optional[list[AgentWebhookAuthHeaderInput]] = None
    timeout_ms: Optional[int] = None


class AgentToolSourceWebhookUpdate(_SpekoModel):
    """Webhook source as sent to ``agents.tools.update``. Unlike the create
    shape, ``secret`` is optional: omit it to keep the existing encrypted
    secret untouched, or supply a new one to rotate it."""

    kind: Literal["webhook"] = "webhook"
    url: str
    secret: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    auth_headers: Optional[list[AgentWebhookAuthHeaderInput]] = None
    timeout_ms: Optional[int] = None


class AgentToolSourceWebhookSerialized(_SpekoModel):
    """Webhook source as returned by the API. The plaintext secret never
    leaves the server — only the ``secret_ref`` pointer is exposed."""

    kind: Literal["webhook"] = "webhook"
    url: str
    secret_ref: str
    headers: Optional[dict[str, str]] = None
    auth_headers: Optional[list[AgentWebhookAuthHeader]] = None
    timeout_ms: Optional[int] = None


AgentToolSourceCreate = Union[
    ChatToolSourceInline,
    AgentToolSourceWebhookCreate,
    ChatToolSourceBuiltin,
    ChatToolSourceIntegration,
]

AgentToolSourceUpdate = Union[
    ChatToolSourceInline,
    AgentToolSourceWebhookUpdate,
    ChatToolSourceBuiltin,
    ChatToolSourceIntegration,
]

AgentToolSourceSerialized = Union[
    ChatToolSourceInline,
    AgentToolSourceWebhookSerialized,
    ChatToolSourceBuiltin,
    ChatToolSourceIntegration,
]


class AgentToolRow(_SpekoModel):
    id: str
    agent_id: str
    name: str
    description: str
    parameters: dict[str, Any]
    source: AgentToolSourceSerialized = Field(discriminator="kind")
    # Spoken lead-in behavior before this tool executes.
    pre_tool_speech: ChatToolPreToolSpeech = "auto"
    # Per-tool override of the agent's tool-call sound; None = inherit it.
    tool_sound: Optional[AgentToolSoundOverride] = None
    created_at: str
    updated_at: str


class AgentToolCreateParams(_SpekoModel):
    name: str
    description: str
    parameters: dict[str, Any]
    source: AgentToolSourceCreate = Field(discriminator="kind")
    # Spoken lead-in behavior before the tool executes. Defaults to `auto`.
    pre_tool_speech: Optional[ChatToolPreToolSpeech] = None
    tool_sound: Optional[AgentToolSoundOverride] = None


class AgentToolUpdateParams(_SpekoModel):
    description: Optional[str] = None
    parameters: Optional[dict[str, Any]] = None
    source: Optional[AgentToolSourceUpdate] = Field(default=None, discriminator="kind")
    pre_tool_speech: Optional[ChatToolPreToolSpeech] = None
    # Explicit None is meaningful on update: it clears the override and returns
    # the tool to the agent-wide sound.
    tool_sound: Optional[AgentToolSoundOverride] = None


class AgentCallListEntry(_SpekoSnakeModel):
    id: str
    call_id: str
    resource_uri: str
    agent_id: str
    status: str
    kind: str
    room_name: Optional[str] = None
    language: str
    created_at: str
    ended_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    recording_status: Optional[str] = None


class AgentCallListPage(_SpekoSnakeModel):
    calls: list[AgentCallListEntry]
    # Mirror of `calls` kept for wire parity with the REST response.
    entries: list[AgentCallListEntry] = Field(default_factory=list)
    next_cursor: Optional[str] = None


# --- Calls ----------------------------------------------------------------------


class CallTranscriptEntry(_SpekoSnakeModel):
    id: str
    index: int
    source: Literal["user", "agent", "system"]
    text: str
    started_at: str
    ended_at: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    eou_ms: Optional[float] = None
    llm_ttft_ms: Optional[float] = None
    tts_ttfb_ms: Optional[float] = None
    latency_status: Optional[Literal["partial", "complete", "interrupted", "error"]] = None
    conversational_latency_ms: Optional[float] = None


class CallTranscript(_SpekoSnakeModel):
    entries: list[CallTranscriptEntry]


class CallCostLine(_SpekoModel):
    provider: str
    metric: str
    quantity: float
    key_source: KeySource
    cost_micro_usd: str


class CallReportWebhookDelivery(_SpekoModel):
    endpoint_id: str
    delivery_id: str
    event_id: str
    delivered: bool
    status: Optional[int] = None
    error: Optional[str] = None
    created_at: str


ScheduledCallbackStatus = Literal["scheduled", "dispatching", "dispatched", "cancelled", "failed"]


class ScheduledCallback(_SpekoSnakeModel):
    id: str
    organization_id: str
    source_session_id: Optional[str] = None
    created_session_id: Optional[str] = None
    agent_id: Optional[str] = None
    phone_number_id: Optional[str] = None
    to_number: str
    from_number: Optional[str] = None
    scheduled_at: str
    status: ScheduledCallbackStatus
    reason: Optional[str] = None
    instructions: Optional[str] = None
    summary: Optional[str] = None
    pipeline_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    failure_cause: Optional[str] = None
    attempted_at: Optional[str] = None
    dispatched_at: Optional[str] = None
    cancelled_at: Optional[str] = None
    created_at: str
    updated_at: str


class CallReport(_SpekoSnakeModel):
    session_id: str
    organization_id: str
    summary: str
    outcome: str
    structured_data: dict[str, Any] = Field(default_factory=dict)
    custom_data: dict[str, Any] = Field(default_factory=dict)
    transcript: CallTranscript
    cost_micro_usd: str
    cost_breakdown: list[CallCostLine] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    scheduled_callback: Optional[dict[str, Any]] = None
    analysis_status: Literal["heuristic", "completed", "failed"]
    analysis_provider: Optional[str] = None
    analysis_model: Optional[str] = None
    analysis_error: Optional[str] = None
    analysis_completed_at: Optional[str] = None
    # Canonical per-endpoint results; the singular post_call_webhook_* wire
    # fields are deprecated aggregates and intentionally not modeled.
    webhook_deliveries: list[CallReportWebhookDelivery] = Field(default_factory=list)
    created_at: str
    updated_at: str


class FinalizeCallReportParams(_SpekoModel):
    force_analysis: Optional[bool] = None
    retry_webhook: Optional[bool] = None


class FinalizeCallReportResult(_SpekoSnakeModel):
    session_id: str
    summary: str
    outcome: str
    cost_micro_usd: str
    webhook_deliveries: list[CallReportWebhookDelivery] = Field(default_factory=list)


class CallRecording(_SpekoModel):
    url: str


class WebJoinParams(_SpekoModel):
    # Display name other participants (and transcripts) see for the joiner.
    display_name: Optional[str] = None


class WebJoinResult(_SpekoModel):
    # LiveKit access token for the live call's room. Mint at click time —
    # short TTL.
    token: str
    # Public LiveKit URL the browser connects to.
    url: str
    # Participant identity minted for this join (unique per join).
    identity: str
    room_name: str
    # ISO timestamp the token stops being accepted for NEW connections.
    expires_at: str


class EndCallResult(_SpekoSnakeModel):
    ok: Literal[True]
    # 'ending' when teardown was requested; 'already_ended' when the call
    # was over.
    status: Literal["ending", "already_ended"]
    # ISO timestamp, present only with 'already_ended'.
    ended_at: Optional[str] = None


class CallEvent(_SpekoSnakeModel):
    id: str
    session_id: Optional[str] = None
    organization_id: str
    provider: str
    event_type: str
    status: Optional[str] = None
    failure_cause: Optional[str] = None
    sip_status_code: Optional[int] = None
    sip_status: Optional[str] = None
    occurred_at: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class CallTransfer(_SpekoSnakeModel):
    id: str
    session_id: str
    organization_id: str
    kind: Literal["blind", "warm"]
    status: Literal["requested", "screening", "bridging", "completed", "failed", "cancelled"]
    transfer_to: str
    from_room_name: Optional[str] = None
    consultation_room_name: Optional[str] = None
    caller_participant_identity: Optional[str] = None
    recipient_participant_identity: Optional[str] = None
    outbound_trunk_id: Optional[str] = None
    screening_prompt: Optional[str] = None
    summary: Optional[str] = None
    failure_cause: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    completed_at: Optional[str] = None


class WarmTransferFallbackResult(_SpekoSnakeModel):
    action: Literal["return_to_assistant", "take_message", "end_call"]
    message: str
    take_message_prompt: Optional[str] = None
    hold_audio_url: Optional[str] = None
    voicemail_detected: bool = False


class CallTransferResponse(CallTransfer):
    routing_attempts: Optional[list[Optional[CallTransfer]]] = None
    next_transfer: Optional[CallTransfer] = None
    fallback: Optional[WarmTransferFallbackResult] = None


class CallDetail(_SpekoSnakeModel):
    id: str
    call_id: str
    resource_uri: str
    agent_id: Optional[str] = None
    status: str
    kind: str
    room_name: Optional[str] = None
    language: str
    pipeline_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    ended_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    recording_status: Optional[str] = None
    recording_duration_ms: Optional[float] = None
    recording_resource_uri: str
    report: Optional[CallReport] = None
    transfers: list[CallTransfer] = Field(default_factory=list)
    transcript: CallTranscript
    span_tree: dict[str, Any] = Field(default_factory=dict)


class BlindTransferParams(_SpekoModel):
    to: str
    participant_identity: Optional[str] = None
    play_dialtone: Optional[bool] = None
    ringing_timeout: Optional[float] = None
    headers: Optional[dict[str, str]] = None


class WarmTransferDestination(_SpekoModel):
    to: str
    label: Optional[str] = None
    outbound_trunk_id: Optional[str] = None
    screening_prompt: Optional[str] = None
    summary: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class WarmTransferFallback(_SpekoModel):
    strategy: Optional[Literal["return_to_assistant", "take_message", "end_call"]] = None
    message: Optional[str] = None
    take_message_prompt: Optional[str] = None
    hold_audio_url: Optional[str] = None


class WarmTransferVoicemailDetection(_SpekoModel):
    mode: Optional[Literal["agent", "amd", "disabled"]] = None
    enabled: Optional[bool] = None
    timeout_seconds: Optional[float] = None


class WarmTransferParams(_SpekoModel):
    to: Optional[str] = None
    destinations: Optional[list[WarmTransferDestination]] = None
    from_: Optional[str] = Field(default=None, alias="from")
    participant_identity: Optional[str] = None
    outbound_trunk_id: Optional[str] = None
    screening_prompt: Optional[str] = None
    summary: Optional[str] = None
    ringing_timeout: Optional[float] = None
    wait_until_answered: Optional[bool] = None
    fallback: Optional[WarmTransferFallback] = None
    voicemail_detection: Optional[WarmTransferVoicemailDetection] = None
    metadata: Optional[dict[str, Any]] = None


class CompleteWarmTransferParams(_SpekoModel):
    recipient_participant_identity: Optional[str] = None
    summary: Optional[str] = None


class CancelWarmTransferParams(_SpekoModel):
    reason: Optional[str] = None
    summary: Optional[str] = None
    try_next: Optional[bool] = None
    voicemail_detected: Optional[bool] = None


class CancelScheduledCallbackParams(_SpekoModel):
    reason: Optional[str] = None


# --- Workspace webhooks ---------------------------------------------------------

# Two families of event, both subscribable on a workspace endpoint.
#
# The `call.pre_call` / `call.status` / `call.report` / `call.analysis` /
# `call.recording` five describe one AI *voice session* as it progresses. The
# rest are programmable-voice control events describing a human call leg by leg;
# their payload carries `call_id`, `control_id` (null for call-scoped events),
# `event_id` and `occurred_at` alongside the event's own fields.
#
# Only `call.report`, `call.analysis` and `call.recording` are retried on
# delivery failure. Control events are one-shot: they are a live projection of
# the `call_event` history, which stays readable over the API, so a redelivery
# would arrive too late to be worth anything.
WorkspaceWebhookEventType = Literal[
    "call.pre_call",
    "call.status",
    "call.report",
    "call.analysis",
    "call.recording",
    "call.initiated",
    "call.ringing",
    "call.answered",
    "call.bridged",
    "call.hold",
    "call.unhold",
    "call.mute",
    "call.unmute",
    "call.dtmf.sent",
    "call.transfer.initiated",
    "call.transfer.completed",
    "call.transfer.failed",
    "call.leg.hangup",
    "call.hangup",
    "sms.received",
    "sms.accepted",
    "sms.sent",
    "sms.delivered",
    "sms.delivery_failed",
    "sms.submission_unknown",
    "sms.opted_out",
    "sms.opted_in",
]

WebhookEventType = Literal[
    "call.pre_call",
    "call.status",
    "call.report",
    "call.analysis",
    "call.recording",
    "call.initiated",
    "call.ringing",
    "call.answered",
    "call.bridged",
    "call.hold",
    "call.unhold",
    "call.mute",
    "call.unmute",
    "call.dtmf.sent",
    "call.transfer.initiated",
    "call.transfer.completed",
    "call.transfer.failed",
    "call.leg.hangup",
    "call.hangup",
    "imessage.received",
    "imessage.reaction_received",
    "imessage.sent",
    "imessage.delivered",
    "imessage.delivery_failed",
    "sms.received",
    "sms.accepted",
    "sms.sent",
    "sms.delivered",
    "sms.delivery_failed",
    "sms.submission_unknown",
    "sms.opted_out",
    "sms.opted_in",
]

WebhookDeliveryStatus = Literal[
    "pending", "delivering", "succeeded", "failed", "cancelled", "expired"
]


class WebhookEndpointAuthHeaderInput(_SpekoModel):
    name: str
    # Write-only plaintext. The server encrypts it and never returns it.
    value: str


class WebhookEndpointAuthHeaderUpdate(_SpekoModel):
    name: str
    # Supply to set or rotate; omit to retain the stored value for this
    # header name.
    value: Optional[str] = None


class WebhookEndpointInput(_SpekoModel):
    name: str
    url: str
    events: list[WorkspaceWebhookEventType]
    # Defaults to True. When False, agent_ids must contain at least one agent.
    all_agents: Optional[bool] = None
    agent_ids: Optional[list[str]] = None
    filter_tags: Optional[dict[str, str]] = None
    headers: Optional[dict[str, str]] = None
    auth_headers: Optional[list[WebhookEndpointAuthHeaderInput]] = None
    timeout_ms: Optional[int] = None
    signing_secret_source: Optional[Literal["workspace", "custom"]] = None
    # Write-only. Required when signing_secret_source is custom.
    signing_secret: Optional[str] = None
    extraction_fields: Optional[list[AgentExtractionField]] = None
    content_mode: Optional[Literal["full", "metadata_only"]] = None


class WebhookEndpointUpdate(_SpekoModel):
    name: Optional[str] = None
    url: Optional[str] = None
    events: Optional[list[WorkspaceWebhookEventType]] = None
    all_agents: Optional[bool] = None
    agent_ids: Optional[list[str]] = None
    filter_tags: Optional[dict[str, str]] = None
    headers: Optional[dict[str, str]] = None
    auth_headers: Optional[list[WebhookEndpointAuthHeaderUpdate]] = None
    timeout_ms: Optional[int] = None
    signing_secret_source: Optional[Literal["workspace", "custom"]] = None
    signing_secret: Optional[str] = None
    extraction_fields: Optional[list[AgentExtractionField]] = None
    content_mode: Optional[Literal["full", "metadata_only"]] = None


class WebhookEndpointAuthHeaderStatus(_SpekoModel):
    name: str
    configured: Literal[True]


class WebhookEndpoint(_SpekoModel):
    id: str
    name: str
    url: str
    events: list[WorkspaceWebhookEventType]
    all_agents: bool
    agent_ids: list[str] = Field(default_factory=list)
    filter_tags: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    auth_headers: list[WebhookEndpointAuthHeaderStatus] = Field(default_factory=list)
    timeout_ms: int
    signing_secret_source: Literal["workspace", "custom"]
    has_custom_signing_secret: bool
    extraction_fields: list[AgentExtractionField] = Field(default_factory=list)
    content_mode: Literal["full", "metadata_only"] = "full"
    legacy_managed: bool = False
    created_at: str
    updated_at: str


class WebhookDelivery(_SpekoModel):
    id: str
    event_id: str
    endpoint_id: str
    endpoint_name: str
    endpoint_kind: Literal["workspace", "imessage"]
    endpoint_deleted: bool = False
    event: WebhookEventType
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    webhook_tags: dict[str, str] = Field(default_factory=dict)
    status: WebhookDeliveryStatus
    attempts: int
    http_status: Optional[int] = None
    error: Optional[str] = None
    occurred_at: str
    expires_at: str
    delivered_at: Optional[str] = None
    created_at: str
    # False for provider-retried iMessage subscriber deliveries.
    can_redeliver: bool = True


class WebhookDeliveryEndpointOption(_SpekoModel):
    id: str
    name: str
    kind: Literal["workspace", "imessage"]
    deleted: bool = False


class WebhookDeliveryPage(_SpekoModel):
    data: list[WebhookDelivery]
    next_cursor: Optional[str] = None
    endpoint_options: list[WebhookDeliveryEndpointOption] = Field(default_factory=list)


class WebhookDeliveryAttempt(_SpekoModel):
    id: str
    attempt_number: int
    trigger: Literal["automatic", "manual"]
    request_url: str
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body: dict[str, Any] = Field(default_factory=dict)
    response_status: Optional[int] = None
    response_body: Optional[str] = None
    response_truncated: bool = False
    duration_ms: float
    error: Optional[str] = None
    created_at: str


class WebhookDeliveryDetail(_SpekoModel):
    id: str
    event_id: str
    endpoint_id: str
    endpoint_name: str
    endpoint_kind: Literal["workspace", "imessage"]
    endpoint_deleted: bool = False
    event: WebhookEventType
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    webhook_tags: dict[str, str] = Field(default_factory=dict)
    status: WebhookDeliveryStatus
    attempt_count: int
    http_status: Optional[int] = None
    error: Optional[str] = None
    occurred_at: str
    expires_at: str
    delivered_at: Optional[str] = None
    created_at: str
    can_redeliver: bool = True
    request_payload: dict[str, Any] = Field(default_factory=dict)
    attempts: list[WebhookDeliveryAttempt] = Field(default_factory=list)


class WebhookRedeliverResult(_SpekoModel):
    delivered: bool
    http_status: Optional[int] = None
    error: Optional[str] = None


# --- Knowledge bases ------------------------------------------------------------


class KnowledgeBaseRow(_SpekoModel):
    id: str
    organization_id: str
    agent_id: str
    name: str
    description: Optional[str] = None
    embedding_model: str
    document_count: int
    chunk_count: int
    created_at: str
    updated_at: str


class KnowledgeBaseCreateParams(_SpekoModel):
    agent_id: str
    name: str
    description: Optional[str] = None


KnowledgeBaseDocumentStatus = Literal["pending", "processing", "ready", "failed"]


class KnowledgeBaseDocumentRow(_SpekoModel):
    id: str
    knowledge_base_id: str
    filename: str
    content_type: str
    size_bytes: int
    status: KnowledgeBaseDocumentStatus
    error_message: Optional[str] = None
    chunk_count: int = 0
    metadata: Optional[dict[str, Any]] = None
    created_at: str
    updated_at: str
    ingested_at: Optional[str] = None


class KnowledgeBaseDocumentCreateParams(_SpekoModel):
    filename: str
    # MIME type. Currently the ingest pipeline accepts text/plain and
    # text/markdown (plus text/x-markdown, application/x-markdown).
    content_type: str
    size_bytes: int
    metadata: Optional[dict[str, Any]] = None


class KnowledgeBaseDocumentUploadSpec(_SpekoModel):
    # Signed GCS URL valid for expires_in_seconds from issuance.
    url: str
    method: Literal["PUT"]
    # Headers that MUST be sent on the PUT (Content-Type, length-range, etc.).
    headers: dict[str, str]
    expires_in_seconds: int


class KnowledgeBaseDocumentCreateResult(_SpekoModel):
    document: KnowledgeBaseDocumentRow
    upload: KnowledgeBaseDocumentUploadSpec


class KnowledgeBaseDocumentUploadParams(_SpekoModel):
    """Convenience parameter shape for ``knowledge_bases.upload_document``.
    The wrapper computes ``size_bytes`` from ``data`` automatically."""

    filename: str
    content_type: str
    data: bytes
    metadata: Optional[dict[str, Any]] = None


# --- SMS messaging -------------------------------------------------------------

SmsMessageStatus = Literal[
    "queued",
    "scheduled",
    "submitting",
    "accepted",
    "sent",
    "delivered",
    "delivery_failed",
    "rejected",
    "submission_unknown",
    "canceled",
    "received",
]
SmsMessageDirection = Literal["inbound", "outbound"]
SmsMessageOrigin = Literal["api", "dashboard", "agent_tool", "agent_auto_reply", "telnyx"]
SmsConsentSource = Literal["inbound", "api", "keyword", "webform", "paper", "verbal", "import"]


class SmsSegmentEstimate(_SpekoSnakeModel):
    encoding: Literal["gsm7", "ucs2"]
    segments: int
    units: int
    per_segment: int


class SmsMessage(_SpekoSnakeModel):
    id: str
    conversation_id: str
    batch_id: Optional[str] = None
    from_phone_number_id: str
    direction: SmsMessageDirection
    origin: SmsMessageOrigin
    from_: str = Field(alias="from")
    to: str
    text: Optional[str] = None
    campaign_id: Optional[str] = None
    brand_id: Optional[str] = None
    campaign_snapshot: Optional[dict[str, Any]] = None
    consent_id: Optional[str] = None
    consent_basis: Optional[str] = None
    recipient_timezone: Optional[str] = None
    requested_send_at: Optional[str] = None
    effective_send_at: Optional[str] = None
    terminal_at: Optional[str] = None
    status: SmsMessageStatus
    provider_status: Optional[str] = None
    encoding: Optional[Literal["gsm7", "ucs2"]] = None
    estimated_segments: int
    segment_count: int
    estimated: SmsSegmentEstimate
    charged_micro_usd: str
    provider_cost_micro_usd: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: Optional[dict[str, Any]] = None
    created_at: str
    updated_at: str


class SmsSendParams(_SpekoSnakeModel):
    from_phone_number_id: str
    to: str
    text: str
    send_at: Optional[str] = None
    consent_id: Optional[str] = None
    recipient_timezone: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class SmsConversationSendParams(_SpekoSnakeModel):
    text: str
    send_at: Optional[str] = None
    consent_id: Optional[str] = None
    recipient_timezone: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class SmsBatchRecipient(_SpekoSnakeModel):
    to: str
    text: str
    consent_id: Optional[str] = None
    recipient_timezone: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class SmsBatchCreateParams(_SpekoSnakeModel):
    from_phone_number_id: str
    recipients: list[SmsBatchRecipient]
    send_at: Optional[str] = None


class SmsBatch(_SpekoSnakeModel):
    id: str
    from_phone_number_id: str
    status: Literal[
        "queued",
        "scheduled",
        "processing",
        "completed",
        "partially_failed",
        "failed",
        "canceled",
    ]
    requested_send_at: Optional[str] = None
    total_count: int
    accepted_count: int
    rejected_count: int
    delivered_count: int
    failed_count: int
    canceled_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: str
    updated_at: str


class SmsConversation(_SpekoSnakeModel):
    id: str
    phone_number_id: str
    remote_phone_number: str
    campaign_id: Optional[str] = None
    campaign_snapshot: Optional[dict[str, Any]] = None
    status: Literal["open", "closed", "spam"]
    automation_status: Literal["disabled", "enabled", "paused"]
    assigned_user_id: Optional[str] = None
    assigned_agent_id: Optional[str] = None
    unread_count: int
    recipient_timezone: Optional[str] = None
    last_inbound_at: Optional[str] = None
    last_outbound_at: Optional[str] = None
    last_message_at: str
    content_redacted_at: Optional[str] = None
    created_at: str
    updated_at: str


class SmsConversationUpdate(_SpekoSnakeModel):
    status: Optional[Literal["open", "closed", "spam"]] = None
    assigned_user_id: Optional[str] = None
    assigned_agent_id: Optional[str] = None
    automation_status: Optional[Literal["disabled", "enabled", "paused"]] = None
    recipient_timezone: Optional[str] = None


class SmsConversationNote(_SpekoSnakeModel):
    id: str
    conversation_id: str
    body: Optional[str] = None
    created_by_user_id: str
    redacted_at: Optional[str] = None
    created_at: str


class SmsConsentInput(_SpekoSnakeModel):
    recipient: str
    campaign_id: str
    source: SmsConsentSource
    proof_reference: Optional[str] = None
    proof: Optional[str] = None
    timezone: Optional[str] = None
    captured_at: Optional[str] = None
    expires_at: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class SmsConsent(_SpekoSnakeModel):
    id: str
    recipient: str
    campaign_id: str
    status: Literal["active", "revoked", "expired"]
    source: SmsConsentSource
    proof_reference: Optional[str] = None
    proof_hash: Optional[str] = None
    timezone: Optional[str] = None
    captured_at: str
    expires_at: Optional[str] = None
    revoked_at: Optional[str] = None
    revoked_reason: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class SmsSuppression(_SpekoSnakeModel):
    id: str
    recipient: str
    status: Literal["suppressed", "lifted"]
    keyword: Optional[str] = None
    source_phone_number_id: Optional[str] = None
    source_message_provider_id: Optional[str] = None
    suppressed_at: str
    lifted_at: Optional[str] = None
    updated_at: str


class SmsSettings(_SpekoSnakeModel):
    messaging_profile_id: Optional[str] = None
    messaging_profile_status: str
    webhook_config_version: int
    opt_out_config_version: int
    help_message: str
    opt_out_message: str
    opt_in_message: str
    retention_days: int
    quiet_hours_start: str
    quiet_hours_end: str
    default_timezone: Optional[str] = None
    default_automation_enabled: bool
    last_synced_at: Optional[str] = None
    last_error: Optional[str] = None
    updated_at: str


class SmsSettingsUpdate(_SpekoSnakeModel):
    help_message: Optional[str] = None
    opt_out_message: Optional[str] = None
    opt_in_message: Optional[str] = None
    retention_days: Optional[int] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    default_timezone: Optional[str] = None
    default_automation_enabled: Optional[bool] = None


class SmsStreamEvent(_SpekoSnakeModel):
    event: str
    id: Optional[str] = None
    message_id: str
    conversation_id: str
    status: SmsMessageStatus
    occurred_at: str
