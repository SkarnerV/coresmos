"""History projection, contributor merge, and DefaultStepProvider."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from agent_runtime.capabilities.providers import CapabilitySession
from agent_runtime.context.budget import TokenBudgetPolicy
from agent_runtime.context.compress import ToolGroupCompressor, pair_tool_messages
from agent_runtime.contracts import (
    ApplicationSnapshot,
    CapabilitySnapshot,
    ContextContribution,
    ContextVersion,
    Message,
    ModelRequest,
    PreparedStep,
    RecordTarget,
    Role,
    RunRequest,
    StepIdentity,
    TranscriptSnapshot,
)
from agent_runtime.exceptions import ContextBudgetError, VersionConflictError
from agent_runtime.ports import (
    ApplicationStatePort,
    Compressor,
    ContextContributor,
    SummaryProjectionPort,
    TranscriptPort,
)


class MemorySummaryProjection:
    def __init__(self) -> None:
        self._data: dict[str, tuple[Message, ...]] = {}

    async def get(self, record_target: RecordTarget) -> tuple[Message, ...]:
        return self._data.get(record_target.value, ())

    async def put(self, record_target: RecordTarget, messages: tuple[Message, ...]) -> None:
        self._data[record_target.value] = messages

    async def invalidate(self, record_target: RecordTarget) -> None:
        self._data.pop(record_target.value, None)


def merge_contributions(items: Sequence[ContextContribution]) -> tuple[ContextContribution, ...]:
    """Deterministic merge: higher priority wins, then source name; keyed by scope+dedupe_key."""
    chosen: dict[tuple[str, str], ContextContribution] = {}
    for item in items:
        key = (item.scope, item.dedupe_key)
        current = chosen.get(key)
        if current is None or (item.priority, item.source) > (current.priority, current.source):
            chosen[key] = item
    ordered = sorted(chosen.values(), key=lambda item: (-item.priority, item.scope, item.source, item.dedupe_key))
    return tuple(ordered)


@dataclass
class RunView:
    request: RunRequest
    record_target: RecordTarget


class DefaultStepProvider:
    def __init__(
        self,
        *,
        transcript: TranscriptPort,
        capabilities: CapabilitySession,
        application: ApplicationStatePort,
        view: RunView,
        contributors: Sequence[ContextContributor] = (),
        budget: TokenBudgetPolicy | None = None,
        compressor: Compressor | None = None,
        summaries: SummaryProjectionPort | None = None,
        extra_contributions: list[ContextContribution] | None = None,
    ) -> None:
        self._transcript = transcript
        self._capabilities = capabilities
        self._application = application
        self._view = view
        self._contributors = tuple(contributors)
        self._budget = budget or TokenBudgetPolicy(max_prompt_tokens=8_000)
        self._compressor = compressor or ToolGroupCompressor()
        self._summaries = summaries if summaries is not None else MemorySummaryProjection()
        self._extra_contributions = extra_contributions if extra_contributions is not None else []

    async def prepare(self, identity: StepIdentity) -> PreparedStep:
        snapshot, capabilities, application = await self._read_consistent()
        history = pair_tool_messages(snapshot.messages)
        contributed: list[ContextContribution] = list(capabilities.contributions)
        contributed.extend(self._extra_contributions)
        for contributor in self._contributors:
            contributed.extend(
                await contributor.contribute(
                    request=self._view.request,
                    target=self._view.record_target,
                    application=application,
                )
            )
        merged = merge_contributions(contributed)
        extra = tuple(
            Message(role=Role.SYSTEM, content=item.content, name=item.source) for item in merged if item.content
        )
        prompt = extra + history
        available = self._budget.available_for_prompt(capabilities.tools)
        compressed_messages, compressed = await self._compressor.compress(
            prompt,
            budget_tokens=available,
            estimator=self._budget.estimator,
            reserved_tokens=0,
        )
        if compressed:
            await self._summaries.put(self._view.record_target, compressed_messages)
        estimated = self._budget.estimate_messages(compressed_messages)
        if estimated > available:
            raise ContextBudgetError("prepared request exceeds the token budget")
        request = ModelRequest(
            messages=compressed_messages,
            tools=capabilities.tools,
            model=self._view.request.model,
            tool_choice=self._view.request.tool_choice,
            recovery=self._view.request.limits.recovery,
        )
        return PreparedStep(
            identity=identity,
            record_target=self._view.record_target,
            request=request,
            capabilities=capabilities,
            context_version=ContextVersion(
                history_version=snapshot.version,
                capability_version=capabilities.version,
                application_version=application.version,
                target=self._view.record_target,
            ),
        )

    async def _read_consistent(self) -> tuple[TranscriptSnapshot, CapabilitySnapshot, ApplicationSnapshot]:
        last_error: VersionConflictError | None = None
        for _ in range(3):
            target = self._view.record_target
            history = self._transcript.snapshot(target)
            capabilities = self._capabilities.snapshot()
            application = await self._application.current(target)
            history_again = self._transcript.snapshot(target)
            capabilities_again = self._capabilities.snapshot()
            application_again = await self._application.current(target)
            if (
                history.version == history_again.version
                and capabilities.version == capabilities_again.version
                and application.version == application_again.version
                and self._view.record_target.value == target.value
            ):
                return history, capabilities, application
            last_error = VersionConflictError("snapshot versions changed while preparing a step")
        assert last_error is not None
        raise last_error

    def invalidate(self, target: RecordTarget) -> None:
        # Fire-and-forget cache drop; caller awaits if needed.
        self._view.record_target = target


async def invalidate_summary(summaries: SummaryProjectionPort, target: RecordTarget) -> None:
    await summaries.invalidate(target)


def with_target(view: RunView, target: RecordTarget) -> RunView:
    return replace(view, record_target=target)
