"""Public value objects, events, and control types.

Types are vendor-neutral. Nested collections are copied at construction so callers
cannot share a writable reference into a frozen object.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Literal

from agent_runtime.jsonutil import JsonValue, freeze_json, freeze_mapping

# --- enumerations ---


class Flow(Enum):
    CONTINUE = "continue"
    FINISH = "finish"
    WAIT = "wait"


class Role(Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class StopReason(Enum):
    USER_STOP = "user_stop"
    HOST_CANCEL = "host_cancel"
    CONSUMER_CLOSED = "consumer_closed"
    DEADLINE = "deadline"


class ToolChoiceMode(Enum):
    AUTO = "auto"
    NONE = "none"
    REQUIRED = "required"
    NAMED = "named"


class UsageSource(Enum):
    PROVIDER = "provider"
    ESTIMATED = "estimated"
    MISSING = "missing"


class MatchKind(Enum):
    MATCHED = "matched"
    NO_MATCH = "no_match"
    DEGRADED = "degraded"
    REJECTED = "rejected"
    CONFIG_ERROR = "config_error"


class LogicalOpKind(Enum):
    INPUT = "input"
    TEXT_DELTA = "text_delta"
    TEXT_FINAL = "text_final"
    TOOL_CALLS = "tool_calls"
    TOOL_RESULTS = "tool_results"
    RUN_FINAL = "run_final"


class RunStatus(Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING = "waiting"


class PartialOutputPolicy(Enum):
    """Failed attempts keep their own output identity; they are not concatenated."""

    ISOLATE_ATTEMPTS = "isolate_attempts"


class OrdinaryToolErrorPolicy(Enum):
    """Business failures become model-visible results. Protocol/recording failures abort."""

    MODEL_VISIBLE = "model_visible"


# Default batch control aggregation after every required side effect has run.
FLOW_PRIORITY: tuple[Flow, ...] = (Flow.WAIT, Flow.FINISH, Flow.CONTINUE)
DEFAULT_PARTIAL_OUTPUT_POLICY = PartialOutputPolicy.ISOLATE_ATTEMPTS
DEFAULT_ORDINARY_TOOL_ERROR_POLICY = OrdinaryToolErrorPolicy.MODEL_VISIBLE


# --- identity, budget, targets ---


@dataclass(frozen=True)
class ActorIdentity:
    actor_id: str
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RecordTarget:
    value: str


@dataclass(frozen=True)
class StepIdentity:
    run_id: str
    step_no: int
    model_round: int


@dataclass(frozen=True)
class RecoveryBudget:
    max_attempts: int = 3
    remaining_attempts: int = 3
    backoff_seconds: tuple[float, ...] = (0.0, 0.25, 0.5)
    retry_partial_output: bool = False

    def consume(self) -> RecoveryBudget:
        remaining = max(0, self.remaining_attempts - 1)
        return RecoveryBudget(
            max_attempts=self.max_attempts,
            remaining_attempts=remaining,
            backoff_seconds=self.backoff_seconds,
            retry_partial_output=self.retry_partial_output,
        )


@dataclass(frozen=True)
class ConsumedBudget:
    model_rounds: int = 0
    steps: int = 0
    estimated_tokens: int = 0


@dataclass(frozen=True)
class ExecutionLimits:
    max_model_rounds: int | None = 16
    max_steps: int | None = 32
    max_estimated_tokens: int | None = None
    recovery: RecoveryBudget = field(default_factory=RecoveryBudget)


@dataclass(frozen=True)
class BindingSetRef:
    """Version handle for this step's execution bindings. Never carries credentials."""

    version: str


@dataclass(frozen=True)
class CapabilityPolicyRef:
    version: str


@dataclass(frozen=True)
class ContextVersion:
    history_version: str
    capability_version: str
    application_version: str
    target: RecordTarget


# --- tools, messages, model io ---


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, JsonValue] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", freeze_mapping(self.parameters))


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, JsonValue] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", freeze_mapping(self.arguments))


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    output: str
    is_error: bool = False


@dataclass(frozen=True)
class InvocationOutcome:
    """Invoker result plus optional side-effect hints consumed only by ResultPolicy."""

    result: ToolResult
    flow_hint: FlowDecision | None = None
    next_record_target: RecordTarget | None = None
    activate_tools: tuple[ToolSpec, ...] = ()
    contributions: tuple[ContextContribution, ...] = ()
    state_payload: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "activate_tools", tuple(self.activate_tools))
        object.__setattr__(self, "contributions", tuple(self.contributions))
        if self.state_payload is not None:
            object.__setattr__(self, "state_payload", freeze_mapping(self.state_payload))


@dataclass(frozen=True)
class Message:
    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    reasoning: str | None = None
    message_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))


@dataclass(frozen=True)
class ToolChoice:
    mode: ToolChoiceMode = ToolChoiceMode.AUTO
    name: str | None = None


@dataclass(frozen=True)
class ModelConfig:
    model: str = "synthetic"
    temperature: float | None = None
    extra: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    source: UsageSource = UsageSource.MISSING


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    model: ModelConfig
    tool_choice: ToolChoice = field(default_factory=ToolChoice)
    recovery: RecoveryBudget = field(default_factory=RecoveryBudget)
    deadline_monotonic: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))


@dataclass(frozen=True)
class ModelResult:
    text: str | None = None
    reasoning: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str | None = None
    attempt: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))

    def is_effective(self) -> bool:
        has_text = bool(self.text and self.text.strip())
        return has_text or bool(self.tool_calls)


# --- snapshots and receipts ---


@dataclass(frozen=True)
class ContextContribution:
    source: str
    content: str
    scope: str
    priority: int
    dedupe_key: str


@dataclass(frozen=True)
class CapabilitySnapshot:
    version: str
    tools: tuple[ToolSpec, ...]
    binding_ref: BindingSetRef
    contributions: tuple[ContextContribution, ...] = ()
    policy_ref: CapabilityPolicyRef = field(default_factory=lambda: CapabilityPolicyRef("default"))

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "contributions", tuple(self.contributions))

    def tool(self, name: str) -> ToolSpec | None:
        for spec in self.tools:
            if spec.name == name:
                return spec
        return None


@dataclass(frozen=True)
class PreparedContext:
    messages: tuple[Message, ...]
    contributions: tuple[ContextContribution, ...]
    estimated_tokens: int
    compressed: bool = False


@dataclass(frozen=True)
class PreparedStep:
    identity: StepIdentity
    record_target: RecordTarget
    request: ModelRequest
    capabilities: CapabilitySnapshot
    context_version: ContextVersion


@dataclass(frozen=True)
class MessageReceipt:
    message_id: str
    run_id: str
    step_no: int | None
    record_target: RecordTarget
    logical_op_id: str
    created: bool


@dataclass(frozen=True)
class CallRecord:
    call_id: str
    name: str
    message_id: str


@dataclass(frozen=True)
class BatchReceipt:
    run_id: str
    step_no: int
    record_target: RecordTarget
    logical_op_id: str
    calls: tuple[CallRecord, ...]
    assistant_message_id: str | None
    recording_required: bool = True
    created: bool = True


@dataclass(frozen=True)
class PendingRef:
    value: str
    completed_call_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class FlowDecision:
    flow: Flow
    reason: str | None = None
    pending_ref: PendingRef | None = None


@dataclass(frozen=True)
class TranscriptSnapshot:
    version: str
    record_target: RecordTarget
    messages: tuple[Message, ...]


@dataclass(frozen=True)
class ApplicationSnapshot:
    version: str
    record_target: RecordTarget
    payload: Mapping[str, JsonValue] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_mapping(self.payload))


@dataclass(frozen=True)
class ToolBatchCommand:
    identity: StepIdentity
    record_target: RecordTarget
    calls: tuple[ToolCall, ...]
    capabilities: CapabilitySnapshot
    assistant_text: str | None = None
    reasoning: str | None = None
    previous_receipt: BatchReceipt | None = None
    attempt: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "calls", tuple(self.calls))


@dataclass(frozen=True)
class CompletionCommand:
    step: PreparedStep
    result: ModelResult


@dataclass(frozen=True)
class Resolution:
    kind: MatchKind
    snapshot: CapabilitySnapshot | None = None
    reason: str | None = None


# --- execution entries ---


@dataclass(frozen=True)
class ModelEntry:
    kind: Literal["model"] = "model"


@dataclass(frozen=True)
class ToolBatchEntry:
    kind: Literal["tool_batch"] = "tool_batch"
    calls: tuple[ToolCall, ...] = ()
    assistant_text: str | None = None
    reasoning: str | None = None
    previous_receipt: BatchReceipt | None = None
    continue_after: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "calls", tuple(self.calls))


@dataclass(frozen=True)
class CompletedEntry:
    kind: Literal["completed"] = "completed"


ExecutionEntry = ModelEntry | ToolBatchEntry | CompletedEntry


@dataclass(frozen=True)
class RunRequest:
    run_id: str
    input_items: tuple[Message, ...]
    record_target: RecordTarget
    entry: ExecutionEntry
    limits: ExecutionLimits = field(default_factory=ExecutionLimits)
    identity: ActorIdentity | None = None
    model: ModelConfig = field(default_factory=ModelConfig)
    consumed: ConsumedBudget = field(default_factory=ConsumedBudget)
    tool_choice: ToolChoice = field(default_factory=ToolChoice)

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_items", tuple(self.input_items))


class RunControl:
    """Per-run cancel sources and deadline. Not a service locator and not frozen."""

    def __init__(self, *, deadline_monotonic: float | None = None) -> None:
        self._deadline = deadline_monotonic
        self._events = {
            StopReason.USER_STOP: asyncio.Event(),
            StopReason.HOST_CANCEL: asyncio.Event(),
            StopReason.CONSUMER_CLOSED: asyncio.Event(),
        }
        self._first: StopReason | None = None
        self._any = asyncio.Event()

    def request_user_stop(self) -> None:
        self._trigger(StopReason.USER_STOP)

    def request_host_cancel(self) -> None:
        self._trigger(StopReason.HOST_CANCEL)

    def notify_consumer_closed(self) -> None:
        self._trigger(StopReason.CONSUMER_CLOSED)

    def _trigger(self, reason: StopReason) -> None:
        self._events[reason].set()
        if self._first is None:
            self._first = reason
            self._any.set()

    @property
    def deadline_monotonic(self) -> float | None:
        return self._deadline

    def reason_set(self, reason: StopReason) -> bool:
        if reason is StopReason.DEADLINE:
            return self._deadline is not None and time.monotonic() >= self._deadline
        return self._events[reason].is_set()

    @property
    def first_reason(self) -> StopReason | None:
        if self._first is not None:
            return self._first
        if self.reason_set(StopReason.DEADLINE):
            return StopReason.DEADLINE
        return None

    def is_set(self) -> bool:
        return self.first_reason is not None

    def remaining_seconds(self) -> float | None:
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - time.monotonic())

    async def wait(self) -> StopReason:
        current = self.first_reason
        if current is not None:
            return current
        remaining = self.remaining_seconds()
        if remaining is None:
            await self._any.wait()
            assert self._first is not None
            return self._first
        try:
            async with asyncio.timeout(remaining):
                await self._any.wait()
        except TimeoutError:
            return StopReason.DEADLINE
        assert self._first is not None
        return self._first


# --- events ---


@dataclass(frozen=True)
class TextDeltaEvent:
    identity: StepIdentity
    text: str
    attempt: int
    receipt: MessageReceipt | None = None
    kind: Literal["text_delta"] = "text_delta"


@dataclass(frozen=True)
class ReasoningDeltaEvent:
    identity: StepIdentity
    text: str
    attempt: int
    kind: Literal["reasoning_delta"] = "reasoning_delta"


@dataclass(frozen=True)
class ModelCompleted:
    identity: StepIdentity
    result: ModelResult
    receipt: MessageReceipt | None = None
    kind: Literal["model_completed"] = "model_completed"


@dataclass(frozen=True)
class ToolCallRecordedEvent:
    identity: StepIdentity
    receipt: BatchReceipt
    kind: Literal["tool_calls_recorded"] = "tool_calls_recorded"


@dataclass(frozen=True)
class ToolProgressEvent:
    identity: StepIdentity
    call_id: str
    payload: Mapping[str, JsonValue]
    kind: Literal["tool_progress"] = "tool_progress"

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_mapping(self.payload))


@dataclass(frozen=True)
class ToolResultEvent:
    identity: StepIdentity
    result: ToolResult
    receipt: MessageReceipt | None = None
    kind: Literal["tool_result"] = "tool_result"


@dataclass(frozen=True)
class ToolBatchCompleted:
    identity: StepIdentity
    decision: FlowDecision
    results: tuple[ToolResult, ...]
    receipt: BatchReceipt | None = None
    kind: Literal["tool_batch_completed"] = "tool_batch_completed"


@dataclass(frozen=True)
class CompletionCompleted:
    identity: StepIdentity
    decision: FlowDecision
    receipt: MessageReceipt | None = None
    kind: Literal["completion_completed"] = "completion_completed"


@dataclass(frozen=True)
class RunStarted:
    run_id: str
    record_target: RecordTarget
    kind: Literal["run_started"] = "run_started"


@dataclass(frozen=True)
class StepStarted:
    identity: StepIdentity
    phase: str
    kind: Literal["step_started"] = "step_started"


@dataclass(frozen=True)
class RunSucceeded:
    run_id: str
    kind: Literal["run_succeeded"] = "run_succeeded"


@dataclass(frozen=True)
class RunFailed:
    run_id: str
    error_type: str
    message: str
    kind: Literal["run_failed"] = "run_failed"


@dataclass(frozen=True)
class RunCancelled:
    run_id: str
    reason: StopReason
    kind: Literal["run_cancelled"] = "run_cancelled"


@dataclass(frozen=True)
class RunWaiting:
    run_id: str
    pending_ref: PendingRef
    decision: FlowDecision
    kind: Literal["run_waiting"] = "run_waiting"


ModelStepEvent = TextDeltaEvent | ReasoningDeltaEvent | ModelCompleted
ToolBatchEvent = ToolCallRecordedEvent | ToolProgressEvent | ToolResultEvent | ToolBatchCompleted
CompletionEvent = CompletionCompleted
RuntimeEvent = (
    RunStarted
    | StepStarted
    | TextDeltaEvent
    | ReasoningDeltaEvent
    | ModelCompleted
    | ToolCallRecordedEvent
    | ToolProgressEvent
    | ToolResultEvent
    | ToolBatchCompleted
    | CompletionCompleted
    | RunSucceeded
    | RunFailed
    | RunCancelled
    | RunWaiting
)


def freeze_object_map(value: Mapping[str, object] | None) -> Mapping[str, JsonValue]:
    return freeze_mapping(value)


def copy_json(value: object) -> JsonValue:
    return freeze_json(value)


def sequence_to_tuple[T](items: Sequence[T] | None) -> tuple[T, ...]:
    return tuple(items or ())
