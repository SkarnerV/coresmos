"""Default model step pipeline: one effective result, request-level recovery, isolated attempts."""

from __future__ import annotations

from collections.abc import AsyncIterator

from agent_runtime.contracts import (
    MessageReceipt,
    ModelCompleted,
    ModelResult,
    ModelStepEvent,
    PreparedStep,
    ReasoningDeltaEvent,
    TextDeltaEvent,
)
from agent_runtime.exceptions import AdapterError, BudgetExhaustedError, CompletionProtocolError
from agent_runtime.lifecycle import CallbackStream, iterate_cancellable, raise_if_stopped, sleep_cancellable
from agent_runtime.observability import timed
from agent_runtime.policies import isolate_attempts
from agent_runtime.ports import LowLevelModel, ManagedEventStream, TranscriptPort
from agent_runtime.session import RunSession


class DefaultModelPipeline:
    def __init__(self, model: LowLevelModel, session: RunSession) -> None:
        self._model = model
        self._session = session

    def stream(self, step: PreparedStep) -> ManagedEventStream[ModelStepEvent]:
        wrapped = CallbackStream(self._stream(step))
        return self._session.scope.register_stream(wrapped)

    async def _stream(self, step: PreparedStep) -> AsyncIterator[ModelStepEvent]:
        recovery = step.request.recovery
        attempts = recovery.remaining_attempts
        if attempts <= 0:
            raise BudgetExhaustedError("remaining_attempts exhausted")
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            raise_if_stopped(self._session.control)
            completed = False
            emitted_output = False
            finish = timed(
                "model_attempt",
                step.identity.run_id,
                step_no=step.identity.step_no,
                attempt=attempt,
            )
            try:
                async for event in self._run_attempt(step, attempt):
                    if isinstance(event, (TextDeltaEvent, ReasoningDeltaEvent)):
                        emitted_output = True
                    yield event
                    if isinstance(event, ModelCompleted):
                        completed = True
            except AdapterError as exc:
                last_error = exc
                completed = False
                if emitted_output and not recovery.retry_partial_output:
                    raise
            finally:
                await self._session.observe(finish())
            if completed:
                return
            if attempt < attempts:
                delay = 0.0
                if recovery.backoff_seconds:
                    delay = recovery.backoff_seconds[min(attempt - 1, len(recovery.backoff_seconds) - 1)]
                await sleep_cancellable(self._session.control, self._session.scope, delay)
        raise AdapterError("empty model response recovery exhausted") from last_error

    async def _run_attempt(self, step: PreparedStep, attempt: int) -> AsyncIterator[ModelStepEvent]:
        transcript: TranscriptPort = self._session.transcript
        request = step.request
        low = self._model.stream(request, self._session.control)
        self._session.scope.register_stream(low)
        previous: MessageReceipt | None = None
        delta_n = 0
        pending: ModelCompleted | None = None
        emitted_output = False
        try:
            async for event in iterate_cancellable(
                low,
                self._session.control,
                self._session.scope,
                idle_timeout=self._session.idle_timeout,
            ):
                if pending is not None:
                    raise CompletionProtocolError("trailing event after model completion")
                if isinstance(event, TextDeltaEvent):
                    emitted_output = True
                    delta_n += 1
                    previous = await transcript.record_text_delta(
                        run_id=step.identity.run_id,
                        step_no=step.identity.step_no,
                        record_target=step.record_target,
                        text=event.text,
                        logical_op_id=_delta_op(step, attempt, delta_n),
                        attempt=attempt,
                        previous_receipt=previous,
                    )
                    yield TextDeltaEvent(
                        identity=step.identity,
                        text=event.text,
                        attempt=attempt,
                        receipt=previous,
                    )
                elif isinstance(event, ReasoningDeltaEvent):
                    emitted_output = True
                    yield ReasoningDeltaEvent(identity=step.identity, text=event.text, attempt=attempt)
                elif isinstance(event, ModelCompleted):
                    if not event.result.is_effective():
                        pending = None
                        continue
                    result = ModelResult(
                        text=event.result.text,
                        reasoning=event.result.reasoning,
                        tool_calls=event.result.tool_calls,
                        usage=event.result.usage,
                        finish_reason=event.result.finish_reason,
                        attempt=attempt,
                    )
                    pending = ModelCompleted(identity=step.identity, result=result)
                else:
                    raise CompletionProtocolError(f"unsupported model event: {type(event)!r}")
        except CompletionProtocolError:
            raise
        except AdapterError:
            if emitted_output and not request.recovery.retry_partial_output:
                raise
            if isolate_attempts():
                return
            raise
        if pending is None:
            return
        receipt = await transcript.record_text_final(
            run_id=step.identity.run_id,
            step_no=step.identity.step_no,
            record_target=step.record_target,
            text=pending.result.text,
            reasoning=pending.result.reasoning,
            tool_calls=pending.result.tool_calls,
            logical_op_id=_final_op(step, pending.result.attempt or attempt),
            attempt=pending.result.attempt or attempt,
            previous_receipt=previous,
        )
        yield ModelCompleted(identity=step.identity, result=pending.result, receipt=receipt)


def _delta_op(step: PreparedStep, attempt: int, delta_n: int) -> str:
    return f"{step.identity.run_id}:{step.identity.step_no}:{step.record_target.value}:a{attempt}:delta:{delta_n}"


def _final_op(step: PreparedStep, attempt: int) -> str:
    return f"{step.identity.run_id}:{step.identity.step_no}:{step.record_target.value}:a{attempt}:final"
