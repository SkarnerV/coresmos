"""Core and default-pipeline ports. Implementations need not inherit these classes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from agent_runtime.contracts import (
    ApplicationSnapshot,
    BatchReceipt,
    BindingSetRef,
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
    ToolBatchCommand,
    ToolBatchEvent,
    ToolCall,
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
    async def invoke(
        self,
        call: ToolCall,
        binding: BindingSetRef,
        control: RunControl,
    ) -> InvocationOutcome: ...


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
    async def get(self, record_target: RecordTarget) -> tuple[Message, ...]: ...

    async def put(self, record_target: RecordTarget, messages: tuple[Message, ...]) -> None: ...

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


class TokenEstimator(Protocol):
    def estimate_text(self, text: str) -> int: ...

    def estimate_schema(self, schema: Mapping[str, JsonValue]) -> int: ...

    def can_estimate(self, text: str) -> bool: ...


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
