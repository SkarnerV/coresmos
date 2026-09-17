"""Contract harness: behavioral scenes bound to a runtime factory. Does not import pytest.

Every scene runs through the public assembly, so the same suite validates the default
implementation and a host's replacement ports. `run_contract_suite(factory)` is the entry point
an adapter uses to prove it satisfies the runtime contract.

A factory must forward the keyword arguments these scenes pass (`transcript_factory`,
`observer`, `capability_provider`, and so on); a scene that cannot be configured is a scene
that cannot be verified.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from agent_runtime.contracts import (
    ConsumedBudget,
    ExecutionLimits,
    ExternalWorkDeclared,
    Flow,
    Message,
    ModelCompleted,
    ModelEntry,
    PendingRef,
    RecordTarget,
    RecoveryBudget,
    Role,
    RunCancelled,
    RunControl,
    RunFailed,
    RunRequest,
    RunStarted,
    RunSucceeded,
    RuntimeEvent,
    RunWaiting,
    StopReason,
    TextDeltaEvent,
    ToolBatchCompleted,
    ToolBatchEntry,
    ToolCall,
    ToolCallRecordedEvent,
    ToolProgressEvent,
    ToolSpec,
)
from agent_runtime.exceptions import CompletionProtocolError
from agent_runtime.recording import MemoryTranscript
from agent_runtime.runner import DefaultRuntime, assemble_default
from agent_runtime.testing.models import ScriptedModel, ScriptedTurn
from agent_runtime.testing.scenarios import ECHO_TOOL, collect_run
from agent_runtime.testing.tools import ScriptedInvoker, ScriptedToolBehavior

OTHER_TOOL = ToolSpec(
    name="other",
    description="other",
    parameters={"type": "object", "properties": {}},
)


class RuntimeFactory(Protocol):
    def __call__(
        self,
        model: ScriptedModel,
        invoker: ScriptedInvoker,
        tools: Sequence[ToolSpec],
        **kwargs: object,
    ) -> DefaultRuntime: ...


@dataclass
class SceneResult:
    events: list[RuntimeEvent]
    model: ScriptedModel
    invoker: ScriptedInvoker
    transcript: MemoryTranscript | None = None
    control: RunControl | None = None
    observed: list[object] = field(default_factory=list)

    def of_type[T](self, kind: type[T]) -> list[T]:
        return [event for event in self.events if isinstance(event, kind)]

    def first[T](self, kind: type[T]) -> T:
        for event in self.events:
            if isinstance(event, kind):
                return event
        raise AssertionError(f"no {kind.__name__} event in scene")


def default_factory(
    model: ScriptedModel,
    invoker: ScriptedInvoker,
    tools: Sequence[ToolSpec],
    **kwargs: object,
) -> DefaultRuntime:
    return assemble_default(model=model, invoker=invoker, tools=tools, **kwargs)  # type: ignore[arg-type]


def _request(
    entry: ModelEntry | ToolBatchEntry,
    run_id: str,
    target: str,
    *,
    limits: ExecutionLimits | None = None,
    text: str = "go",
) -> RunRequest:
    return RunRequest(
        run_id=run_id,
        input_items=(Message(role=Role.USER, content=text),),
        record_target=RecordTarget(target),
        entry=entry,
        limits=limits or ExecutionLimits(),
    )


# --- scenes -------------------------------------------------------------------------------


async def scene_plain_text(factory: RuntimeFactory = default_factory) -> SceneResult:
    model = ScriptedModel([ScriptedTurn(text="hello", deltas=("hel", "lo"))])
    invoker = ScriptedInvoker()
    runtime = factory(model, invoker, ())
    events = await collect_run(_request(ModelEntry(), "scene-text", "t1"), RunControl(), runtime)
    return SceneResult(events, model, invoker)


def check_plain_text(result: SceneResult) -> None:
    assert_success(result)
    _require(result.model.call_count == 1, "plain text must take exactly one model attempt")
    _require(result.invoker.invoke_count == 0, "plain text must not invoke a tool")
    deltas = result.of_type(TextDeltaEvent)
    _require(bool(deltas), "streamed text must reach the consumer")
    _require(all(event.receipt is not None for event in deltas), "each text delta needs a message receipt")
    completed = result.first(ModelCompleted)
    _require(completed.receipt is not None, "the final text needs a message receipt")
    _require(completed.result.text == "hello", "the final text must be the model's text")


async def scene_tool_then_text(factory: RuntimeFactory = default_factory) -> SceneResult:
    model = ScriptedModel(
        [
            ScriptedTurn(tool_calls=(ToolCall("c1", "echo", {"text": "hi"}),)),
            ScriptedTurn(text="done: hi"),
        ]
    )
    invoker = ScriptedInvoker({"echo": ScriptedToolBehavior(output="hi", progress=({"pct": 50},))})
    runtime = factory(model, invoker, (ECHO_TOOL,))
    events = await collect_run(_request(ModelEntry(), "scene-tool-text", "t1"), RunControl(), runtime)
    return SceneResult(events, model, invoker)


def check_tool_then_text(result: SceneResult) -> None:
    assert_success(result)
    _require(result.model.call_count == 2, "a tool round then a text round is two model attempts")
    _require(result.invoker.invoke_count == 1, "the tool must run exactly once")
    order = [type(event).__name__ for event in result.events]
    _require(
        order.index("ToolCallRecordedEvent") < order.index("ToolResultEvent"),
        "calls must be recorded before their results are reported",
    )
    second = result.model.requests[1]
    _require(
        any(message.role is Role.TOOL for message in second.messages),
        "the next model request must already contain the committed tool result",
    )
    progress = result.of_type(ToolProgressEvent)
    _require(bool(progress), "adapter progress reported during a call must reach the consumer")


async def scene_record_failure(factory: RuntimeFactory = default_factory) -> SceneResult:
    transcript = MemoryTranscript()
    transcript.inject_write_failure("scene-fail:input:0")
    model = ScriptedModel([ScriptedTurn(text="nope")])
    invoker = ScriptedInvoker()
    runtime = factory(model, invoker, (ECHO_TOOL,), transcript_factory=lambda: transcript)
    events = await collect_run(_request(ModelEntry(), "scene-fail", "t1"), RunControl(), runtime)
    return SceneResult(events, model, invoker, transcript=transcript)


def check_record_failure(result: SceneResult) -> None:
    assert_failed(result)
    _require(result.model.call_count == 0, "a failed required record must not reach the model")
    _require(result.invoker.invoke_count == 0, "a failed required record must not run a tool")
    assert result.transcript is not None
    _require(
        result.transcript.snapshot(RecordTarget("t1")).messages == (),
        "a failed record must leave no uncommitted history behind",
    )


async def scene_wait_and_stop(factory: RuntimeFactory = default_factory) -> SceneResult:
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
    runtime = factory(model, invoker, (ECHO_TOOL,))
    events = await collect_run(
        _request(ToolBatchEntry(calls=(ToolCall("c1", "echo", {"text": "x"}),)), "scene-wait", "t1"),
        RunControl(),
        runtime,
    )
    return SceneResult(events, model, invoker)


def check_wait_and_stop(result: SceneResult) -> None:
    assert_waiting(result)
    _require(result.model.call_count == 0, "a waiting run must not call the model")
    waiting = result.first(RunWaiting)
    _require(waiting.pending_ref.value == "wait-1", "the wait must carry the host's pending reference")
    _require(
        waiting.pending_ref.completed_call_ids == ("c1",),
        "the wait must report which calls already executed",
    )


async def scene_capability_version_change(factory: RuntimeFactory = default_factory) -> SceneResult:
    model = ScriptedModel(
        [
            ScriptedTurn(tool_calls=(ToolCall("c1", "echo", {"text": "hi"}),)),
            ScriptedTurn(text="after"),
        ]
    )
    invoker = ScriptedInvoker({"echo": ScriptedToolBehavior(output="hi", activate_tools=(OTHER_TOOL,))})
    runtime = factory(model, invoker, (ECHO_TOOL,))
    events = await collect_run(_request(ModelEntry(), "scene-cap", "t1"), RunControl(), runtime)
    return SceneResult(events, model, invoker)


def check_capability_version_change(result: SceneResult) -> None:
    assert_success(result)
    first, second = result.model.requests[0], result.model.requests[1]
    _require(
        {spec.name for spec in first.tools} == {"echo"},
        "the batch stays pinned to the snapshot the model decided against",
    )
    _require(
        any(spec.name == "other" for spec in second.tools),
        "a tool activated during the batch must appear in the next request",
    )
    _require(result.invoker.invoke_count == 1, "activation must not add a call to the current batch")


async def scene_message_identity(factory: RuntimeFactory = default_factory) -> SceneResult:
    transcript = MemoryTranscript()
    model = ScriptedModel(
        [
            ScriptedTurn(tool_calls=(ToolCall("c1", "echo", {"text": "hi"}),)),
            ScriptedTurn(text="second", deltas=("sec", "ond")),
        ]
    )
    invoker = ScriptedInvoker({"echo": ScriptedToolBehavior(output="hi")})
    runtime = factory(model, invoker, (ECHO_TOOL,), transcript_factory=lambda: transcript)
    events = await collect_run(_request(ModelEntry(), "scene-identity", "t1"), RunControl(), runtime)
    return SceneResult(events, model, invoker, transcript=transcript)


def check_message_identity(result: SceneResult) -> None:
    assert_success(result)
    deltas = result.of_type(TextDeltaEvent)
    _require(len(deltas) >= 2, "this scene needs at least two deltas in one step")
    ids = {event.receipt.message_id for event in deltas if event.receipt is not None}
    _require(len(ids) == 1, "deltas in the same step and target must reuse one message")
    created = [event.receipt.created for event in deltas if event.receipt is not None]
    _require(created[0] and not any(created[1:]), "only the first delta creates the message")
    tool_step = result.first(ToolCallRecordedEvent)
    first_completed = result.of_type(ModelCompleted)[0]
    _require(first_completed.receipt is not None, "the tool-calling turn needs a message receipt")
    assert first_completed.receipt is not None
    _require(
        tool_step.receipt.assistant_message_id == first_completed.receipt.message_id,
        "recording calls reuses the assistant message of the same step and target",
    )
    final = result.of_type(ModelCompleted)[-1]
    _require(final.receipt is not None, "the final text needs a receipt")
    assert final.receipt is not None
    _require(
        final.receipt.message_id != tool_step.receipt.assistant_message_id,
        "a new step must not reuse the previous step's message",
    )


async def scene_repeat_run_is_idempotent(factory: RuntimeFactory = default_factory) -> SceneResult:
    transcript = MemoryTranscript()
    model = ScriptedModel([ScriptedTurn(text="once"), ScriptedTurn(text="once")])
    invoker = ScriptedInvoker()
    runtime = factory(model, invoker, (), transcript_factory=lambda: transcript)
    request = _request(ModelEntry(), "scene-repeat", "t1")
    events = await collect_run(request, RunControl(), runtime)
    before = len(transcript.snapshot(RecordTarget("t1")).messages)
    events += await collect_run(request, RunControl(), runtime)
    after = len(transcript.snapshot(RecordTarget("t1")).messages)
    result = SceneResult(events, model, invoker, transcript=transcript)
    result.observed.append({"before": before, "after": after})
    return result


def check_repeat_run_is_idempotent(result: SceneResult) -> None:
    counts = result.observed[0]
    assert isinstance(counts, dict)
    _require(
        counts["before"] == counts["after"],
        "replaying the same logical operations must not create duplicate messages",
    )


async def scene_empty_then_recovered(factory: RuntimeFactory = default_factory) -> SceneResult:
    model = ScriptedModel([ScriptedTurn(empty=True), ScriptedTurn(text="recovered")])
    invoker = ScriptedInvoker()
    runtime = factory(model, invoker, (ECHO_TOOL,))
    limits = ExecutionLimits(max_model_rounds=1, recovery=RecoveryBudget(max_attempts=2, remaining_attempts=2))
    events = await collect_run(_request(ModelEntry(), "scene-empty", "t1", limits=limits), RunControl(), runtime)
    return SceneResult(events, model, invoker)


def check_empty_then_recovered(result: SceneResult) -> None:
    assert_success(result)
    _require(result.model.call_count == 2, "an empty result must be retried inside the same round")
    first, second = result.model.requests[0], result.model.requests[1]
    _require(first.tools == second.tools, "recovery must reuse this request's tool snapshot")
    _require(first.tool_choice == second.tool_choice, "recovery must reuse this request's tool choice")
    _require(first.recovery == second.recovery, "recovery must reuse this request's recovery budget")
    completed = result.of_type(ModelCompleted)
    _require(len(completed) == 1, "only the effective attempt publishes a completion")


async def scene_empty_response_exhausted(factory: RuntimeFactory = default_factory) -> SceneResult:
    model = ScriptedModel([ScriptedTurn(empty=True), ScriptedTurn(empty=True)])
    invoker = ScriptedInvoker()
    runtime = factory(model, invoker, ())
    limits = ExecutionLimits(recovery=RecoveryBudget(max_attempts=2, remaining_attempts=2, backoff_seconds=(0.0,)))
    events = await collect_run(_request(ModelEntry(), "scene-empty-out", "t1", limits=limits), RunControl(), runtime)
    return SceneResult(events, model, invoker)


def check_empty_response_exhausted(result: SceneResult) -> None:
    assert_failed(result)
    _require(not result.of_type(ModelCompleted), "an empty result must never publish a completion")
    _require(result.model.call_count == 2, "recovery must spend its attempts before failing")


async def scene_trailing_completion_event(factory: RuntimeFactory = default_factory) -> SceneResult:
    model = ScriptedModel([ScriptedTurn(text="hi", trailing_after_complete=True)])
    invoker = ScriptedInvoker()
    runtime = factory(model, invoker, ())
    events = await collect_run(_request(ModelEntry(), "scene-trailing", "t1"), RunControl(), runtime)
    return SceneResult(events, model, invoker)


async def scene_duplicate_completion_event(factory: RuntimeFactory = default_factory) -> SceneResult:
    model = ScriptedModel([ScriptedTurn(text="hi", duplicate_complete=True)])
    invoker = ScriptedInvoker()
    runtime = factory(model, invoker, ())
    events = await collect_run(_request(ModelEntry(), "scene-duplicate", "t1"), RunControl(), runtime)
    return SceneResult(events, model, invoker)


def check_completion_protocol_error(result: SceneResult) -> None:
    assert_failed(result)
    failed = result.first(RunFailed)
    _require(
        failed.error_type == CompletionProtocolError.__name__,
        f"a broken completion protocol must be reported as such, got {failed.error_type}",
    )
    _require(result.invoker.invoke_count == 0, "a protocol error must not continue into execution")


async def scene_user_stop_during_tool(factory: RuntimeFactory = default_factory) -> SceneResult:
    control = RunControl()
    invoker = ScriptedInvoker({"echo": ScriptedToolBehavior(block=True, external_handle="job-1")})
    model = ScriptedModel()
    runtime = factory(model, invoker, (ECHO_TOOL,))

    async def stop_once_running() -> None:
        for _ in range(500):
            if invoker.invoke_count > 0:
                break
            await asyncio.sleep(0.002)
        control.request_user_stop()

    stopper = asyncio.create_task(stop_once_running())
    events = await collect_run(
        _request(ToolBatchEntry(calls=(ToolCall("c1", "echo", {"text": "x"}),)), "scene-stop", "t1"),
        control,
        runtime,
    )
    await stopper
    return SceneResult(events, model, invoker, control=control)


def check_user_stop_during_tool(result: SceneResult) -> None:
    cancelled = result.first(RunCancelled)
    _require(
        cancelled.reason is StopReason.USER_STOP,
        f"a user stop must be reported as a user stop, got {cancelled.reason}",
    )
    _require(result.invoker.invoke_count == 1, "the scene must actually reach the blocking call")
    _require(
        any(work.handle == "job-1" for work in cancelled.external_work),
        "external work the adapter declared must be reported for the host to reconcile",
    )
    _require(
        bool(result.of_type(ExternalWorkDeclared)),
        "declared external work must also reach the consumer while the run is alive",
    )


async def scene_consumer_close(factory: RuntimeFactory = default_factory) -> SceneResult:
    control = RunControl()
    model = ScriptedModel([ScriptedTurn(text="hi")])
    invoker = ScriptedInvoker()
    runtime = factory(model, invoker, ())
    stream = runtime.run(_request(ModelEntry(), "scene-close", "t1"), control)
    events: list[RuntimeEvent] = []
    async for event in stream:
        events.append(event)
        if isinstance(event, RunStarted):
            break
    await stream.aclose()
    return SceneResult(events, model, invoker, control=control)


def check_consumer_close(result: SceneResult) -> None:
    assert result.control is not None
    _require(
        result.control.reason_set(StopReason.CONSUMER_CLOSED),
        "closing the consumer stream must be recorded as a consumer close",
    )
    _require(
        result.control.first_reason is StopReason.CONSUMER_CLOSED,
        "a disconnect must never be reported as a user stop",
    )
    _require(
        not result.of_type(RunSucceeded) and not result.of_type(RunFailed),
        "a closed consumer must not receive a terminal event",
    )


async def scene_initial_tools_keep_model_rounds(factory: RuntimeFactory = default_factory) -> SceneResult:
    model = ScriptedModel([ScriptedTurn(text="after tools")])
    invoker = ScriptedInvoker({"echo": ScriptedToolBehavior(output="hi")})
    runtime = factory(model, invoker, (ECHO_TOOL,))
    events = await collect_run(
        _request(
            ToolBatchEntry(calls=(ToolCall("c1", "echo", {"text": "x"}),)),
            "scene-initial-tools",
            "t1",
            limits=ExecutionLimits(max_model_rounds=1),
        ),
        RunControl(),
        runtime,
    )
    return SceneResult(events, model, invoker)


def check_initial_tools_keep_model_rounds(result: SceneResult) -> None:
    assert_success(result)
    _require(result.invoker.invoke_count == 1, "the entry batch must run")
    _require(
        result.model.call_count == 1,
        "an entry tool batch must leave the single model round available",
    )


async def scene_retry_keeps_model_rounds(factory: RuntimeFactory = default_factory) -> SceneResult:
    model = ScriptedModel([ScriptedTurn(empty=True), ScriptedTurn(text="second try")])
    invoker = ScriptedInvoker()
    runtime = factory(model, invoker, ())
    limits = ExecutionLimits(
        max_model_rounds=1,
        recovery=RecoveryBudget(max_attempts=3, remaining_attempts=3, backoff_seconds=(0.0,)),
    )
    events = await collect_run(_request(ModelEntry(), "scene-retry", "t1", limits=limits), RunControl(), runtime)
    return SceneResult(events, model, invoker)


def check_retry_keeps_model_rounds(result: SceneResult) -> None:
    assert_success(result)
    _require(result.model.call_count == 2, "the scene must actually retry")


async def scene_budget_stops_before_the_model(factory: RuntimeFactory = default_factory) -> SceneResult:
    model = ScriptedModel([ScriptedTurn(text="never")])
    invoker = ScriptedInvoker()
    runtime = factory(model, invoker, ())
    limits = ExecutionLimits(max_estimated_tokens=1)
    events = await collect_run(
        _request(ModelEntry(), "scene-budget", "t1", limits=limits),
        RunControl(),
        runtime,
    )
    result = SceneResult(events, model, invoker)
    result.observed.append({"consumed": ConsumedBudget()})
    return result


def check_budget_stops_before_the_model(result: SceneResult) -> None:
    assert_failed(result)
    _require(result.model.call_count == 0, "an exhausted token budget must not reach the model")


async def scene_runs_are_isolated(factory: RuntimeFactory = default_factory) -> SceneResult:
    transcript = MemoryTranscript()
    model = ScriptedModel([ScriptedTurn(text="one"), ScriptedTurn(text="two")])
    invoker = ScriptedInvoker()
    runtime = factory(model, invoker, (), transcript_factory=lambda: transcript)
    events = await collect_run(
        _request(ModelEntry(), "scene-iso-a", "target-a", text="alpha"),
        RunControl(),
        runtime,
    )
    events += await collect_run(
        _request(ModelEntry(), "scene-iso-b", "target-b", text="beta"),
        RunControl(),
        runtime,
    )
    return SceneResult(events, model, invoker, transcript=transcript)


def check_runs_are_isolated(result: SceneResult) -> None:
    _require(len(result.of_type(RunSucceeded)) == 2, "both runs must succeed")
    second = result.model.requests[1]
    contents = [message.content for message in second.messages]
    _require("beta" in contents, "the second run must see its own input")
    _require("alpha" not in contents, "a run must not read another target's history")
    assert result.transcript is not None
    _require(
        all(message.content != "beta" for message in result.transcript.snapshot(RecordTarget("target-a")).messages),
        "records must stay on the target they were written to",
    )


async def scene_observer_failure(factory: RuntimeFactory = default_factory) -> SceneResult:
    seen: list[object] = []

    class Failing:
        async def on_event(self, event: object) -> None:
            seen.append(event)
            raise RuntimeError("observer is broken")

    model = ScriptedModel(
        [
            ScriptedTurn(tool_calls=(ToolCall("c1", "echo", {"text": "hi"}),)),
            ScriptedTurn(text="fine"),
        ]
    )
    invoker = ScriptedInvoker({"echo": ScriptedToolBehavior(output="hi")})
    runtime = factory(model, invoker, (ECHO_TOOL,), observer=Failing())
    events = await collect_run(_request(ModelEntry(), "scene-observer", "t1"), RunControl(), runtime)
    return SceneResult(events, model, invoker, observed=seen)


def check_observer_failure(result: SceneResult) -> None:
    assert_success(result)
    _require(result.invoker.invoke_count == 1, "a failing observer must not cause a tool to run twice")
    _require(result.model.call_count == 2, "a failing observer must not change the model sequence")


async def scene_target_change_and_finish(factory: RuntimeFactory = default_factory) -> SceneResult:
    transcript = MemoryTranscript()
    invoker = ScriptedInvoker(
        {
            "echo": ScriptedToolBehavior(
                output="moved",
                flow_hint=Flow.FINISH,
                next_target=RecordTarget("t2"),
            )
        }
    )
    model = ScriptedModel()
    runtime = factory(model, invoker, (ECHO_TOOL,), transcript_factory=lambda: transcript)
    events = await collect_run(
        _request(ToolBatchEntry(calls=(ToolCall("c1", "echo", {"text": "x"}),)), "scene-target", "t1"),
        RunControl(),
        runtime,
    )
    return SceneResult(events, model, invoker, transcript=transcript)


def check_target_change_and_finish(result: SceneResult) -> None:
    assert_success(result)
    _require(result.model.call_count == 0, "a finishing batch must not call the model")
    assert result.transcript is not None
    origin = result.transcript.snapshot(RecordTarget("t1")).messages
    moved = result.transcript.snapshot(RecordTarget("t2")).messages
    assistant = [message for message in origin if message.role is Role.ASSISTANT and message.tool_calls]
    tools = [message for message in origin if message.role is Role.TOOL]
    _require(len(assistant) == 1 and len(tools) == 1, "the call and its result stay on the deciding target")
    _require(
        tools[0].tool_call_id == assistant[0].tool_calls[0].call_id,
        "the result must stay associated with its call",
    )
    _require(moved == (), "the new target must not inherit the previous target's messages")
    batch = result.first(ToolBatchCompleted)
    _require(batch.decision.flow is Flow.FINISH, "the batch decision must be the policy's decision")


async def scene_target_change_then_continue(factory: RuntimeFactory = default_factory) -> SceneResult:
    transcript = MemoryTranscript()
    invoker = ScriptedInvoker({"echo": ScriptedToolBehavior(output="moved", next_target=RecordTarget("t2"))})
    model = ScriptedModel([ScriptedTurn(text="on the new target")])
    runtime = factory(model, invoker, (ECHO_TOOL,), transcript_factory=lambda: transcript)
    events = await collect_run(
        _request(ToolBatchEntry(calls=(ToolCall("c1", "echo", {"text": "x"}),)), "scene-target-continue", "t1"),
        RunControl(),
        runtime,
    )
    return SceneResult(events, model, invoker, transcript=transcript)


def check_target_change_then_continue(result: SceneResult) -> None:
    assert_success(result)
    _require(result.model.call_count == 1, "the run must continue to one model round")
    request = result.model.requests[0]
    _require(
        not any(message.role is Role.TOOL for message in request.messages),
        "after a target switch the next request reads the new target only; targets never merge",
    )
    assert result.transcript is not None
    _require(
        bool(result.transcript.snapshot(RecordTarget("t2")).messages),
        "the continued step must record onto the new target",
    )


# --- assertions and suite -----------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def assert_success(result: SceneResult) -> None:
    if not any(isinstance(event, RunSucceeded) for event in result.events):
        raise AssertionError("scene did not succeed")


def assert_failed(result: SceneResult) -> None:
    if not any(isinstance(event, RunFailed) for event in result.events):
        raise AssertionError("scene did not fail as required")


def assert_waiting(result: SceneResult) -> None:
    if not any(isinstance(event, RunWaiting) for event in result.events):
        raise AssertionError("scene did not wait as required")


@dataclass(frozen=True)
class ContractScene:
    """One design contract row: how to run it and what the result must show."""

    name: str
    requirement: str
    run: Callable[[RuntimeFactory], Awaitable[SceneResult]]
    check: Callable[[SceneResult], None]


CONTRACT_SCENES: tuple[ContractScene, ...] = (
    ContractScene("plain_text", "valid receipts, final text, success", scene_plain_text, check_plain_text),
    ContractScene(
        "tool_then_text",
        "the next model request happens only after calls and results are committed",
        scene_tool_then_text,
        check_tool_then_text,
    ),
    ContractScene(
        "record_failure",
        "zero tool calls and no uncommitted history",
        scene_record_failure,
        check_record_failure,
    ),
    ContractScene(
        "wait_and_stop",
        "no extra model call; the wait reference and executed calls are correct",
        scene_wait_and_stop,
        check_wait_and_stop,
    ),
    ContractScene(
        "capability_version_change",
        "the batch keeps the old version; the next request uses the new one",
        scene_capability_version_change,
        check_capability_version_change,
    ),
    ContractScene(
        "message_identity",
        "same step and target reuse one message; a new step is isolated",
        scene_message_identity,
        check_message_identity,
    ),
    ContractScene(
        "repeat_run_is_idempotent",
        "repeating a logical operation does not create a second message",
        scene_repeat_run_is_idempotent,
        check_repeat_run_is_idempotent,
    ),
    ContractScene(
        "empty_then_recovered",
        "recovery keeps request tools, tool choice and budget",
        scene_empty_then_recovered,
        check_empty_then_recovered,
    ),
    ContractScene(
        "empty_response_exhausted",
        "a still-empty recovery publishes no completion",
        scene_empty_response_exhausted,
        check_empty_response_exhausted,
    ),
    ContractScene(
        "trailing_completion_event",
        "an event after completion is detected and stops execution",
        scene_trailing_completion_event,
        check_completion_protocol_error,
    ),
    ContractScene(
        "duplicate_completion_event",
        "a second completion event is detected and stops execution",
        scene_duplicate_completion_event,
        check_completion_protocol_error,
    ),
    ContractScene(
        "user_stop_during_tool",
        "a stop interrupts the tool wait and reports declared external work",
        scene_user_stop_during_tool,
        check_user_stop_during_tool,
    ),
    ContractScene(
        "consumer_close",
        "a disconnect is its own reason and yields no terminal event",
        scene_consumer_close,
        check_consumer_close,
    ),
    ContractScene(
        "initial_tools_keep_model_rounds",
        "an entry tool batch does not consume a model round",
        scene_initial_tools_keep_model_rounds,
        check_initial_tools_keep_model_rounds,
    ),
    ContractScene(
        "retry_keeps_model_rounds",
        "a retry does not consume an extra model round",
        scene_retry_keeps_model_rounds,
        check_retry_keeps_model_rounds,
    ),
    ContractScene(
        "budget_stops_before_the_model",
        "an exhausted token budget stops before a request is made",
        scene_budget_stops_before_the_model,
        check_budget_stops_before_the_model,
    ),
    ContractScene(
        "runs_are_isolated",
        "two runs do not pollute each other's target, history or model",
        scene_runs_are_isolated,
        check_runs_are_isolated,
    ),
    ContractScene(
        "observer_failure",
        "a broken observer replays nothing and changes no result",
        scene_observer_failure,
        check_observer_failure,
    ),
    ContractScene(
        "target_change_and_finish",
        "a tool changes the record target and terminates with correct associations",
        scene_target_change_and_finish,
        check_target_change_and_finish,
    ),
    ContractScene(
        "target_change_then_continue",
        "after a target switch the next request reads only the new target",
        scene_target_change_then_continue,
        check_target_change_then_continue,
    ),
)


async def run_contract_scene(scene: ContractScene, factory: RuntimeFactory = default_factory) -> SceneResult:
    result = await scene.run(factory)
    scene.check(result)
    return result


async def run_contract_suite(
    factory: RuntimeFactory = default_factory,
    *,
    only: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Run every contract scene against a factory. The first failing scene raises AssertionError."""
    selected = [scene for scene in CONTRACT_SCENES if only is None or scene.name in set(only)]
    if only is not None and len(selected) != len(set(only)):
        unknown = set(only) - {scene.name for scene in CONTRACT_SCENES}
        raise AssertionError(f"unknown contract scenes: {sorted(unknown)}")
    passed: list[str] = []
    for scene in selected:
        try:
            await run_contract_scene(scene, factory)
        except AssertionError as exc:
            raise AssertionError(f"contract scene {scene.name} ({scene.requirement}): {exc}") from exc
        passed.append(scene.name)
    return tuple(passed)


async def run_bound_scene(
    scene: Callable[[RuntimeFactory], Awaitable[SceneResult]],
    factory: RuntimeFactory = default_factory,
) -> SceneResult:
    return await scene(factory)


scene_tool_updates_context = scene_capability_version_change
