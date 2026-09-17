"""Independent acceptance regressions; run explicitly (outside the normal testpaths)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from agent_runtime.application import MemoryApplicationState
from agent_runtime.capabilities import CapabilitySession, FixedCapabilityProvider
from agent_runtime.context import DefaultStepProvider, RunView, TokenBudgetPolicy, pair_tool_messages
from agent_runtime.contracts import (
    ApplicationSnapshot,
    BatchReceipt,
    BindingSetRef,
    CallRecord,
    ContextContribution,
    ExecutionEntry,
    InvocationOutcome,
    Message,
    ModelCompleted,
    ModelEntry,
    ModelRequest,
    ModelResult,
    ModelStepEvent,
    RecordTarget,
    Role,
    RunControl,
    RunFailed,
    RunRequest,
    RunSucceeded,
    StepIdentity,
    TextDeltaEvent,
    ToolBatchEntry,
    ToolCall,
    ToolResult,
)
from agent_runtime.exceptions import AdapterError, ContextBudgetError, ReceiptMismatchError
from agent_runtime.lifecycle import CallbackStream
from agent_runtime.recording import MemoryTranscript
from agent_runtime.runner import DefaultRuntime, assemble_default
from agent_runtime.testing.models import ScriptedModel, ScriptedTurn
from agent_runtime.testing.scenarios import ECHO_TOOL, collect_run
from agent_runtime.testing.tools import ScriptedInvoker

TARGET = RecordTarget("acceptance-target")
CALL = ToolCall("c1", "echo", {"text": "hello"})
IDENTITY = StepIdentity("acceptance", 1, 1)


def make_request(entry: ExecutionEntry | None = None) -> RunRequest:
    return RunRequest(run_id="acceptance", input_items=(), record_target=TARGET, entry=entry or ModelEntry())


async def record_calls(transcript: MemoryTranscript, calls: tuple[ToolCall, ...]) -> BatchReceipt:
    return await transcript.record_tool_calls(
        run_id="acceptance",
        step_no=1,
        record_target=TARGET,
        calls=calls,
        assistant_text=None,
        reasoning=None,
        logical_op_id="calls",
    )


class GatedModel:
    def __init__(self) -> None:
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()
        self.exited = asyncio.Event()

    def stream(self, request: ModelRequest, control: RunControl) -> CallbackStream[ModelStepEvent]:
        async def events() -> AsyncIterator[ModelStepEvent]:
            try:
                yield TextDeltaEvent(identity=IDENTITY, text="hello", attempt=1)
                self.blocked.set()
                await self.release.wait()
                yield ModelCompleted(identity=IDENTITY, result=ModelResult(text="hello"))
            finally:
                self.exited.set()

        return CallbackStream(events())


async def test_text_delta_is_visible_before_model_finishes() -> None:
    model = GatedModel()
    runtime = assemble_default(model=model, invoker=ScriptedInvoker(), tools=())
    stream = runtime.run(make_request(), RunControl())
    saw_delta = asyncio.Event()

    async def consume() -> None:
        try:
            async for event in stream:
                if isinstance(event, TextDeltaEvent):
                    saw_delta.set()
        finally:
            await stream.aclose()

    worker = asyncio.create_task(consume())
    observer = asyncio.create_task(saw_delta.wait())
    try:
        await asyncio.wait_for(model.blocked.wait(), timeout=2)
        done, _ = await asyncio.wait({observer}, timeout=0.5)
        streamed_before_completion = observer in done
    finally:
        model.release.set()
        await asyncio.wait_for(worker, timeout=2)
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
    assert model.exited.is_set()
    assert streamed_before_completion, "Runtime withheld the delta until the entire model stream ended"


async def test_tool_entry_continue_after_false_does_not_call_model() -> None:
    model = ScriptedModel([ScriptedTurn(text="unexpected model call")])
    invoker = ScriptedInvoker()
    runtime = assemble_default(model=model, invoker=invoker, tools=(ECHO_TOOL,))
    events = await collect_run(make_request(ToolBatchEntry(calls=(CALL,), continue_after=False)), RunControl(), runtime)
    assert invoker.invoke_count == 1
    assert any(isinstance(event, RunSucceeded) for event in events)
    assert model.call_count == 0, "continue_after=False still entered the model loop"


async def test_failed_result_batch_does_not_publish_partial_history() -> None:
    transcript = MemoryTranscript()
    receipt = await record_calls(transcript, (CALL, ToolCall("c2", "echo", {"text": "two"})))
    before = transcript.snapshot(TARGET)
    with pytest.raises(ReceiptMismatchError):
        await transcript.record_tool_results(
            run_id="acceptance",
            step_no=1,
            record_target=TARGET,
            results=(ToolResult("c1", "echo", "ok"), ToolResult("unknown", "echo", "bad")),
            logical_op_id="results",
            previous_receipt=receipt,
        )
    assert transcript.snapshot(TARGET) == before, "A rejected batch committed its first result"


async def test_result_receipt_cannot_add_an_unrecorded_call() -> None:
    transcript = MemoryTranscript()
    receipt = await record_calls(transcript, (CALL,))
    forged = replace(receipt, calls=(CallRecord("unrecorded", "echo", receipt.assistant_message_id),))
    with pytest.raises(ReceiptMismatchError):
        await transcript.record_tool_results(
            run_id="acceptance",
            step_no=1,
            record_target=TARGET,
            results=(ToolResult("unrecorded", "echo", "ok"),),
            logical_op_id="results",
            previous_receipt=forged,
        )


async def test_result_receipt_is_bound_to_its_run_step_and_target() -> None:
    transcript = MemoryTranscript()
    receipt = await record_calls(transcript, (CALL,))
    with pytest.raises(ReceiptMismatchError):
        await transcript.record_tool_results(
            run_id="different-run",
            step_no=99,
            record_target=RecordTarget("different-target"),
            results=(ToolResult("c1", "echo", "ok"),),
            logical_op_id="other-results",
            previous_receipt=receipt,
        )


class ContributingInvoker(ScriptedInvoker):
    async def invoke(self, call: ToolCall, binding: BindingSetRef, control: RunControl) -> InvocationOutcome:
        outcome = await super().invoke(call, binding, control)
        return replace(
            outcome,
            contributions=(ContextContribution("tool-context", "NEXT_ROUND_CONTEXT", "run", 10, "k"),),
        )


async def test_tool_context_contribution_reaches_next_request() -> None:
    model = ScriptedModel([ScriptedTurn(tool_calls=(CALL,)), ScriptedTurn(text="done")])
    invoker = ContributingInvoker()
    runtime = assemble_default(model=model, invoker=invoker, tools=(ECHO_TOOL,))
    events = await collect_run(make_request(), RunControl(), runtime)
    assert invoker.invoke_count == 1 and model.call_count == 2
    assert any(isinstance(event, RunSucceeded) for event in events)
    assert any(message.content == "NEXT_ROUND_CONTEXT" for message in model.requests[1].messages), (
        "InvocationOutcome.contributions was discarded by the default pipeline"
    )


async def test_prepared_request_budget_includes_tool_call_arguments() -> None:
    transcript = MemoryTranscript()
    call = ToolCall("c1", "echo", {"text": "x" * 6000})
    receipt = await record_calls(transcript, (call,))
    await transcript.record_tool_results(
        run_id="acceptance",
        step_no=1,
        record_target=TARGET,
        results=(ToolResult("c1", "echo", "ok"),),
        logical_op_id="results",
        previous_receipt=receipt,
    )
    request = make_request()
    application = MemoryApplicationState(TARGET)
    provider = FixedCapabilityProvider((ECHO_TOOL,))
    snapshot = (await provider.resolve(request, await application.current(TARGET))).snapshot
    assert snapshot is not None
    budget = TokenBudgetPolicy(max_prompt_tokens=512, reserved_output_tokens=16)
    steps = DefaultStepProvider(
        transcript=transcript,
        capabilities=CapabilitySession(snapshot, provider.bindings),
        application=application,
        view=RunView(request=request, record_target=TARGET),
        budget=budget,
    )
    try:
        prepared = await steps.prepare(IDENTITY)
    except ContextBudgetError:
        return  # Rejecting an oversized request is also acceptable.
    estimated = budget.estimate_messages(prepared.request.messages)
    available = budget.available_for_prompt(prepared.request.tools)
    assert estimated <= available, f"Prepared request uses {estimated} tokens; available budget is {available}"


def test_history_projection_does_not_leave_unmatched_assistant_calls() -> None:
    messages = (
        Message(role=Role.ASSISTANT, tool_calls=(CALL, ToolCall("c2", "echo", {"text": "two"}))),
        Message(role=Role.TOOL, tool_call_id="c1", name="echo", content="ok"),
        Message(role=Role.USER, content="next request"),
    )
    projected = pair_tool_messages(messages)
    for index, message in enumerate(projected):
        if message.role is not Role.ASSISTANT or not message.tool_calls:
            continue
        results = []
        for following in projected[index + 1 :]:
            if following.role is not Role.TOOL:
                break
            results.append(following.tool_call_id)
        assert sorted(results) == sorted(call.call_id for call in message.tool_calls), (
            "Projection forwarded an incomplete tool-call/result group"
        )


async def test_host_task_cancellation_propagates_and_closes_model() -> None:
    model = GatedModel()
    runtime = assemble_default(model=model, invoker=ScriptedInvoker(), tools=())
    worker = asyncio.create_task(collect_run(make_request(), RunControl(), runtime))
    try:
        await asyncio.wait_for(model.blocked.wait(), timeout=2)
        worker.cancel()
        result = (await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), timeout=2))[0]
    finally:
        model.release.set()
        await asyncio.gather(worker, return_exceptions=True)
    assert isinstance(result, asyncio.CancelledError), f"Host task cancellation was converted to {result!r}"
    assert model.exited.is_set(), "Underlying model task did not exit before cancellation returned"


class GatedApplication(MemoryApplicationState):
    def __init__(self) -> None:
        super().__init__(TARGET)
        self.calls = 0
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def current(self, record_target: RecordTarget) -> ApplicationSnapshot:
        self.calls += 1
        if self.calls == 2:  # Initialization succeeds; step preparation blocks.
            self.blocked.set()
            await self.release.wait()
        return await super().current(record_target)


async def test_user_stop_interrupts_step_preparation() -> None:
    application = GatedApplication()
    model = ScriptedModel([ScriptedTurn(text="done")])
    control = RunControl()
    runtime = DefaultRuntime(
        model=model,
        invoker=ScriptedInvoker(),
        tools=(),
        application_factory=lambda target: application,
    )
    worker = asyncio.create_task(collect_run(make_request(), control, runtime))
    try:
        await asyncio.wait_for(application.blocked.wait(), timeout=2)
        control.request_user_stop()
        done, _ = await asyncio.wait({worker}, timeout=0.5)
        stopped_before_release = worker in done
    finally:
        application.release.set()
        await asyncio.wait_for(worker, timeout=2)
    assert model.call_count == 0
    assert stopped_before_release, "User stop waited for the blocked application/provider call to return"


class PartialFailureModel:
    def __init__(self) -> None:
        self.calls = 0

    def stream(self, request: ModelRequest, control: RunControl) -> CallbackStream[ModelStepEvent]:
        async def events() -> AsyncIterator[ModelStepEvent]:
            self.calls += 1
            if self.calls == 1:
                yield TextDeltaEvent(identity=IDENTITY, text="partial", attempt=1)
                raise AdapterError("connection failed after partial output")
            yield ModelCompleted(identity=IDENTITY, result=ModelResult(text="retried answer"))

        return CallbackStream(events())


async def test_partial_output_failure_does_not_retry_without_opt_in() -> None:
    model = PartialFailureModel()
    runtime = assemble_default(model=model, invoker=ScriptedInvoker(), tools=())
    events = await collect_run(make_request(), RunControl(), runtime)
    assert model.calls == 1, "Default partial-output policy silently issued another model request"
    assert any(isinstance(event, RunFailed) for event in events)
    assert not any(isinstance(event, RunSucceeded) for event in events)
