from __future__ import annotations

import pytest

from agent_runtime.contracts import (
    BindingSetRef,
    CapabilitySnapshot,
    CompletionCommand,
    CompletionCompleted,
    ConsumedBudget,
    ContextVersion,
    ExecutionLimits,
    Flow,
    FlowDecision,
    ModelCompleted,
    ModelConfig,
    ModelEntry,
    ModelRequest,
    ModelResult,
    PreparedStep,
    RecordTarget,
    RunControl,
    RunRequest,
    StepIdentity,
    ToolBatchCommand,
    ToolBatchCompleted,
)
from agent_runtime.core import AgentLoop, consume_strict
from agent_runtime.exceptions import CompletionProtocolError
from agent_runtime.lifecycle import CallbackStream, RunScope
from agent_runtime.ports import ExecutionPorts


class _Steps:
    def __init__(self, prepared: PreparedStep) -> None:
        self.prepared = prepared

    async def prepare(self, identity: StepIdentity) -> PreparedStep:
        return self.prepared


class _Model:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.calls = 0

    def stream(self, step: PreparedStep):  # noqa: ANN201
        self.calls += 1

        async def agen():  # noqa: ANN202
            for event in self.events:
                yield event

        return CallbackStream(agen())


class _Tools:
    def __init__(self, decision: FlowDecision | None = None, events: list[object] | None = None) -> None:
        self.calls = 0
        self.decision = decision or FlowDecision(flow=Flow.FINISH)
        self.events = events

    def execute(self, command: ToolBatchCommand):  # noqa: ANN201
        self.calls += 1

        async def agen():  # noqa: ANN202
            if self.events is not None:
                for event in self.events:
                    yield event
                return
            yield ToolBatchCompleted(identity=command.identity, decision=self.decision, results=())

        return CallbackStream(agen())


class _Completion:
    def __init__(self, decision: FlowDecision | None = None, events: list[object] | None = None) -> None:
        self.calls = 0
        self.decision = decision or FlowDecision(flow=Flow.FINISH)
        self.events = events

    def complete(self, command: CompletionCommand):  # noqa: ANN201
        self.calls += 1

        async def agen():  # noqa: ANN202
            if self.events is not None:
                for event in self.events:
                    yield event
                return
            yield CompletionCompleted(identity=command.step.identity, decision=self.decision)

        return CallbackStream(agen())


def _prepared(run_id: str = "r") -> PreparedStep:
    identity = StepIdentity(run_id=run_id, step_no=1, model_round=1)
    target = RecordTarget("t")
    caps = CapabilitySnapshot(version="cap-1", tools=(), binding_ref=BindingSetRef("b"))
    return PreparedStep(
        identity=identity,
        record_target=target,
        request=ModelRequest(messages=(), tools=(), model=ModelConfig()),
        capabilities=caps,
        context_version=ContextVersion("h", "c", "a", target),
    )


async def test_missing_and_trailing_completion_are_rejected() -> None:
    prepared = _prepared()

    async def empty():  # noqa: ANN202
        if False:
            yield None

    with pytest.raises(CompletionProtocolError, match="missing"):
        await consume_strict(
            CallbackStream(empty()), lambda event: isinstance(event, ModelCompleted), RunScope(RunControl())
        )

    async def trailing():  # noqa: ANN202
        yield ModelCompleted(identity=prepared.identity, result=ModelResult(text="ok"))
        yield ModelCompleted(identity=prepared.identity, result=ModelResult(text="again"))

    with pytest.raises(CompletionProtocolError, match="trailing"):
        await consume_strict(
            CallbackStream(trailing()),
            lambda event: isinstance(event, ModelCompleted),
            RunScope(RunControl()),
        )


async def test_error_after_completion_aborts() -> None:
    prepared = _prepared()

    async def boom():  # noqa: ANN202
        yield ModelCompleted(identity=prepared.identity, result=ModelResult(text="ok"))
        raise RuntimeError("late")

    with pytest.raises(CompletionProtocolError, match="error after completion"):
        await consume_strict(
            CallbackStream(boom()), lambda event: isinstance(event, ModelCompleted), RunScope(RunControl())
        )


async def test_finish_does_not_call_model_again() -> None:
    prepared = _prepared()
    model = _Model([ModelCompleted(identity=prepared.identity, result=ModelResult(text="ok"))])
    tools = _Tools()
    completion = _Completion(FlowDecision(flow=Flow.FINISH))
    loop = AgentLoop(
        ExecutionPorts(steps=_Steps(prepared), model=model, tools=tools, completion=completion),
        scope=RunScope(RunControl()),
        limits=ExecutionLimits(max_model_rounds=8, max_steps=8),
        consumed=ConsumedBudget(),
    )
    events = []
    async for event in loop.run(
        RunRequest(run_id="r", input_items=(), record_target=RecordTarget("t"), entry=ModelEntry())
    ):
        events.append(event)
    assert model.calls == 1
    assert tools.calls == 0
    assert completion.calls == 1
