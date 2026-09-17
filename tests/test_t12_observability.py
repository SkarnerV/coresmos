from __future__ import annotations

import asyncio

from agent_runtime.contracts import (
    Message,
    ModelEntry,
    RecordTarget,
    Role,
    RunControl,
    RunRequest,
    RunSucceeded,
    ToolCall,
)
from agent_runtime.observability import IsolatedObserver, NoOpObserver, OpenTelemetryObserver, TimingEvent
from agent_runtime.runner import assemble_default
from agent_runtime.testing import ScriptedInvoker, ScriptedModel, ScriptedToolBehavior, ScriptedTurn, collect_run
from agent_runtime.testing.scenarios import ECHO_TOOL


class RecordingObserver:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def on_event(self, event: object) -> None:
        self.events.append(event)


class ExplodingObserver:
    def __init__(self) -> None:
        self.calls = 0

    async def on_event(self, event: object) -> None:
        del event
        self.calls += 1
        raise RuntimeError("observer boom")


class BlockingObserver:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def on_event(self, event: object) -> None:
        del event
        self.entered.set()
        await asyncio.sleep(30)


def _request(run_id: str = "obs") -> RunRequest:
    return RunRequest(
        run_id=run_id,
        input_items=(Message(role=Role.USER, content="go"),),
        record_target=RecordTarget("t"),
        entry=ModelEntry(),
    )


async def test_observer_failure_does_not_replay_tools() -> None:
    model = ScriptedModel(
        [
            ScriptedTurn(tool_calls=(ToolCall("c1", "echo", {"text": "hi"}),)),
            ScriptedTurn(text="done"),
        ]
    )
    invoker = ScriptedInvoker({"echo": ScriptedToolBehavior(output="hi")})
    runtime = assemble_default(
        model=model,
        invoker=invoker,
        tools=(ECHO_TOOL,),
        observer=ExplodingObserver(),
    )
    events = await collect_run(_request(), RunControl(), runtime)
    assert any(isinstance(event, RunSucceeded) for event in events)
    assert invoker.invoke_count == 1


async def test_blocking_observer_does_not_hold_the_run() -> None:
    blocker = BlockingObserver()
    runtime = assemble_default(
        model=ScriptedModel([ScriptedTurn(text="ok")]),
        invoker=ScriptedInvoker(),
        tools=(),
        observer=blocker,
    )
    events = await asyncio.wait_for(collect_run(_request("block"), RunControl(), runtime), timeout=2)
    assert any(isinstance(event, RunSucceeded) for event in events)


async def test_timing_events_include_run_and_step_identity() -> None:
    recorder = RecordingObserver()
    runtime = assemble_default(
        model=ScriptedModel([ScriptedTurn(text="ok")]),
        invoker=ScriptedInvoker(),
        tools=(),
        observer=recorder,
    )
    await collect_run(_request("timed"), RunControl(), runtime)
    timings = [event for event in recorder.events if isinstance(event, TimingEvent)]
    assert any(event.name == "run" and event.run_id == "timed" for event in timings)
    assert any(event.name == "model_attempt" and event.step_no == 1 for event in timings)


async def test_isolated_observer_drops_when_queue_is_full() -> None:
    inner = RecordingObserver()
    isolated = IsolatedObserver(inner, maxsize=1, offer_timeout=0.01)
    await isolated.on_event("one")
    await isolated.on_event("two")
    await isolated.on_event("three")

    class _Scope:
        def create_task(self, coro, *, name=None):  # noqa: ANN001, ANN201
            del name
            return asyncio.create_task(coro)

    isolated.start(_Scope())  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    await isolated.aclose()
    assert inner.events == ["one"]


async def test_otel_and_noop_observers_are_import_safe() -> None:
    await NoOpObserver().on_event("x")
    await OpenTelemetryObserver().on_event(TimingEvent(name="run", run_id="r", duration_ms=1.0))
