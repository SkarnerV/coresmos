from __future__ import annotations

from agent_runtime.context.budget import TokenBudgetPolicy
from agent_runtime.contracts import (
    Message,
    ModelEntry,
    RecordTarget,
    Role,
    RunControl,
    RunFailed,
    RunRequest,
    RunSucceeded,
    RunWaiting,
)
from agent_runtime.exceptions import ContextBudgetError
from agent_runtime.runner import assemble_default
from agent_runtime.testing.harness import (
    assert_failed,
    assert_success,
    assert_waiting,
    default_factory,
    scene_plain_text,
    scene_record_failure,
    scene_tool_then_text,
    scene_tool_updates_context,
    scene_wait_and_stop,
)
from agent_runtime.testing.models import ScriptedModel, ScriptedTurn
from agent_runtime.testing.scenarios import collect_run
from agent_runtime.testing.tools import ScriptedInvoker


async def test_four_synthetic_scenes_with_default_factory() -> None:
    text = await scene_plain_text()
    assert_success(text)
    assert text.model.call_count == 1

    loop = await scene_tool_then_text()
    assert_success(loop)
    assert loop.model.call_count == 2
    assert loop.invoker.invoke_count == 1

    updated = await scene_tool_updates_context()
    assert_success(updated)
    assert any(spec.name == "other" for spec in updated.model.requests[1].tools)

    waiting = await scene_wait_and_stop()
    assert_waiting(waiting)
    assert waiting.model.call_count == 0
    assert any(isinstance(event, RunWaiting) for event in waiting.events)

    failed = await scene_record_failure()
    assert_failed(failed)
    assert failed.model.call_count == 0
    assert failed.invoker.invoke_count == 0


async def test_factory_binding_can_inject_budget_without_changing_loop() -> None:
    def factory(model: ScriptedModel, invoker: ScriptedInvoker, tools, **kwargs):  # noqa: ANN001, ANN202
        return default_factory(
            model,
            invoker,
            tools,
            budget=TokenBudgetPolicy(max_prompt_tokens=8, reserved_output_tokens=4),
            **kwargs,
        )

    result = await scene_plain_text(factory)
    assert any(isinstance(event, (RunFailed, RunSucceeded)) for event in result.events)


async def test_context_budget_error_is_a_run_failure() -> None:
    runtime = assemble_default(
        model=ScriptedModel([ScriptedTurn(text="nope")]),
        invoker=ScriptedInvoker(),
        tools=(),
        budget=TokenBudgetPolicy(max_prompt_tokens=8, reserved_output_tokens=4),
    )
    events = await collect_run(
        RunRequest(
            run_id="tiny",
            input_items=(Message(role=Role.USER, content="x" * 400),),
            record_target=RecordTarget("t"),
            entry=ModelEntry(),
        ),
        RunControl(),
        runtime,
    )
    assert any(isinstance(event, RunFailed) for event in events)
    failed = next(event for event in events if isinstance(event, RunFailed))
    assert failed.error_type == ContextBudgetError.__name__
