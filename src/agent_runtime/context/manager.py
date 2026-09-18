"""History projection, contributor merge, summary projection, and DefaultStepProvider."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

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
    PreparedContext,
    PreparedStep,
    RecordTarget,
    Role,
    RunControl,
    RunRequest,
    StepIdentity,
    SummaryProjection,
    TranscriptSnapshot,
)
from agent_runtime.exceptions import ContextBudgetError, VersionConflictError
from agent_runtime.ports import (
    ApplicationStatePort,
    CacheableContributor,
    Compressor,
    ContextContributor,
    SummaryProjectionPort,
    TranscriptPort,
)


class MemorySummaryProjection:
    """Reference projection store. Holds compressed model views, never the original facts."""

    def __init__(self) -> None:
        self._data: dict[str, SummaryProjection] = {}

    async def get(self, record_target: RecordTarget) -> SummaryProjection | None:
        return self._data.get(record_target.value)

    async def put(self, record_target: RecordTarget, projection: SummaryProjection) -> None:
        self._data[record_target.value] = projection

    async def invalidate(self, record_target: RecordTarget) -> None:
        self._data.pop(record_target.value, None)


class StaticContributor:
    """Reference contributor: fixed content, fully cacheable because it reads no external source."""

    def __init__(self, contributions: Sequence[ContextContribution], *, key: str = "static") -> None:
        self._contributions = tuple(contributions)
        self._key = key

    async def contribute(
        self,
        *,
        request: RunRequest,
        target: RecordTarget,
        application: ApplicationSnapshot,
    ) -> tuple[ContextContribution, ...]:
        del request, target, application
        return self._contributions

    def cache_key(
        self,
        *,
        request: RunRequest,
        target: RecordTarget,
        application: ApplicationSnapshot,
    ) -> str | None:
        del request, application
        return f"{self._key}:{target.value}"


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


def contribution_digest(items: Sequence[ContextContribution]) -> str:
    digest = hashlib.sha256()
    for item in items:
        digest.update(f"{item.scope}\0{item.dedupe_key}\0{item.priority}\0{item.source}\0{item.content}\0".encode())
    return digest.hexdigest()[:16]


def message_digest(messages: Sequence[Message]) -> str:
    """Hash every field consumed by the compressor for this prompt."""
    digest = hashlib.sha256()
    for message in messages:
        digest.update(
            repr(
                (
                    message.role,
                    message.content,
                    tuple(
                        (call.call_id, call.name, tuple(sorted(call.arguments.items()))) for call in message.tool_calls
                    ),
                    message.tool_call_id,
                    message.name,
                    message.reasoning,
                    message.message_id,
                )
            ).encode()
        )
        digest.update(b"\0")
    return digest.hexdigest()[:16]


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
        control: RunControl | None = None,
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
        self._control = control
        self._contributor_cache: dict[int, tuple[str, tuple[ContextContribution, ...]]] = {}

    async def prepare(self, identity: StepIdentity) -> PreparedStep:
        snapshot, capabilities, application = await self._read_consistent()
        history = pair_tool_messages(snapshot.messages)
        contributed: list[ContextContribution] = list(capabilities.contributions)
        contributed.extend(self._extra_contributions)
        contributed.extend(await self._collect(application))
        merged = merge_contributions(contributed)
        extra = tuple(
            Message(role=Role.SYSTEM, content=item.content, name=item.source) for item in merged if item.content
        )
        prompt = extra + history
        available = self._budget.available_for_prompt(capabilities.tools)
        key = "|".join(
            (
                snapshot.version,
                capabilities.version,
                application.version,
                contribution_digest(merged),
                message_digest(prompt),
                str(available),
            )
        )
        view_messages, compressed = await self._project(prompt, key, available)
        estimated = self._budget.estimate_messages(view_messages)
        if estimated > available:
            raise ContextBudgetError("prepared request exceeds the token budget")
        request = ModelRequest(
            messages=view_messages,
            tools=capabilities.tools,
            model=self._view.request.model,
            tool_choice=self._view.request.tool_choice,
            recovery=self._view.request.limits.recovery,
            deadline_monotonic=self._control.deadline_monotonic if self._control is not None else None,
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
            estimated_tokens=self._budget.estimate_model_attempt(view_messages, capabilities.tools),
            context=PreparedContext(
                messages=view_messages,
                contributions=merged,
                estimated_tokens=estimated,
                compressed=compressed,
            ),
        )

    async def invalidate_summary(self, target: RecordTarget) -> None:
        """Drop the stored projection for a target after a commit the next step would depend on."""
        await self._summaries.invalidate(target)

    async def _project(self, prompt: tuple[Message, ...], key: str, available: int) -> tuple[tuple[Message, ...], bool]:
        """Reuse a matching stored projection; otherwise compress and store the new one.

        Reuse matters because a summarizing compressor spends a model call. The key covers every
        input to compression, so a stale projection can never be served.
        """
        stored = await self._summaries.get(self._view.record_target)
        if stored is not None and stored.key == key:
            return stored.messages, True
        messages, compressed = await self._compressor.compress(
            prompt,
            budget_tokens=available,
            estimator=self._budget.estimator,
            reserved_tokens=0,
        )
        if compressed:
            await self._summaries.put(self._view.record_target, SummaryProjection(key=key, messages=messages))
        return messages, compressed

    async def _collect(self, application: ApplicationSnapshot) -> list[ContextContribution]:
        """Call each contributor, honoring the scope cache the contributor itself declares."""
        collected: list[ContextContribution] = []
        for index, contributor in enumerate(self._contributors):
            key: str | None = None
            if isinstance(contributor, CacheableContributor):
                key = contributor.cache_key(
                    request=self._view.request,
                    target=self._view.record_target,
                    application=application,
                )
            if key is not None:
                cached = self._contributor_cache.get(index)
                if cached is not None and cached[0] == key:
                    collected.extend(cached[1])
                    continue
            items = await contributor.contribute(
                request=self._view.request,
                target=self._view.record_target,
                application=application,
            )
            if key is not None:
                self._contributor_cache[index] = (key, tuple(items))
            collected.extend(items)
        return collected

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
