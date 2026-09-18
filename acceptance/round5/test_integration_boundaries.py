"""Independent integration acceptance for the extensions introduced in e21dadf."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from agent_runtime.capabilities import FixedCapabilityProvider, ResolverChain
from agent_runtime.context import TokenBudgetPolicy
from agent_runtime.contracts import (
    ExecutionLimits,
    InvocationOutcome,
    MatchKind,
    Message,
    ModelEntry,
    RecordTarget,
    Resolution,
    Role,
    RunCancelled,
    RunControl,
    RunRequest,
    RunSucceeded,
    StopReason,
    ToolBatchEntry,
    ToolCall,
    ToolInvocation,
    ToolResult,
)
from agent_runtime.exceptions import ReceiptMismatchError
from agent_runtime.recording import MemoryTranscript
from agent_runtime.runner import assemble_default
from agent_runtime.testing import ScriptedInvoker, ScriptedModel, ScriptedTurn, collect_run
from agent_runtime.testing.scenarios import ECHO_TOOL

TARGET = RecordTarget("round5-target")


async def test_new_run_does_not_reuse_another_transcripts_compressed_messages() -> None:
    model = ScriptedModel([ScriptedTurn(text="first answer"), ScriptedTurn(text="second answer")])
    runtime = assemble_default(
        model=model,
        invoker=ScriptedInvoker(),
        budget=TokenBudgetPolicy(max_prompt_tokens=40, reserved_output_tokens=1),
    )
    for run_id, old, current in (
        ("first", "old history " * 100, "first question"),
        ("second", "short history", "second question"),
    ):
        request = RunRequest(
            run_id=run_id,
            input_items=(Message(role=Role.USER, content=old), Message(role=Role.USER, content=current)),
            record_target=TARGET,
            entry=ModelEntry(),
        )
        events = await collect_run(request, RunControl(), runtime)
        assert any(isinstance(event, RunSucceeded) for event in events)
    assert model.call_count == 2
    actual = [message.content for message in model.requests[1].messages]
    assert actual == ["short history", "second question"], f"Second run received stale history: {actual}"


async def test_resolver_fallback_executes_the_selected_providers_binding() -> None:
    class SkipProvider(FixedCapabilityProvider):
        async def resolve(self, request, application):
            return Resolution(kind=MatchKind.NO_MATCH, reason="not this catalog")

    skipped = SkipProvider((ECHO_TOOL,), invoker_key="skipped-backend")
    selected = FixedCapabilityProvider((ECHO_TOOL,), invoker_key="selected-backend")
    chain = ResolverChain((skipped, selected))
    invoked: list[str] = []

    class RoutingInvoker:
        async def invoke(self, invocation: ToolInvocation) -> InvocationOutcome:
            backend = chain.bindings.resolve(invocation.binding, invocation.call.name)
            invoked.append(backend)
            return InvocationOutcome(result=ToolResult(invocation.call.call_id, invocation.call.name, backend))

    runtime = assemble_default(model=ScriptedModel(), invoker=RoutingInvoker(), capability_provider=chain)
    request = RunRequest(
        run_id="fallback",
        input_items=(),
        record_target=TARGET,
        entry=ToolBatchEntry(calls=(ToolCall("c1", "echo", {"text": "hi"}),), continue_after=False),
    )
    events = await collect_run(request, RunControl(), runtime)
    assert any(isinstance(event, RunSucceeded) for event in events)
    assert invoked == ["selected-backend"], f"Fallback resolved the wrong executor: {invoked}"


@pytest.mark.parametrize("reason", [StopReason.USER_STOP, StopReason.HOST_CANCEL, StopReason.DEADLINE])
async def test_stop_interrupts_initial_capability_resolution(reason: StopReason) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    exited = asyncio.Event()

    class SlowProvider(FixedCapabilityProvider):
        async def resolve(self, request, application):
            entered.set()
            try:
                await release.wait()
                return await super().resolve(request, application)
            finally:
                exited.set()

    model = ScriptedModel([ScriptedTurn(text="must not run")])
    runtime = assemble_default(model=model, invoker=ScriptedInvoker(), capability_provider=SlowProvider())
    control = RunControl()
    request = RunRequest(
        run_id=f"resolve-{reason.value}",
        input_items=(),
        record_target=TARGET,
        entry=ModelEntry(),
        limits=ExecutionLimits(deadline_seconds=0.05 if reason is StopReason.DEADLINE else None),
    )
    worker = asyncio.create_task(collect_run(request, control, runtime))
    stopped_while_provider_blocked = False
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        if reason is StopReason.USER_STOP:
            control.request_user_stop()
        elif reason is StopReason.HOST_CANCEL:
            control.request_host_cancel()
        done, _ = await asyncio.wait({worker}, timeout=0.3)
        stopped_while_provider_blocked = worker in done
    finally:
        release.set()
        events = await asyncio.wait_for(worker, timeout=2)
    assert exited.is_set() and model.call_count == 0
    assert stopped_while_provider_blocked, f"{reason.value} did not interrupt the pending capability provider"
    assert any(isinstance(event, RunCancelled) and event.reason is reason for event in events)


async def test_recording_required_flag_cannot_bypass_receipt_validation() -> None:
    transcript = MemoryTranscript()
    batch = await transcript.record_tool_calls(
        run_id="receipt",
        step_no=1,
        record_target=TARGET,
        calls=(ToolCall("c1", "echo", {"text": "hi"}),),
        assistant_text=None,
        reasoning=None,
        logical_op_id="calls",
    )
    assert batch.recording_required
    before = transcript.snapshot(TARGET)
    with pytest.raises(ReceiptMismatchError):
        await transcript.record_tool_results(
            run_id="receipt",
            step_no=1,
            record_target=TARGET,
            results=(ToolResult("c1", "echo", "hi"),),
            logical_op_id="results",
            previous_receipt=replace(batch, recording_required=False),
        )
    assert transcript.snapshot(TARGET) == before
