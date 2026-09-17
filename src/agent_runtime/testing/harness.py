"""Contract harness that binds factories to shared behavioral scenes. Does not import pytest."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

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
    RuntimeEvent,
    RunWaiting,
    ToolBatchEntry,
    ToolCall,
    ToolSpec,
)
from agent_runtime.recording import MemoryTranscript
from agent_runtime.runner import DefaultRuntime, assemble_default
from agent_runtime.testing.models import ScriptedModel, ScriptedTurn
from agent_runtime.testing.scenarios import ECHO_TOOL, collect_run
from agent_runtime.testing.tools import ScriptedInvoker, ScriptedToolBehavior


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


def default_factory(
    model: ScriptedModel,
    invoker: ScriptedInvoker,
    tools: Sequence[ToolSpec],
    **kwargs: object,
) -> DefaultRuntime:
    return assemble_default(model=model, invoker=invoker, tools=tools, **kwargs)  # type: ignore[arg-type]


def _request(entry: ModelEntry | ToolBatchEntry, run_id: str, target: str) -> RunRequest:
    return RunRequest(
        run_id=run_id,
        input_items=(Message(role=Role.USER, content="go"),),
        record_target=RecordTarget(target),
        entry=entry,
    )


async def scene_plain_text(factory: RuntimeFactory = default_factory) -> SceneResult:
    model = ScriptedModel([ScriptedTurn(text="hello", deltas=("hel", "lo"))])
    invoker = ScriptedInvoker()
    runtime = factory(model, invoker, ())
    events = await collect_run(_request(ModelEntry(), "scene-text", "t1"), RunControl(), runtime)
    return SceneResult(events, model, invoker)


async def scene_tool_then_text(factory: RuntimeFactory = default_factory) -> SceneResult:
    model = ScriptedModel(
        [
            ScriptedTurn(tool_calls=(ToolCall("c1", "echo", {"text": "hi"}),)),
            ScriptedTurn(text="done: hi"),
        ]
    )
    invoker = ScriptedInvoker({"echo": ScriptedToolBehavior(output="hi")})
    runtime = factory(model, invoker, (ECHO_TOOL,))
    events = await collect_run(_request(ModelEntry(), "scene-tool-text", "t1"), RunControl(), runtime)
    return SceneResult(events, model, invoker)


async def scene_tool_updates_context(factory: RuntimeFactory = default_factory) -> SceneResult:
    extra = ToolSpec(
        name="other",
        description="other",
        parameters={"type": "object", "properties": {}},
    )
    model = ScriptedModel(
        [
            ScriptedTurn(tool_calls=(ToolCall("c1", "echo", {"text": "hi"}),)),
            ScriptedTurn(text="after"),
        ]
    )
    invoker = ScriptedInvoker({"echo": ScriptedToolBehavior(output="hi", activate_tools=(extra,))})
    runtime = factory(model, invoker, (ECHO_TOOL,))
    events = await collect_run(_request(ModelEntry(), "scene-cap", "t1"), RunControl(), runtime)
    return SceneResult(events, model, invoker)


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


async def scene_record_failure(factory: RuntimeFactory = default_factory) -> SceneResult:
    transcript = MemoryTranscript()
    transcript.inject_write_failure("scene-fail:input:0")
    model = ScriptedModel([ScriptedTurn(text="nope")])
    invoker = ScriptedInvoker()
    runtime = factory(model, invoker, (ECHO_TOOL,), transcript_factory=lambda: transcript)
    events = await collect_run(_request(ModelEntry(), "scene-fail", "t1"), RunControl(), runtime)
    return SceneResult(events, model, invoker)


def assert_success(result: SceneResult) -> None:
    if not any(isinstance(event, RunSucceeded) for event in result.events):
        raise AssertionError("scene did not succeed")


def assert_failed(result: SceneResult) -> None:
    if not any(isinstance(event, RunFailed) for event in result.events):
        raise AssertionError("scene did not fail as required")


def assert_waiting(result: SceneResult) -> None:
    if not any(isinstance(event, RunWaiting) for event in result.events):
        raise AssertionError("scene did not wait as required")


async def run_bound_scene(
    scene: Callable[[RuntimeFactory], Awaitable[SceneResult]],
    factory: RuntimeFactory = default_factory,
) -> SceneResult:
    return await scene(factory)
