"""Independent acceptance probes for the B01-B07 fixes in 0e4ef2b."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent_runtime.adapters.openai import OpenAIChatCompletionsModel
from agent_runtime.context.budget import TokenBudgetPolicy
from agent_runtime.contracts import (
    CompletedEntry,
    ExecutionLimits,
    Flow,
    Message,
    ModelEntry,
    PendingRef,
    RecordTarget,
    RecoveryBudget,
    Role,
    RunControl,
    RunFailed,
    RunRequest,
    RunStatus,
    ToolBatchEntry,
    ToolCall,
    ToolResult,
)
from agent_runtime.recording import MemoryTranscript
from agent_runtime.runner import assemble_default
from agent_runtime.testing import ScriptedInvoker, ScriptedModel, ScriptedToolBehavior, ScriptedTurn, collect_run
from agent_runtime.testing.scenarios import ECHO_TOOL

TARGET = RecordTarget("round4")


@pytest.mark.parametrize("first", [ScriptedTurn(empty=True), ScriptedTurn(error="retryable")], ids=["empty", "error"])
async def test_recovery_attempt_reserves_its_own_token_budget(first: ScriptedTurn) -> None:
    # 40 characters = 10 prompt tokens, plus one reserved output token.
    # The run budget admits exactly one attempt, even though recovery allows two.
    model = ScriptedModel([first, ScriptedTurn(text="unexpected retry")])
    runtime = assemble_default(
        model=model,
        invoker=ScriptedInvoker(),
        tools=(),
        budget=TokenBudgetPolicy(max_prompt_tokens=100, reserved_output_tokens=1),
    )
    request = RunRequest(
        run_id="round4-budget",
        input_items=(Message(role=Role.USER, content="x" * 40),),
        record_target=TARGET,
        entry=ModelEntry(),
        limits=ExecutionLimits(
            max_estimated_tokens=11,
            recovery=RecoveryBudget(max_attempts=2, remaining_attempts=2, backoff_seconds=()),
        ),
    )
    events = await collect_run(request, RunControl(), runtime)
    assert model.call_count == 1, f"A one-attempt token budget admitted {model.call_count} requests"
    assert any(isinstance(event, RunFailed) and event.error_type == "BudgetExhaustedError" for event in events)


async def test_replay_results_accepts_receipt_returned_by_replayed_calls() -> None:
    transcript = MemoryTranscript()
    call_args = {
        "run_id": "round4-replay",
        "step_no": 1,
        "record_target": TARGET,
        "calls": (ToolCall("c1", "echo", {"text": "hello"}),),
        "assistant_text": None,
        "reasoning": None,
        "logical_op_id": "calls",
    }
    result_args = {
        "run_id": "round4-replay",
        "step_no": 1,
        "record_target": TARGET,
        "results": (ToolResult("c1", "echo", "hello"),),
        "logical_op_id": "results",
    }
    original_batch = await transcript.record_tool_calls(**call_args)
    original_results = await transcript.record_tool_results(**result_args, previous_receipt=original_batch)
    before = transcript.snapshot(TARGET)
    replayed_batch = await transcript.record_tool_calls(**call_args)
    assert original_batch.created and not replayed_batch.created
    replayed_results = await transcript.record_tool_results(**result_args, previous_receipt=replayed_batch)
    assert tuple(item.message_id for item in replayed_results) == tuple(item.message_id for item in original_results)
    assert all(not item.created for item in replayed_results)
    assert transcript.snapshot(TARGET) == before


async def test_adapter_deadline_also_bounds_stream_close() -> None:
    closing = asyncio.Event()
    release = asyncio.Event()

    class SlowCloseStream:
        def __aiter__(self):
            return self.chunks()

        async def chunks(self):
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop")],
                usage=None,
            )

        async def aclose(self) -> None:
            closing.set()
            await release.wait()

    async def create(**kwargs):
        return SlowCloseStream()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    model = OpenAIChatCompletionsModel(client, timeout=0.05)
    runtime = assemble_default(model=model, invoker=ScriptedInvoker(), tools=())
    request = RunRequest(run_id="round4-close", input_items=(), record_target=TARGET, entry=ModelEntry())
    worker = asyncio.create_task(collect_run(request, RunControl(), runtime))
    finished_within_deadline = False
    try:
        await asyncio.wait_for(closing.wait(), timeout=1)
        done, _ = await asyncio.wait({worker}, timeout=0.3)
        finished_within_deadline = worker in done
    finally:
        # Always release the fake resource, including on assertion failure.
        release.set()
        events = await asyncio.wait_for(worker, timeout=2)
        await model.aclose()
    assert finished_within_deadline, "The 50 ms adapter timeout did not bound SDK stream close after 300 ms"
    assert any(isinstance(event, RunFailed) for event in events)


@pytest.mark.parametrize("case", ["success", "failure", "cancel", "wait", "completed-entry", "wait-finalize-failure"])
async def test_terminal_is_committed_and_observed_before_host_handoff(case: str) -> None:
    transcript = MemoryTranscript()
    run_id = f"round4-terminal-{case}"
    waiting = case in {"wait", "wait-finalize-failure"}
    expected = {
        "success": RunStatus.SUCCEEDED,
        "failure": RunStatus.FAILED,
        "cancel": RunStatus.CANCELLED,
        "wait": RunStatus.WAITING,
        "completed-entry": RunStatus.SUCCEEDED,
        "wait-finalize-failure": RunStatus.FAILED,
    }[case]
    observed: list[tuple[str, RunStatus | None]] = []
    terminal_kinds = {"run_succeeded", "run_failed", "run_cancelled", "run_waiting"}

    class Recorder:
        async def on_event(self, event: object) -> None:
            kind = getattr(event, "kind", "")
            if kind in terminal_kinds:
                observed.append((kind, transcript.run_status(run_id)))

    if case == "wait-finalize-failure":
        transcript.inject_write_failure(f"{run_id}:finalize:waiting")
    entry = ModelEntry()
    if waiting:
        entry = ToolBatchEntry(calls=(ToolCall("c1", "echo", {"text": "hi"}),))
    elif case == "completed-entry":
        entry = CompletedEntry()
    model = ScriptedModel([ScriptedTurn(error="failure") if case == "failure" else ScriptedTurn(text="ok")])
    runtime = assemble_default(
        model=model,
        invoker=ScriptedInvoker({"echo": ScriptedToolBehavior(flow_hint=Flow.WAIT, pending_ref=PendingRef("pending"))}),
        tools=(ECHO_TOOL,),
        transcript_factory=lambda: transcript,
        observer=Recorder(),
    )
    request = RunRequest(
        run_id=run_id,
        input_items=(),
        record_target=TARGET,
        entry=entry,
        limits=ExecutionLimits(recovery=RecoveryBudget(max_attempts=1, remaining_attempts=1)),
    )
    control = RunControl()
    stream = runtime.run(request, control)
    terminal = None
    try:
        async for event in stream:
            if case == "cancel" and event.kind == "run_started":
                control.request_user_stop()
            if event.kind in terminal_kinds:
                terminal = event.kind
                assert transcript.run_status(run_id) is expected
                break
    finally:
        await stream.aclose()
    expected_kind = f"run_{expected.value}"
    assert terminal == expected_kind
    assert observed == [(expected_kind, expected)]
    assert transcript.run_status(run_id) is expected
