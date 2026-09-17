from __future__ import annotations

from agent_runtime.application import MemoryApplicationState
from agent_runtime.capabilities import CapabilitySession, FixedCapabilityProvider
from agent_runtime.context import (
    DefaultStepProvider,
    RunView,
    TokenBudgetPolicy,
    merge_contributions,
    pair_tool_messages,
)
from agent_runtime.contracts import (
    ContextContribution,
    Message,
    ModelConfig,
    ModelEntry,
    RecordTarget,
    Role,
    RunRequest,
    StepIdentity,
    ToolCall,
    ToolSpec,
)
from agent_runtime.recording import MemoryTranscript

ECHO = ToolSpec(
    name="echo",
    description="echo",
    parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
)


async def test_merge_is_deterministic_and_deduped() -> None:
    items = (
        ContextContribution("b", "B", "sys", 1, "k"),
        ContextContribution("a", "A", "sys", 2, "k"),
        ContextContribution("c", "C", "user", 1, "k"),
    )
    merged = merge_contributions(items)
    assert [item.source for item in merged] == ["a", "c"]
    assert merge_contributions(items) == merged


def test_tool_calls_pair_with_results() -> None:
    messages = (
        Message(role=Role.USER, content="q"),
        Message(role=Role.ASSISTANT, tool_calls=(ToolCall("c1", "echo", {"text": "hi"}),)),
        Message(role=Role.TOOL, content="hi", tool_call_id="c1", name="echo"),
        Message(role=Role.TOOL, content="orphan", tool_call_id="missing", name="echo"),
    )
    paired = pair_tool_messages(messages)
    assert [msg.tool_call_id for msg in paired if msg.role is Role.TOOL] == ["c1"]


async def test_prepare_is_stable_for_same_inputs() -> None:
    target = RecordTarget("t")
    transcript = MemoryTranscript()
    await transcript.record_input(
        run_id="r",
        record_target=target,
        message=Message(role=Role.USER, content="hello"),
        logical_op_id="in",
    )
    provider = FixedCapabilityProvider((ECHO,))
    app = MemoryApplicationState(target)
    snapshot = (
        await provider.resolve(
            RunRequest(run_id="r", input_items=(), record_target=target, entry=ModelEntry()),
            await app.current(target),
        )
    ).snapshot
    assert snapshot is not None
    session = CapabilitySession(snapshot, provider.bindings)
    request = RunRequest(run_id="r", input_items=(), record_target=target, entry=ModelEntry(), model=ModelConfig())
    view = RunView(request=request, record_target=target)
    provider_step = DefaultStepProvider(
        transcript=transcript,
        capabilities=session,
        application=app,
        view=view,
        budget=TokenBudgetPolicy(max_prompt_tokens=4000, reserved_output_tokens=16),
    )
    first = await provider_step.prepare(StepIdentity("r", 1, 1))
    second = await provider_step.prepare(StepIdentity("r", 1, 1))
    assert first.request.messages == second.request.messages
    assert first.capabilities.version == second.capabilities.version
    assert first.request.tools[0].name == "echo"
