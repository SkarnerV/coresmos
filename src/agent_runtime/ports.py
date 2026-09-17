"""Core and default-pipeline ports. Implementations need not inherit these classes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar, runtime_checkable

from agent_runtime.contracts import (
    ApplicationSnapshot,
    BatchReceipt,
    CompletionCommand,
    CompletionEvent,
    ContextContribution,
    FlowDecision,
    InvocationOutcome,
    Message,
    MessageReceipt,
    ModelRequest,
    ModelStepEvent,
    PreparedStep,
    RecordTarget,
    Resolution,
    RunControl,
    RunRequest,
    RunStatus,
    RuntimeEvent,
    StepIdentity,
    SummaryProjection,
    ToolBatchCommand,
    ToolBatchEvent,
    ToolCall,
    ToolInvocation,
    ToolResult,
    TranscriptSnapshot,
)
from agent_runtime.jsonutil import JsonValue

T_co = TypeVar("T_co", covariant=True)


class ManagedEventStream(Protocol[T_co]):
    def __aiter__(self) -> ManagedEventStream[T_co]: ...

    async def __anext__(self) -> T_co: ...

    async def aclose(self) -> None: ...


class StepProvider(Protocol):
    async def prepare(self, identity: StepIdentity) -> PreparedStep: ...


@runtime_checkable
class ProjectionInvalidation(Protocol):
    """Optional on a StepProvider: drop caches derived from a commit that then failed."""

    async def invalidate_summary(self, target: RecordTarget) -> None: ...


class ModelStepPort(Protocol):
    def stream(self, step: PreparedStep) -> ManagedEventStream[ModelStepEvent]: ...


class ToolBatchPort(Protocol):
    def execute(self, command: ToolBatchCommand) -> ManagedEventStream[ToolBatchEvent]: ...


class CompletionPort(Protocol):
    def complete(self, command: CompletionCommand) -> ManagedEventStream[CompletionEvent]: ...


@dataclass(frozen=True)
class ExecutionPorts:
    steps: StepProvider
    model: ModelStepPort
    tools: ToolBatchPort
    completion: CompletionPort


class LowLevelModel(Protocol):
    """Vendor-normalized model. Recovery, recording, and idle timeouts belong to the pipeline."""

    def stream(
        self,
        request: ModelRequest,
        control: RunControl,
    ) -> ManagedEventStream[ModelStepEvent]: ...


class ToolInvoker(Protocol):
    """Executes one validated call. Argument authorization belongs here, not in the snapshot."""

    async def invoke(self, invocation: ToolInvocation) -> InvocationOutcome: ...


class TranscriptPort(Protocol):
    async def record_input(
        self,
        *,
        run_id: str,
        record_target: RecordTarget,
        message: Message,
        logical_op_id: str,
    ) -> MessageReceipt: ...

    async def record_text_delta(
        self,
        *,
        run_id: str,
        step_no: int,
        record_target: RecordTarget,
        text: str,
        logical_op_id: str,
        attempt: int,
        previous_receipt: MessageReceipt | None = None,
    ) -> MessageReceipt: ...

    async def record_text_final(
        self,
        *,
        run_id: str,
        step_no: int,
        record_target: RecordTarget,
        text: str | None,
        reasoning: str | None,
        tool_calls: Sequence[ToolCall],
        logical_op_id: str,
        attempt: int,
        previous_receipt: MessageReceipt | None = None,
    ) -> MessageReceipt: ...

    async def record_tool_calls(
        self,
        *,
        run_id: str,
        step_no: int,
        record_target: RecordTarget,
        calls: Sequence[ToolCall],
        assistant_text: str | None,
        reasoning: str | None,
        logical_op_id: str,
        previous_receipt: BatchReceipt | None = None,
        attempt: int = 1,
    ) -> BatchReceipt: ...

    async def record_tool_results(
        self,
        *,
        run_id: str,
        step_no: int,
        record_target: RecordTarget,
        results: Sequence[ToolResult],
        logical_op_id: str,
        previous_receipt: BatchReceipt,
    ) -> tuple[MessageReceipt, ...]: ...

    def snapshot(self, record_target: RecordTarget) -> TranscriptSnapshot: ...

    async def finalize_run(self, *, run_id: str, status: RunStatus) -> None: ...


class ApplicationStatePort(Protocol):
    async def current(self, record_target: RecordTarget) -> ApplicationSnapshot: ...

    async def commit(
        self,
        record_target: RecordTarget,
        payload: Mapping[str, JsonValue],
    ) -> ApplicationSnapshot: ...


class ResultPolicy(Protocol):
    """May run several required side effects in order, then return one aggregated FlowDecision."""

    async def apply(
        self,
        *,
        results: Sequence[InvocationOutcome],
        command: ToolBatchCommand,
    ) -> FlowDecision: ...


class CompletionPolicy(Protocol):
    async def apply(
        self,
        command: CompletionCommand,
        receipt: MessageReceipt,
    ) -> FlowDecision: ...


class SummaryProjectionPort(Protocol):
    """Store for compressed model views. Projections only; original facts stay in the transcript."""

    async def get(self, record_target: RecordTarget) -> SummaryProjection | None: ...

    async def put(self, record_target: RecordTarget, projection: SummaryProjection) -> None: ...

    async def invalidate(self, record_target: RecordTarget) -> None: ...


class Observer(Protocol):
    async def on_event(self, event: object) -> None: ...


class CapabilityProvider(Protocol):
    async def resolve(self, request: RunRequest, application: ApplicationSnapshot) -> Resolution: ...


class ContextContributor(Protocol):
    async def contribute(
        self,
        *,
        request: RunRequest,
        target: RecordTarget,
        application: ApplicationSnapshot,
    ) -> tuple[ContextContribution, ...]: ...


@runtime_checkable
class CacheableContributor(Protocol):
    """Opt-in scope caching. Only the contributor knows what invalidates its own content.

    Returning a stable key lets the step provider reuse the last contributions instead of
    hitting an external source every model round. Returning None disables caching.
    """

    def cache_key(
        self,
        *,
        request: RunRequest,
        target: RecordTarget,
        application: ApplicationSnapshot,
    ) -> str | None: ...


class TokenEstimator(Protocol):
    def estimate_text(self, text: str) -> int: ...

    def estimate_schema(self, schema: Mapping[str, JsonValue]) -> int: ...

    def can_estimate(self, text: str) -> bool: ...


class Summarizer(Protocol):
    """Turns dropped history into one short text. Model-backed implementations inject a model."""

    async def summarize(self, messages: Sequence[Message], *, budget_tokens: int) -> str: ...


class Compressor(Protocol):
    async def compress(
        self,
        messages: Sequence[Message],
        *,
        budget_tokens: int,
        estimator: TokenEstimator,
        reserved_tokens: int,
    ) -> tuple[tuple[Message, ...], bool]: ...


class Runtime(Protocol):
    def run(
        self,
        request: RunRequest,
        control: RunControl,
    ) -> ManagedEventStream[RuntimeEvent]: ...
