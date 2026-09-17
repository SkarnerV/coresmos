from __future__ import annotations

from collections.abc import AsyncIterator

from agent_runtime.contracts import (
    ExecutionLimits,
    Message,
    ModelCompleted,
    ModelEntry,
    ModelRequest,
    ModelResult,
    ModelStepEvent,
    RecordTarget,
    RecoveryBudget,
    Role,
    RunControl,
    RunFailed,
    RunRequest,
    RunSucceeded,
    StepIdentity,
    TextDeltaEvent,
)
from agent_runtime.exceptions import AdapterError
from agent_runtime.lifecycle import CallbackStream
from agent_runtime.runner import assemble_default
from agent_runtime.testing import ScriptedInvoker, collect_run

IDENTITY = StepIdentity("partial", 1, 1)


class PartialFailureModel:
    def __init__(self) -> None:
        self.calls = 0

    def stream(self, request: ModelRequest, control: RunControl) -> CallbackStream[ModelStepEvent]:
        del request, control

        async def events() -> AsyncIterator[ModelStepEvent]:
            self.calls += 1
            if self.calls == 1:
                yield TextDeltaEvent(identity=IDENTITY, text="partial", attempt=1)
                raise AdapterError("connection failed after partial output")
            yield ModelCompleted(identity=IDENTITY, result=ModelResult(text="retried answer"))

        return CallbackStream(events())


def _request(*, retry_partial_output: bool = False) -> RunRequest:
    return RunRequest(
        run_id="partial",
        input_items=(Message(role=Role.USER, content="go"),),
        record_target=RecordTarget("t"),
        entry=ModelEntry(),
        limits=ExecutionLimits(
            recovery=RecoveryBudget(retry_partial_output=retry_partial_output),
        ),
    )


async def test_partial_output_failure_does_not_retry_without_opt_in() -> None:
    model = PartialFailureModel()
    runtime = assemble_default(model=model, invoker=ScriptedInvoker(), tools=())
    events = await collect_run(_request(), RunControl(), runtime)
    assert model.calls == 1
    assert any(isinstance(event, RunFailed) for event in events)
    assert not any(isinstance(event, RunSucceeded) for event in events)


async def test_partial_output_retry_can_be_enabled() -> None:
    model = PartialFailureModel()
    runtime = assemble_default(model=model, invoker=ScriptedInvoker(), tools=())
    events = await collect_run(_request(retry_partial_output=True), RunControl(), runtime)
    assert model.calls == 2
    assert any(isinstance(event, RunSucceeded) for event in events)
