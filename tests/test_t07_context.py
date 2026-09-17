from __future__ import annotations

from agent_runtime.application import MemoryApplicationState
from agent_runtime.capabilities import CapabilitySession, FixedCapabilityProvider
from agent_runtime.context import (
    DefaultStepProvider,
    MemorySummaryProjection,
    ModelSummarizer,
    RunView,
    StaticContributor,
    SummarizingCompressor,
    TokenBudgetPolicy,
    ToolGroupCompressor,
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
from agent_runtime.exceptions import ContextBudgetError
from agent_runtime.recording import MemoryTranscript
from agent_runtime.testing.models import ScriptedModel, ScriptedTurn

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
    assert first.context is not None
    assert first.context.compressed is False


async def test_plain_history_is_trimmed_oldest_first() -> None:
    compressor = ToolGroupCompressor()

    class Estimator:
        def can_estimate(self, text: str) -> bool:
            del text
            return True

        def estimate_text(self, text: str) -> int:
            return len(text)

        def estimate_schema(self, schema: object) -> int:
            del schema
            return 0

    messages = (
        Message(role=Role.USER, content="old" * 20),
        Message(role=Role.ASSISTANT, content="reply" * 20),
        Message(role=Role.USER, content="now"),
    )
    compressed, changed = await compressor.compress(
        messages,
        budget_tokens=20,
        estimator=Estimator(),
        reserved_tokens=0,
    )
    assert changed is True
    assert compressed[0].content == "now"
    assert all(message.role is not Role.ASSISTANT or not message.tool_calls for message in compressed)


async def test_summarizing_compressor_replaces_oldest_units() -> None:
    dropped: list[tuple[Message, ...]] = []

    class Fake:
        async def summarize(self, messages, *, budget_tokens: int) -> str:  # noqa: ANN001
            dropped.append(tuple(messages))
            del budget_tokens
            return "SUMMARY"

    compressor = SummarizingCompressor(Fake(), keep_recent_units=1)
    messages = (
        Message(role=Role.USER, content="one " * 40),
        Message(role=Role.ASSISTANT, content="two " * 40),
        Message(role=Role.USER, content="three"),
    )
    view, changed = await compressor.compress(
        messages,
        budget_tokens=40,
        estimator=_char_estimator(),
        reserved_tokens=0,
    )
    assert changed is True
    assert dropped
    assert view[0].role is Role.SYSTEM
    assert view[0].content == "SUMMARY"
    assert view[-1].content == "three"


async def test_model_summarizer_uses_the_model_port() -> None:
    model = ScriptedModel([ScriptedTurn(text="short")])
    summarizer = ModelSummarizer(model, config=ModelConfig(model="summarizer"))
    text = await summarizer.summarize((Message(role=Role.USER, content="long story"),), budget_tokens=16)
    assert text == "short"
    assert model.call_count == 1
    assert model.requests[0].model.model == "summarizer"


async def test_contributor_cache_skips_repeat_lookups() -> None:
    target = RecordTarget("t")
    transcript = MemoryTranscript()
    await transcript.record_input(
        run_id="r",
        record_target=target,
        message=Message(role=Role.USER, content="hello"),
        logical_op_id="in",
    )
    provider, session, app, view = await _step_parts(transcript, target)
    calls = {"n": 0}

    class Counting(StaticContributor):
        async def contribute(self, *, request, target, application):  # noqa: ANN001, ANN202
            calls["n"] += 1
            return await super().contribute(request=request, target=target, application=application)

    step = DefaultStepProvider(
        transcript=transcript,
        capabilities=session,
        application=app,
        view=view,
        contributors=(Counting((ContextContribution("s", "cached", "sys", 1, "k"),)),),
        budget=TokenBudgetPolicy(max_prompt_tokens=4000, reserved_output_tokens=16),
    )
    await step.prepare(StepIdentity("r", 1, 1))
    await step.prepare(StepIdentity("r", 2, 2))
    assert calls["n"] == 1


async def test_summary_projection_is_reused_for_the_same_key() -> None:
    target = RecordTarget("t")
    transcript = MemoryTranscript()
    for index, text in enumerate(("old " * 40, "mid " * 40, "latest question")):
        await transcript.record_input(
            run_id="r",
            record_target=target,
            message=Message(role=Role.USER, content=text),
            logical_op_id=f"in-{index}",
        )
    provider, session, app, view = await _step_parts(transcript, target)
    compresses = {"n": 0}

    class CountingCompressor(ToolGroupCompressor):
        async def compress(self, messages, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            compresses["n"] += 1
            return await super().compress(messages, **kwargs)

    summaries = MemorySummaryProjection()
    step = DefaultStepProvider(
        transcript=transcript,
        capabilities=session,
        application=app,
        view=view,
        compressor=CountingCompressor(),
        summaries=summaries,
        budget=TokenBudgetPolicy(max_prompt_tokens=80, reserved_output_tokens=8),
    )
    first = await step.prepare(StepIdentity("r", 1, 1))
    second = await step.prepare(StepIdentity("r", 2, 2))
    assert first.context is not None and first.context.compressed is True
    assert second.request.messages == first.request.messages
    assert compresses["n"] == 1
    stored = await summaries.get(target)
    assert stored is not None


async def test_unestimable_reasoning_fails_closed() -> None:
    class RejectReasoning:
        def can_estimate(self, text: str) -> bool:
            return "secret" not in text

        def estimate_text(self, text: str) -> int:
            return max(1, len(text) // 4)

        def estimate_schema(self, schema: object) -> int:
            del schema
            return 1

    policy = TokenBudgetPolicy(max_prompt_tokens=4000, estimator=RejectReasoning())
    try:
        policy.estimate_messages((Message(role=Role.ASSISTANT, reasoning="secret plan"),))
        raised = False
    except ContextBudgetError:
        raised = True
    assert raised


def _char_estimator() -> object:
    class Estimator:
        def can_estimate(self, text: str) -> bool:
            del text
            return True

        def estimate_text(self, text: str) -> int:
            return max(1, len(text))

        def estimate_schema(self, schema: object) -> int:
            del schema
            return 0

    return Estimator()


async def _step_parts(transcript: MemoryTranscript, target: RecordTarget):  # noqa: ANN202
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
    return provider, session, app, view
