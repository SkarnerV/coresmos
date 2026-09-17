from __future__ import annotations

import asyncio

from agent_runtime.contracts import (
    CompletedEntry,
    ExecutionLimits,
    Message,
    ModelEntry,
    RecordTarget,
    Role,
    RunCancelled,
    RunControl,
    RunFailed,
    RunRequest,
    RunStarted,
    RunSucceeded,
    StopReason,
    ToolCall,
)
from agent_runtime.pipelines.tools import DefaultToolPipeline
from agent_runtime.recording import MemoryTranscript
from agent_runtime.runner import PortOverrides, assemble_default
from agent_runtime.testing import (
    ScriptedInvoker,
    ScriptedModel,
    ScriptedToolBehavior,
    ScriptedTurn,
    collect_run,
    run_tool_then_text,
)
from agent_runtime.testing.scenarios import ECHO_TOOL as ECHO


async def test_tool_then_text_closed_loop() -> None:
    events = await run_tool_then_text()
    kinds = [getattr(event, "kind", "") for event in events]
    assert "run_started" in kinds
    assert "tool_calls_recorded" in kinds
    assert "tool_result" in kinds
    assert "run_succeeded" in kinds


async def test_completed_entry_skips_model_and_tools() -> None:
    model = ScriptedModel([ScriptedTurn(text="nope")])
    invoker = ScriptedInvoker()
    runtime = assemble_default(model=model, invoker=invoker, tools=(ECHO,))
    events = await collect_run(
        RunRequest(
            run_id="done",
            input_items=(Message(role=Role.USER, content="already done"),),
            record_target=RecordTarget("t"),
            entry=CompletedEntry(),
        ),
        RunControl(),
        runtime,
    )
    assert model.call_count == 0
    assert invoker.invoke_count == 0
    assert any(isinstance(event, RunSucceeded) for event in events)


async def test_input_record_failure_invokes_nothing() -> None:
    transcript = MemoryTranscript()
    transcript.inject_write_failure("blocked:input:0")
    model = ScriptedModel([ScriptedTurn(text="nope")])
    invoker = ScriptedInvoker()
    runtime = assemble_default(
        model=model,
        invoker=invoker,
        tools=(ECHO,),
        transcript_factory=lambda: transcript,
    )
    events = await collect_run(
        RunRequest(
            run_id="blocked",
            input_items=(Message(role=Role.USER, content="x"),),
            record_target=RecordTarget("t"),
            entry=ModelEntry(),
        ),
        RunControl(),
        runtime,
    )
    assert model.call_count == 0
    assert invoker.invoke_count == 0
    assert any(isinstance(event, RunFailed) for event in events)


async def test_finalize_failure_does_not_publish_success() -> None:
    transcript = MemoryTranscript()
    transcript.inject_write_failure("fin:finalize:succeeded")
    runtime = assemble_default(
        model=ScriptedModel([ScriptedTurn(text="ok")]),
        invoker=ScriptedInvoker(),
        tools=(),
        transcript_factory=lambda: transcript,
    )
    events = await collect_run(
        RunRequest(
            run_id="fin",
            input_items=(Message(role=Role.USER, content="x"),),
            record_target=RecordTarget("t"),
            entry=ModelEntry(),
        ),
        RunControl(),
        runtime,
    )
    assert not any(isinstance(event, RunSucceeded) for event in events)
    assert any(isinstance(event, RunFailed) for event in events)


async def test_consumer_close_does_not_keep_yielding() -> None:
    runtime = assemble_default(
        model=ScriptedModel([ScriptedTurn(text="hello", deltas=("h", "e"))]),
        invoker=ScriptedInvoker(),
        tools=(),
    )
    control = RunControl()
    stream = runtime.run(
        RunRequest(
            run_id="c",
            input_items=(Message(role=Role.USER, content="x"),),
            record_target=RecordTarget("t"),
            entry=ModelEntry(),
        ),
        control,
    )
    seen: list[object] = []
    async for event in stream:
        seen.append(event)
        if len(seen) >= 2:
            break
    await stream.aclose()
    assert any(isinstance(event, RunStarted) for event in seen)
    assert not any(isinstance(event, RunSucceeded) for event in seen)
    assert control.reason_set(StopReason.CONSUMER_CLOSED)


async def test_concurrent_runs_are_isolated() -> None:
    runtime_a = assemble_default(
        model=ScriptedModel(
            [ScriptedTurn(tool_calls=(ToolCall("c1", "echo", {"text": "a"}),)), ScriptedTurn(text="A")]
        ),
        invoker=ScriptedInvoker({"echo": ScriptedToolBehavior(output="a")}),
        tools=(ECHO,),
    )
    runtime_b = assemble_default(
        model=ScriptedModel([ScriptedTurn(text="B")]),
        invoker=ScriptedInvoker({"echo": ScriptedToolBehavior(output="should-not-run")}),
        tools=(ECHO,),
    )

    async def run_one(runtime: object, run_id: str, target: str) -> list[object]:
        return await collect_run(
            RunRequest(
                run_id=run_id,
                input_items=(Message(role=Role.USER, content=run_id),),
                record_target=RecordTarget(target),
                entry=ModelEntry(),
            ),
            RunControl(),
            runtime,
        )

    left, right = await asyncio.gather(run_one(runtime_a, "ra", "ta"), run_one(runtime_b, "rb", "tb"))
    assert any(isinstance(event, RunSucceeded) for event in left)
    assert any(isinstance(event, RunSucceeded) for event in right)
    assert any(getattr(event, "kind", "") == "tool_result" for event in left)
    assert not any(getattr(event, "kind", "") == "tool_result" for event in right)


async def test_run_deadline_cancels_a_blocking_tool() -> None:
    runtime = assemble_default(
        model=ScriptedModel(),
        invoker=ScriptedInvoker({"echo": ScriptedToolBehavior(block=True, external_handle="job-9")}),
        tools=(ECHO,),
    )
    from agent_runtime.contracts import ToolBatchEntry

    events = await collect_run(
        RunRequest(
            run_id="deadline",
            input_items=(Message(role=Role.USER, content="x"),),
            record_target=RecordTarget("t"),
            entry=ToolBatchEntry(calls=(ToolCall("c1", "echo", {"text": "x"}),)),
            limits=ExecutionLimits(deadline_seconds=0.05),
        ),
        RunControl(),
        runtime,
    )
    cancelled = next(event for event in events if isinstance(event, RunCancelled))
    assert cancelled.reason is StopReason.DEADLINE
    assert any(work.handle == "job-9" for work in cancelled.external_work)


async def test_port_override_keeps_runner_lifecycle() -> None:
    model = ScriptedModel([ScriptedTurn(text="never")])
    invoker = ScriptedInvoker()
    runtime = assemble_default(
        model=model,
        invoker=invoker,
        tools=(ECHO,),
        ports=PortOverrides(tools=lambda session: DefaultToolPipeline(invoker, session, session.capabilities.bindings)),
    )
    events = await collect_run(
        RunRequest(
            run_id="override",
            input_items=(Message(role=Role.USER, content="hello"),),
            record_target=RecordTarget("t"),
            entry=ModelEntry(),
        ),
        RunControl(),
        runtime,
    )
    assert any(isinstance(event, RunSucceeded) for event in events)
    assert model.call_count == 1
