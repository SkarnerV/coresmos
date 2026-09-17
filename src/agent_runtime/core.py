"""Four-port agent loop. Tool names and business types are not inspected here."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from agent_runtime.contracts import (
    CompletedEntry,
    CompletionCommand,
    CompletionCompleted,
    ConsumedBudget,
    ExecutionLimits,
    Flow,
    ModelCompleted,
    ModelEntry,
    PendingRef,
    RunRequest,
    RuntimeEvent,
    RunWaiting,
    StepIdentity,
    StepStarted,
    ToolBatchCommand,
    ToolBatchCompleted,
    ToolBatchEntry,
)
from agent_runtime.exceptions import BudgetExhaustedError, CompletionProtocolError, ProtocolError
from agent_runtime.lifecycle import RunScope, iterate_cancellable, raise_if_stopped, wait_cancellable
from agent_runtime.ports import ExecutionPorts, ManagedEventStream


async def iterate_strict[T](
    stream: ManagedEventStream[T],
    is_completion: Callable[[T], bool],
    scope: RunScope,
) -> AsyncIterator[T]:
    """Yield events immediately. Completion must be last; drain trailing events as errors."""
    completion: T | None = None
    last: T | None = None
    try:
        async for item in iterate_cancellable(stream, scope.control, scope, idle_timeout=None):
            if completion is not None:
                raise CompletionProtocolError("trailing event after completion")
            if is_completion(item):
                completion = item
            last = item
            yield item
    except CompletionProtocolError:
        raise
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        if completion is not None:
            raise CompletionProtocolError("error after completion event") from exc
        raise
    if completion is None:
        raise CompletionProtocolError("missing completion event")
    if last is None or not is_completion(last):
        raise CompletionProtocolError("completion event must be last")


async def consume_strict[T](
    stream: ManagedEventStream[T],
    is_completion: Callable[[T], bool],
    scope: RunScope,
) -> tuple[T, tuple[T, ...]]:
    """Exactly one completion event, last item; drain after it to reject trailing events."""
    items: list[T] = []
    completion: T | None = None
    async for item in iterate_strict(stream, is_completion, scope):
        items.append(item)
        if is_completion(item):
            completion = item
    if completion is None:
        raise CompletionProtocolError("missing completion event")
    return completion, tuple(items)


class AgentLoop:
    def __init__(
        self,
        ports: ExecutionPorts,
        *,
        scope: RunScope,
        limits: ExecutionLimits,
        consumed: ConsumedBudget,
    ) -> None:
        self._ports = ports
        self._scope = scope
        self._limits = limits
        self._consumed = consumed

    async def run(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        entry = request.entry
        if isinstance(entry, CompletedEntry):
            return
        step_no = self._consumed.steps
        model_rounds = self._consumed.model_rounds

        async def allocate(*, consume_model: bool) -> StepIdentity:
            nonlocal step_no, model_rounds
            raise_if_stopped(self._scope.control)
            if self._limits.max_steps is not None and step_no + 1 > self._limits.max_steps:
                raise BudgetExhaustedError("max_steps exhausted")
            if (
                consume_model
                and self._limits.max_model_rounds is not None
                and model_rounds + 1 > self._limits.max_model_rounds
            ):
                raise BudgetExhaustedError("max_model_rounds exhausted")
            step_no += 1
            if consume_model:
                model_rounds += 1
            return StepIdentity(run_id=request.run_id, step_no=step_no, model_round=model_rounds)

        if isinstance(entry, ToolBatchEntry):
            identity = await allocate(consume_model=False)
            yield StepStarted(identity=identity, phase="tools")
            prepared = await wait_cancellable(
                self._ports.steps.prepare(identity),
                self._scope.control,
                self._scope,
            )
            command = ToolBatchCommand(
                identity=identity,
                record_target=prepared.record_target,
                calls=entry.calls,
                capabilities=prepared.capabilities,
                assistant_text=entry.assistant_text,
                reasoning=entry.reasoning,
                previous_receipt=entry.previous_receipt,
            )
            completed: ToolBatchCompleted | None = None
            async for tool_event in iterate_strict(
                self._ports.tools.execute(command),
                lambda event: isinstance(event, ToolBatchCompleted),
                self._scope,
            ):
                yield tool_event
                if isinstance(tool_event, ToolBatchCompleted):
                    completed = tool_event
            if completed is None:
                raise CompletionProtocolError("tool batch completion event has the wrong type")
            decision = completed.decision
            if decision.flow is Flow.FINISH:
                return
            if decision.flow is Flow.WAIT:
                yield RunWaiting(run_id=request.run_id, pending_ref=_require_pending(decision), decision=decision)
                return
            if not entry.continue_after:
                return
        elif isinstance(entry, ModelEntry):
            pass
        else:
            raise ProtocolError(f"unsupported execution entry: {type(entry)!r}")

        while True:
            identity = await allocate(consume_model=True)
            yield StepStarted(identity=identity, phase="model")
            prepared = await wait_cancellable(
                self._ports.steps.prepare(identity),
                self._scope.control,
                self._scope,
            )
            model_done: ModelCompleted | None = None
            async for model_event in iterate_strict(
                self._ports.model.stream(prepared),
                lambda event: isinstance(event, ModelCompleted),
                self._scope,
            ):
                yield model_event
                if isinstance(model_event, ModelCompleted):
                    model_done = model_event
            if model_done is None:
                raise CompletionProtocolError("model completion event has the wrong type")
            result = model_done.result
            if result.tool_calls:
                yield StepStarted(identity=identity, phase="tools")
                command = ToolBatchCommand(
                    identity=identity,
                    record_target=prepared.record_target,
                    calls=result.tool_calls,
                    capabilities=prepared.capabilities,
                    assistant_text=result.text,
                    reasoning=result.reasoning,
                    attempt=result.attempt,
                )
                tool_done: ToolBatchCompleted | None = None
                async for batch_event in iterate_strict(
                    self._ports.tools.execute(command),
                    lambda event: isinstance(event, ToolBatchCompleted),
                    self._scope,
                ):
                    yield batch_event
                    if isinstance(batch_event, ToolBatchCompleted):
                        tool_done = batch_event
                if tool_done is None:
                    raise CompletionProtocolError("tool batch completion event has the wrong type")
                decision = tool_done.decision
            else:
                yield StepStarted(identity=identity, phase="completion")
                completion_done: CompletionCompleted | None = None
                async for completion_event in iterate_strict(
                    self._ports.completion.complete(CompletionCommand(step=prepared, result=result)),
                    lambda event: isinstance(event, CompletionCompleted),
                    self._scope,
                ):
                    yield completion_event
                    if isinstance(completion_event, CompletionCompleted):
                        completion_done = completion_event
                if completion_done is None:
                    raise CompletionProtocolError("completion event has the wrong type")
                decision = completion_done.decision
            if decision.flow is Flow.CONTINUE:
                continue
            if decision.flow is Flow.WAIT:
                yield RunWaiting(run_id=request.run_id, pending_ref=_require_pending(decision), decision=decision)
                return
            return


def _require_pending(decision: object) -> PendingRef:
    pending = getattr(decision, "pending_ref", None)
    if not isinstance(pending, PendingRef) or not pending.value:
        raise ProtocolError("WAIT is missing a pending_ref")
    return pending
