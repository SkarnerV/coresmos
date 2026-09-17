"""Default tool batch: validate all → record calls → sequential invoke → commit results → decide."""

from __future__ import annotations

from collections.abc import AsyncIterator

from agent_runtime.capabilities.providers import BindingRegistry
from agent_runtime.capabilities.schema import validate_tool_call
from agent_runtime.contracts import (
    Flow,
    FlowDecision,
    InvocationOutcome,
    ToolBatchCommand,
    ToolBatchCompleted,
    ToolBatchEvent,
    ToolCallRecordedEvent,
    ToolResultEvent,
)
from agent_runtime.exceptions import CapabilityError, RecordingError, SchemaValidationError
from agent_runtime.lifecycle import CallbackStream, raise_if_stopped
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
        results: tuple[InvocationOutcome, ...],
        command: ToolBatchCommand,
    ) -> FlowDecision:
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
            outcome = await self._invoker.invoke(call, command.capabilities.binding_ref, self._session.control)
            await self._session.observe(invoke())
            visible = outcome.result
            if visible.is_error:
                visible = model_visible_tool_error(visible)
                outcome = InvocationOutcome(
                    result=visible,
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
        try:
            result_receipts = await self._session.transcript.record_tool_results(
                run_id=command.identity.run_id,
                step_no=command.identity.step_no,
                record_target=command.record_target,
                results=tuple(item.result for item in outcomes),
                logical_op_id=(
                    f"{command.identity.run_id}:{command.identity.step_no}:{command.record_target.value}:tool_results"
                ),
                previous_receipt=receipt,
            )
        except RecordingError:
            raise
        await self._session.observe(record_results())
        del result_receipts
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
