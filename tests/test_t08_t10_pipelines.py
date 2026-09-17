from __future__ import annotations

from agent_runtime.contracts import (
    Flow,
    Message,
    ModelEntry,
    PendingRef,
    RecordTarget,
    Role,
    RunControl,
    RunFailed,
    RunRequest,
    RunSucceeded,
    ToolBatchCompleted,
    ToolBatchEntry,
    ToolCall,
    ToolCallRecordedEvent,
    ToolSpec,
)
from agent_runtime.recording import MemoryTranscript
from agent_runtime.runner import assemble_default
from agent_runtime.testing import ScriptedInvoker, ScriptedModel, ScriptedToolBehavior, ScriptedTurn, collect_run

ECHO = ToolSpec(
    name="echo",
    description="echo",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
)
OTHER = ToolSpec(
    name="other",
    description="other",
    parameters={"type": "object", "properties": {}},
)


def _request(entry: ModelEntry | ToolBatchEntry | None = None, run_id: str = "r") -> RunRequest:
    return RunRequest(
        run_id=run_id,
        input_items=(Message(role=Role.USER, content="go"),),
        record_target=RecordTarget("chat"),
        entry=entry if entry is not None else ModelEntry(),
    )


async def test_empty_and_reasoning_only_do_not_complete_until_text() -> None:
    model = ScriptedModel(
        [
            ScriptedTurn(empty=True),
            ScriptedTurn(reasoning="think"),
            ScriptedTurn(text="answer"),
        ]
    )
    runtime = assemble_default(model=model, invoker=ScriptedInvoker(), tools=())
    events = await collect_run(_request(), RunControl(), runtime)
    assert any(isinstance(event, RunSucceeded) for event in events)
    assert model.call_count == 3


async def test_illegal_second_call_does_not_invoke_first() -> None:
    model = ScriptedModel(
        [
            ScriptedTurn(
                tool_calls=(
                    ToolCall("c1", "echo", {"text": "ok"}),
                    ToolCall("c2", "missing", {}),
                )
            )
        ]
    )
    invoker = ScriptedInvoker({"echo": ScriptedToolBehavior(output="ok")})
    runtime = assemble_default(model=model, invoker=invoker, tools=(ECHO,))
    events = await collect_run(_request(), RunControl(), runtime)
    assert invoker.invoke_count == 0
    assert any(isinstance(event, RunFailed) for event in events)


async def test_record_failure_before_invoke_is_a_barrier() -> None:
    transcript = MemoryTranscript()
    transcript.inject_write_failure("r:1:chat:tool_calls")
    model = ScriptedModel([ScriptedTurn(tool_calls=(ToolCall("c1", "echo", {"text": "ok"}),))])
    invoker = ScriptedInvoker()
    runtime = assemble_default(
        model=model,
        invoker=invoker,
        tools=(ECHO,),
        transcript_factory=lambda: transcript,
    )
    events = await collect_run(_request(), RunControl(), runtime)
    assert invoker.invoke_count == 0
    assert any(isinstance(event, RunFailed) for event in events)
    assert not any(isinstance(event, ToolCallRecordedEvent) for event in events)


async def test_new_tool_not_in_snapshot_is_rejected() -> None:
    invoker = ScriptedInvoker()
    runtime = assemble_default(model=ScriptedModel(), invoker=invoker, tools=(ECHO,))
    events = await collect_run(
        _request(ToolBatchEntry(calls=(ToolCall("c1", "other", {}),))),
        RunControl(),
        runtime,
    )
    assert invoker.invoke_count == 0
    assert any(isinstance(event, RunFailed) for event in events)


async def test_wait_carries_pending_ref_and_skips_model() -> None:
    invoker = ScriptedInvoker(
        {
            "echo": ScriptedToolBehavior(
                output="queued",
                flow_hint=Flow.WAIT,
                pending_ref=PendingRef(value="wait-1", completed_call_ids=("c1",)),
            )
        }
    )
    model = ScriptedModel()
    runtime = assemble_default(model=model, invoker=invoker, tools=(ECHO,))
    events = await collect_run(
        _request(ToolBatchEntry(calls=(ToolCall("c1", "echo", {"text": "x"}),))),
        RunControl(),
        runtime,
    )
    assert model.call_count == 0
    waiting = [event for event in events if getattr(event, "kind", "") == "run_waiting"]
    assert waiting and waiting[0].pending_ref.value == "wait-1"  # type: ignore[attr-defined]
    completed = [event for event in events if isinstance(event, ToolBatchCompleted)]
    assert completed and completed[0].decision.flow is Flow.WAIT


async def test_text_submit_failure_skips_completion_policy() -> None:
    from agent_runtime.pipelines.completion import FinishCompletionPolicy
    from agent_runtime.runner import DefaultRuntime

    class CountingPolicy(FinishCompletionPolicy):
        def __init__(self) -> None:
            self.calls = 0

        async def apply(self, command, receipt):  # noqa: ANN001, ANN201
            self.calls += 1
            return await super().apply(command, receipt)

    transcript = MemoryTranscript()
    transcript.inject_write_failure("r:1:chat:a1:final")
    policy = CountingPolicy()
    runtime = DefaultRuntime(
        model=ScriptedModel([ScriptedTurn(text="hello")]),
        invoker=ScriptedInvoker(),
        tools=(),
        completion_policy=policy,
        transcript_factory=lambda: transcript,
    )
    events = await collect_run(_request(), RunControl(), runtime)
    assert policy.calls == 0
    assert any(isinstance(event, RunFailed) for event in events)
