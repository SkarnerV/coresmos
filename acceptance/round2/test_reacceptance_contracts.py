"""Second acceptance: repaired paths, new adapter, and terminal lifecycle boundaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from agent_runtime.adapters.openai import OpenAIChatCompletionsModel, request_payload
from agent_runtime.application import MemoryApplicationState
from agent_runtime.contracts import (
    ApplicationSnapshot,
    ConsumedBudget,
    ExecutionLimits,
    Flow,
    Message,
    ModelConfig,
    ModelEntry,
    ModelRequest,
    PendingRef,
    RecordTarget,
    RecoveryBudget,
    Role,
    RunControl,
    RunFailed,
    RunRequest,
    RunStatus,
    RunSucceeded,
    RunWaiting,
    ToolBatchEntry,
    ToolCall,
    ToolResult,
)
from agent_runtime.exceptions import IdempotencyError, ReceiptMismatchError
from agent_runtime.recording import MemoryTranscript
from agent_runtime.runner import assemble_default
from agent_runtime.testing import ScriptedInvoker, ScriptedModel, ScriptedToolBehavior, ScriptedTurn, collect_run
from agent_runtime.testing.scenarios import ECHO_TOOL

TARGET = RecordTarget("round2-target")
CALL = ToolCall("c1", "echo", {"text": "hello"})


def run_request(**kwargs: object) -> RunRequest:
    return RunRequest(
        run_id="round2",
        record_target=TARGET,
        input_items=(),
        entry=ModelEntry(),
        **kwargs,  # type: ignore[arg-type]
    )


def test_openai_tool_schema_is_json_serializable() -> None:
    request = ModelRequest(messages=(), tools=(ECHO_TOOL,), model=ModelConfig())
    payload = request_payload(request)
    encoded = json.dumps(payload)
    assert json.loads(encoded)["tools"][0]["function"]["parameters"]["properties"]["text"]["type"] == "string"


def test_openai_history_supports_nested_tool_arguments() -> None:
    call = ToolCall("nested", "tool", {"item": {"id": 1}, "items": [{"id": 2}]})
    request = ModelRequest(
        messages=(Message(role=Role.ASSISTANT, tool_calls=(call,)),),
        tools=(),
        model=ModelConfig(),
    )
    payload = request_payload(request)
    arguments = payload["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(arguments) == {"item": {"id": 1}, "items": [{"id": 2}]}


def chunk(content: str | None, finish: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=None), finish_reason=finish)],
        usage=None,
    )


async def test_openai_timeout_covers_wait_after_first_delta_in_runtime() -> None:
    blocked = asyncio.Event()
    release = asyncio.Event()
    exited = asyncio.Event()

    async def chunks() -> AsyncIterator[object]:
        try:
            yield chunk("hello")
            blocked.set()
            await release.wait()
            yield chunk(None, "stop")
        finally:
            exited.set()

    async def create(**kwargs: object) -> AsyncIterator[object]:
        return chunks()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    model = OpenAIChatCompletionsModel(client, timeout=0.05)
    runtime = assemble_default(model=model, invoker=ScriptedInvoker(), tools=())
    worker = asyncio.create_task(collect_run(run_request(), RunControl(), runtime))
    try:
        await asyncio.wait_for(blocked.wait(), timeout=2)
        done, _ = await asyncio.wait({worker}, timeout=0.3)
        timed_out_without_release = worker in done
    finally:
        release.set()
        events = await asyncio.wait_for(worker, timeout=2)
        await model.aclose()
    assert exited.is_set()
    assert timed_out_without_release, "Adapter timeout expired, but Runtime still waited for the next chunk"
    assert any(isinstance(event, RunFailed) for event in events)


class FailingInitialApplication(MemoryApplicationState):
    async def current(self, record_target: RecordTarget) -> ApplicationSnapshot:
        raise RuntimeError("initial snapshot unavailable")


async def test_initialization_failure_does_not_leak_observer_task() -> None:
    before = asyncio.all_tasks()
    model = ScriptedModel([ScriptedTurn(text="not reached")])
    runtime = assemble_default(
        model=model,
        invoker=ScriptedInvoker(),
        tools=(),
        application_factory=FailingInitialApplication,
    )
    try:
        await collect_run(run_request(), RunControl(), runtime)
    except RuntimeError as exc:
        assert str(exc) == "initial snapshot unavailable"
    leaked = [task for task in asyncio.all_tasks() - before if not task.done()]
    try:
        assert model.call_count == 0
        assert not leaked, f"Initialization left live tasks: {[task.get_name() for task in leaked]}"
    finally:
        for task in leaked:
            task.cancel()
        await asyncio.gather(*leaked, return_exceptions=True)


async def record_pair(transcript: MemoryTranscript, target: RecordTarget, step: int):
    batch = await transcript.record_tool_calls(
        run_id="round2",
        step_no=step,
        record_target=target,
        calls=(CALL,),
        assistant_text=None,
        reasoning=None,
        logical_op_id=f"{step}:calls",
    )
    receipts = await transcript.record_tool_results(
        run_id="round2",
        step_no=step,
        record_target=target,
        results=(ToolResult("c1", "echo", "ok"),),
        logical_op_id=f"{step}:results",
        previous_receipt=batch,
    )
    return batch, receipts


async def test_result_replay_revalidates_run_step_and_target() -> None:
    transcript = MemoryTranscript()
    batch, _ = await record_pair(transcript, TARGET, 1)
    with pytest.raises((ReceiptMismatchError, IdempotencyError)):
        await transcript.record_tool_results(
            run_id="wrong-run",
            step_no=99,
            record_target=RecordTarget("wrong-target"),
            results=(ToolResult("c1", "echo", "ok"),),
            logical_op_id="1:results",
            previous_receipt=batch,
        )


async def test_result_replay_returns_original_message_of_same_step_and_target() -> None:
    transcript = MemoryTranscript()
    _, first = await record_pair(transcript, TARGET, 1)
    second_target = RecordTarget("second-target")
    batch, second = await record_pair(transcript, second_target, 2)
    assert first[0].message_id != second[0].message_id
    replayed = await transcript.record_tool_results(
        run_id="round2",
        step_no=2,
        record_target=second_target,
        results=(ToolResult("c1", "echo", "ok"),),
        logical_op_id="2:results",
        previous_receipt=batch,
    )
    assert replayed[0].message_id == second[0].message_id, "Replay returned another step/target's result message"


async def test_wait_status_commits_before_waiting_event_is_published() -> None:
    transcript = MemoryTranscript()
    model = ScriptedModel()
    invoker = ScriptedInvoker({"echo": ScriptedToolBehavior(flow_hint=Flow.WAIT, pending_ref=PendingRef("pending"))})
    runtime = assemble_default(
        model=model,
        invoker=invoker,
        tools=(ECHO_TOOL,),
        transcript_factory=lambda: transcript,
    )
    request = RunRequest(run_id="round2", record_target=TARGET, input_items=(), entry=ToolBatchEntry(calls=(CALL,)))
    stream = runtime.run(request, RunControl())
    status_at_wait = None
    saw_wait = False
    try:
        async for event in stream:
            if isinstance(event, RunWaiting):
                saw_wait = True
                status_at_wait = transcript.run_status("round2")
                break  # A host hands off and closes after the terminal WAIT event.
    finally:
        await stream.aclose()
    assert saw_wait and invoker.invoke_count == 1 and model.call_count == 0
    assert status_at_wait is RunStatus.WAITING, f"WAIT was published before finalization: {status_at_wait!r}"
    assert transcript.run_status("round2") is RunStatus.WAITING


async def test_observer_receives_run_terminal_event() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.events: list[object] = []

        async def on_event(self, event: object) -> None:
            self.events.append(event)

    observer = Recorder()
    runtime = assemble_default(
        model=ScriptedModel([ScriptedTurn(text="ok")]),
        invoker=ScriptedInvoker(),
        tools=(),
        observer=observer,
    )
    events = await collect_run(run_request(), RunControl(), runtime)
    assert any(isinstance(event, RunSucceeded) for event in events)
    assert any(isinstance(event, RunSucceeded) for event in observer.events), "Observer never received run_succeeded"


@pytest.mark.parametrize(
    ("limits", "consumed"),
    [
        (ExecutionLimits(recovery=RecoveryBudget(max_attempts=0, remaining_attempts=0)), ConsumedBudget()),
        (ExecutionLimits(max_estimated_tokens=10), ConsumedBudget(estimated_tokens=100)),
    ],
    ids=["no-model-attempts-remaining", "restored-token-budget-already-exceeded"],
)
async def test_exhausted_budget_does_not_start_another_model_request(
    limits: ExecutionLimits, consumed: ConsumedBudget
) -> None:
    model = ScriptedModel([ScriptedTurn(text="unexpected")])
    runtime = assemble_default(model=model, invoker=ScriptedInvoker(), tools=())
    events = await collect_run(run_request(limits=limits, consumed=consumed), RunControl(), runtime)
    assert model.call_count == 0, "Runtime made a model request with an exhausted execution budget"
    assert any(isinstance(event, RunFailed) for event in events)
