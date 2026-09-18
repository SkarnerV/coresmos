"""Follow-up acceptance for the local D01-D04 fixes over e21dadf."""

from __future__ import annotations

import asyncio

import pytest

from agent_runtime.capabilities import BindingRegistry, FixedCapabilityProvider, ResolverChain
from agent_runtime.context import TokenBudgetPolicy
from agent_runtime.contracts import (
    InvocationOutcome,
    MatchKind,
    Message,
    ModelEntry,
    RecordTarget,
    Resolution,
    Role,
    RunControl,
    RunRequest,
    RunSucceeded,
    ToolBatchEntry,
    ToolCall,
    ToolInvocation,
    ToolResult,
    ToolSpec,
)
from agent_runtime.exceptions import ReceiptMismatchError
from agent_runtime.recording import MemoryTranscript
from agent_runtime.runner import assemble_default
from agent_runtime.testing import ScriptedInvoker, ScriptedModel, ScriptedTurn, collect_run
from agent_runtime.testing.scenarios import ECHO_TOOL

TARGET = RecordTarget("round6")


class FirstRunProvider(FixedCapabilityProvider):
    async def resolve(self, request, application):
        if request.run_id == "first":
            return await super().resolve(request, application)
        return Resolution(kind=MatchKind.NO_MATCH)


@pytest.mark.parametrize("explicit_registry", [False, True])
async def test_concurrent_provider_selection_keeps_each_runs_binding(explicit_registry: bool) -> None:
    chain = ResolverChain(
        (
            FirstRunProvider((ECHO_TOOL,), invoker_key="first-backend"),
            FixedCapabilityProvider((ECHO_TOOL,), invoker_key="second-backend"),
        ),
        bindings=BindingRegistry() if explicit_registry else None,
    )
    invoked: dict[str, str] = {}

    class Invoker:
        async def invoke(self, invocation: ToolInvocation) -> InvocationOutcome:
            backend = chain.bindings.resolve(invocation.binding, invocation.call.name)
            invoked[invocation.call.call_id] = backend
            return InvocationOutcome(result=ToolResult(invocation.call.call_id, invocation.call.name, backend))

    model = ScriptedModel()
    runtime = assemble_default(model=model, invoker=Invoker(), capability_provider=chain)

    async def run_one(run_id: str):
        request = RunRequest(
            run_id=run_id,
            input_items=(),
            record_target=RecordTarget(run_id),
            entry=ToolBatchEntry(calls=(ToolCall(run_id, "echo", {"text": "hi"}),), continue_after=False),
        )
        return await collect_run(request, RunControl(), runtime)

    results = await asyncio.gather(run_one("first"), run_one("second"))
    assert all(any(isinstance(event, RunSucceeded) for event in events) for events in results)
    assert invoked == {"first": "first-backend", "second": "second-backend"}
    assert model.call_count == 0


async def test_activating_another_tool_preserves_the_selected_backend() -> None:
    chain = ResolverChain(
        (
            FirstRunProvider((ECHO_TOOL,), invoker_key="skipped-backend"),
            FixedCapabilityProvider((ECHO_TOOL,), invoker_key="selected-backend"),
        )
    )
    extra = ToolSpec("other", "another tool", {"type": "object", "properties": {}})
    invoked: list[str] = []

    class Invoker:
        async def invoke(self, invocation: ToolInvocation) -> InvocationOutcome:
            backend = chain.bindings.resolve(invocation.binding, invocation.call.name)
            invoked.append(backend)
            return InvocationOutcome(
                result=ToolResult(invocation.call.call_id, invocation.call.name, backend),
                activate_tools=(extra,) if len(invoked) == 1 else (),
            )

    model = ScriptedModel(
        [
            ScriptedTurn(tool_calls=(ToolCall("c1", "echo", {"text": "first"}),)),
            ScriptedTurn(tool_calls=(ToolCall("c2", "echo", {"text": "second"}),)),
            ScriptedTurn(text="done"),
        ]
    )
    runtime = assemble_default(model=model, invoker=Invoker(), capability_provider=chain)
    request = RunRequest(run_id="activation", input_items=(), record_target=TARGET, entry=ModelEntry())
    events = await collect_run(request, RunControl(), runtime)
    assert any(isinstance(event, RunSucceeded) for event in events)
    assert model.call_count == 3 and len(invoked) == 2
    assert invoked == ["selected-backend", "selected-backend"], f"Activation changed an existing binding: {invoked}"


async def test_projection_checks_content_when_message_ids_are_identical() -> None:
    model = ScriptedModel([ScriptedTurn(text="first answer"), ScriptedTurn(text="second answer")])
    runtime = assemble_default(
        model=model,
        invoker=ScriptedInvoker(),
        budget=TokenBudgetPolicy(max_prompt_tokens=40, reserved_output_tokens=1),
    )
    for text in ("first question", "second question"):
        request = RunRequest(
            run_id="same-id",
            input_items=(Message(role=Role.USER, content="old " * 100), Message(role=Role.USER, content=text)),
            record_target=TARGET,
            entry=ModelEntry(),
        )
        events = await collect_run(request, RunControl(), runtime)
        assert any(isinstance(event, RunSucceeded) for event in events)
    assert model.requests[0].messages[-1].message_id == model.requests[1].messages[-1].message_id
    assert [request.messages[-1].content for request in model.requests] == ["first question", "second question"]


@pytest.mark.parametrize("changed", ["run_id", "step_no", "record_target"])
async def test_unrecorded_batch_rejects_mismatched_submission_identity(changed: str) -> None:
    transcript = MemoryTranscript(unrecorded_tools=("internal",))
    receipt = await transcript.record_tool_calls(
        run_id="receipt",
        step_no=1,
        record_target=TARGET,
        calls=(ToolCall("c1", "internal", {}),),
        assistant_text=None,
        reasoning=None,
        logical_op_id="calls",
    )
    arguments = {"run_id": "receipt", "step_no": 1, "record_target": TARGET}
    arguments[changed] = {"run_id": "other", "step_no": 2, "record_target": RecordTarget("other")}[changed]
    with pytest.raises(ReceiptMismatchError):
        await transcript.record_tool_results(
            **arguments,
            results=(ToolResult("c1", "internal", "ok"),),
            logical_op_id="results",
            previous_receipt=receipt,
        )
    assert transcript.snapshot(TARGET).messages == ()


async def test_unrecorded_batch_replay_remains_valid() -> None:
    transcript = MemoryTranscript(unrecorded_tools=("internal",))
    arguments = {
        "run_id": "receipt",
        "step_no": 1,
        "record_target": TARGET,
        "calls": (ToolCall("c1", "internal", {}),),
        "assistant_text": None,
        "reasoning": None,
        "logical_op_id": "calls",
    }
    for created in (True, False):
        receipt = await transcript.record_tool_calls(**arguments)
        assert receipt.created is created and not receipt.recording_required
        results = await transcript.record_tool_results(
            run_id="receipt",
            step_no=1,
            record_target=TARGET,
            results=(ToolResult("c1", "internal", "ok"),),
            logical_op_id="results",
            previous_receipt=receipt,
        )
        assert results == ()
    assert transcript.snapshot(TARGET).messages == ()
