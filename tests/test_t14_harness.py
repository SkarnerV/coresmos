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
)
from agent_runtime.exceptions import ContextBudgetError
from agent_runtime.runner import assemble_default
from agent_runtime.testing.harness import (
    CONTRACT_SCENES,
    default_factory,
    run_contract_suite,
    scene_plain_text,
)
from agent_runtime.testing.models import ScriptedModel, ScriptedTurn
from agent_runtime.testing.scenarios import collect_run
from agent_runtime.testing.tools import ScriptedInvoker


async def test_contract_suite_covers_the_design_rows() -> None:
    names = {scene.name for scene in CONTRACT_SCENES}
    required = {
        "plain_text",
        "tool_then_text",
        "record_failure",
        "wait_and_stop",
        "capability_version_change",
        "message_identity",
        "empty_then_recovered",
        "empty_response_exhausted",
        "trailing_completion_event",
        "duplicate_completion_event",
        "user_stop_during_tool",
        "consumer_close",
        "initial_tools_keep_model_rounds",
        "retry_keeps_model_rounds",
        "budget_stops_before_the_model",
        "runs_are_isolated",
        "observer_failure",
        "target_change_and_finish",
    }
    assert required <= names
    passed = await run_contract_suite()
    assert set(passed) == names


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
