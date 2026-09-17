"""Default text completion: commit final text, then run completion policy once."""

from __future__ import annotations

from collections.abc import AsyncIterator

from agent_runtime.contracts import (
    CompletionCommand,
    CompletionCompleted,
    CompletionEvent,
    Flow,
    FlowDecision,
    MessageReceipt,
)
from agent_runtime.exceptions import RecordingError
from agent_runtime.lifecycle import CallbackStream, raise_if_stopped
from agent_runtime.ports import CompletionPolicy, ManagedEventStream
from agent_runtime.session import RunSession


class FinishCompletionPolicy:
    async def apply(self, command: CompletionCommand, receipt: MessageReceipt) -> FlowDecision:
        del command, receipt
        return FlowDecision(flow=Flow.FINISH, reason="default")


class DefaultCompletionPipeline:
    def __init__(self, session: RunSession, policy: CompletionPolicy | None = None) -> None:
        self._session = session
        self._policy = policy if policy is not None else FinishCompletionPolicy()

    def complete(self, command: CompletionCommand) -> ManagedEventStream[CompletionEvent]:
        wrapped = CallbackStream(self._complete(command))
        return self._session.scope.register_stream(wrapped)

    async def _complete(self, command: CompletionCommand) -> AsyncIterator[CompletionEvent]:
        raise_if_stopped(self._session.control)
        attempt = command.result.attempt
        try:
            receipt = await self._session.transcript.record_text_final(
                run_id=command.step.identity.run_id,
                step_no=command.step.identity.step_no,
                record_target=command.step.record_target,
                text=command.result.text,
                reasoning=command.result.reasoning,
                tool_calls=command.result.tool_calls,
                logical_op_id=(
                    f"{command.step.identity.run_id}:{command.step.identity.step_no}:"
                    f"{command.step.record_target.value}:a{attempt}:final"
                ),
                attempt=attempt,
            )
        except RecordingError:
            raise
        self._session.completion_policy_calls += 1
        try:
            decision = await self._policy.apply(command, receipt)
        except Exception as exc:
            raise RecordingError("completion policy state commit failed") from exc
        if decision.flow is Flow.WAIT and (decision.pending_ref is None or not decision.pending_ref.value):
            raise RecordingError("WAIT requires a pending_ref")
        yield CompletionCompleted(identity=command.step.identity, decision=decision, receipt=receipt)
