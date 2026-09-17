"""Default tool batch: validate all → record calls → sequential invoke → commit results → decide."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence

from agent_runtime.capabilities.providers import BindingRegistry
from agent_runtime.capabilities.schema import validate_tool_call
from agent_runtime.contracts import (
    ExternalWorkDeclared,
    ExternalWorkRef,
    Flow,
    FlowDecision,
    InvocationOutcome,
    StopReason,
    ToolBatchCommand,
    ToolBatchCompleted,
    ToolBatchEvent,
    ToolCall,
    ToolCallRecordedEvent,
    ToolInvocation,
    ToolProgressEvent,
    ToolResultEvent,
)
from agent_runtime.exceptions import CancelledRunError, CapabilityError, RecordingError, SchemaValidationError
from agent_runtime.jsonutil import JsonValue
from agent_runtime.lifecycle import CallbackStream, cancel_and_wait, raise_if_stopped
from agent_runtime.observability import timed
from agent_runtime.policies import aggregate_flow_decisions, model_visible_tool_error
from agent_runtime.ports import ManagedEventStream, ResultPolicy, ToolInvoker
from agent_runtime.session import RunSession


class DefaultResultPolicy:
    def __init__(self, session: RunSession) -> None:
        self._session = session

    async def apply(
        self,
        *,
        results: Sequence[InvocationOutcome],
        command: ToolBatchCommand,
    ) -> FlowDecision:
        """Apply every required side effect in order, then aggregate one control decision.

        A `next_record_target` takes effect from the next step onward: this batch's calls and
        results stay on the target the model decided against. Targets are never merged, so a
        run that switches target and continues prepares its next request from the new target's
        history alone.
        """
        del command
        decisions: list[FlowDecision] = []
        for outcome in results:
            self._session.policy_calls += 1
            if outcome.next_record_target is not None:
                self._session.record_target = outcome.next_record_target
            if outcome.activate_tools:
                self._session.capabilities.activate(outcome.activate_tools)
            if outcome.contributions:
                self._session.extra_contributions.extend(outcome.contributions)
            if outcome.state_payload is not None:
                await self._session.application.commit(self._session.record_target, outcome.state_payload)
            if outcome.flow_hint is not None:
                decisions.append(outcome.flow_hint)
        if not decisions:
            return FlowDecision(flow=Flow.CONTINUE, reason="default")
        return aggregate_flow_decisions(decisions)


class DefaultToolPipeline:
    def __init__(
        self,
        invoker: ToolInvoker,
        session: RunSession,
        bindings: BindingRegistry,
        result_policy: ResultPolicy | None = None,
    ) -> None:
        self._invoker = invoker
        self._session = session
        self._bindings = bindings
        self._policy = result_policy if result_policy is not None else DefaultResultPolicy(session)

    def execute(self, command: ToolBatchCommand) -> ManagedEventStream[ToolBatchEvent]:
        wrapped = CallbackStream(self._execute(command))
        return self._session.scope.register_stream(wrapped)

    async def _execute(self, command: ToolBatchCommand) -> AsyncIterator[ToolBatchEvent]:
        raise_if_stopped(self._session.control)
        _validate_batch(command, self._bindings)
        record_calls = timed(
            "record_tool_calls",
            command.identity.run_id,
            step_no=command.identity.step_no,
        )
        receipt = await self._session.transcript.record_tool_calls(
            run_id=command.identity.run_id,
            step_no=command.identity.step_no,
            record_target=command.record_target,
            calls=command.calls,
            assistant_text=command.assistant_text,
            reasoning=command.reasoning,
            logical_op_id=(
                f"{command.identity.run_id}:{command.identity.step_no}:{command.record_target.value}:tool_calls"
            ),
            previous_receipt=command.previous_receipt,
            attempt=command.attempt,
        )
        await self._session.observe(record_calls())
        yield ToolCallRecordedEvent(identity=command.identity, receipt=receipt)
        outcomes: list[InvocationOutcome] = []
        for call in command.calls:
            raise_if_stopped(self._session.control)
            invoke = timed(
                "tool_call",
                command.identity.run_id,
                step_no=command.identity.step_no,
                call_id=call.call_id,
            )
            invoked: list[InvocationOutcome] = []
            async for reported in self._invoke(command, call, invoked):
                yield reported
            await self._session.observe(invoke())
            outcome = invoked[0]
            visible = outcome.result
            if visible.is_error:
                outcome = InvocationOutcome(
                    result=model_visible_tool_error(visible),
                    flow_hint=outcome.flow_hint,
                    next_record_target=outcome.next_record_target,
                    activate_tools=outcome.activate_tools,
                    contributions=outcome.contributions,
                    state_payload=outcome.state_payload,
                )
            outcomes.append(outcome)
            yield ToolResultEvent(identity=command.identity, result=outcome.result)
        record_results = timed(
            "record_tool_results",
            command.identity.run_id,
            step_no=command.identity.step_no,
        )
        if receipt.recording_required:
            await self._session.transcript.record_tool_results(
                run_id=command.identity.run_id,
                step_no=command.identity.step_no,
                record_target=command.record_target,
                results=tuple(item.result for item in outcomes),
                logical_op_id=(
                    f"{command.identity.run_id}:{command.identity.step_no}:{command.record_target.value}:tool_results"
                ),
                previous_receipt=receipt,
            )
        await self._session.observe(record_results())
        try:
            decision = await self._policy.apply(results=tuple(outcomes), command=command)
        except Exception as exc:
            raise RecordingError("required tool result state commit failed") from exc
        yield ToolBatchCompleted(
            identity=command.identity,
            decision=decision,
            results=tuple(item.result for item in outcomes),
            receipt=receipt,
        )

    async def _invoke(
        self,
        command: ToolBatchCommand,
        call: ToolCall,
        sink: list[InvocationOutcome],
    ) -> AsyncIterator[ToolBatchEvent]:
        """Run one call as a task so a stop interrupts the wait, not just the phase boundary.

        Whatever the adapter reports while the call is in flight is forwarded as it arrives.
        Declared external work is kept for the terminal event, because a stop cancels our wait
        without undoing work that already left the process.
        """
        session = self._session
        reported: asyncio.Queue[ToolBatchEvent] = asyncio.Queue()

        def declare(work: ExternalWorkRef) -> None:
            session.external_work.append(work)
            reported.put_nowait(ExternalWorkDeclared(identity=command.identity, work=work))

        def progress(payload: Mapping[str, JsonValue]) -> None:
            reported.put_nowait(ToolProgressEvent(identity=command.identity, call_id=call.call_id, payload=payload))

        invocation = ToolInvocation(
            call=call,
            binding=command.capabilities.binding_ref,
            control=session.control,
            declare_external_work=declare,
            report_progress=progress,
        )
        work = session.scope.create_task(self._invoker.invoke(invocation), name=f"tool-call:{call.call_id}")
        stop = session.scope.create_task(session.control.wait(), name="tool-call-stop")
        try:
            while True:
                while not reported.empty():
                    yield reported.get_nowait()
                if work.done() or stop.done():
                    break
                waiter = session.scope.create_task(reported.get(), name="tool-call-report")
                try:
                    await asyncio.wait({work, stop, waiter}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    if not waiter.done():
                        await cancel_and_wait(waiter)
                if waiter.done() and not waiter.cancelled():
                    yield waiter.result()
            if work.done():
                sink.append(work.result())
            else:
                raise CancelledRunError((session.control.first_reason or StopReason.HOST_CANCEL).value)
        finally:
            await cancel_and_wait(work, stop)
            if work.done() and not work.cancelled():
                # A call that failed at the same moment we stopped still has to be read, or
                # the loop reports its exception as never retrieved.
                work.exception()


def _validate_batch(command: ToolBatchCommand, bindings: BindingRegistry) -> None:
    if not command.calls:
        raise SchemaValidationError("tool batch contains no calls")
    snapshot_names = {spec.name for spec in command.capabilities.tools}
    for call in command.calls:
        if call.name not in snapshot_names:
            raise CapabilityError(f"call {call.name} is not in this step's capability snapshot")
        spec = command.capabilities.tool(call.name)
        assert spec is not None
        validate_tool_call(spec, call)
        bindings.resolve(command.capabilities.binding_ref, call.name)
