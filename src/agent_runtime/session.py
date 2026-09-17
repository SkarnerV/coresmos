"""Per-run mutable coordination. Not a service locator; AgentLoop never receives this object."""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_runtime.capabilities.providers import CapabilitySession
from agent_runtime.context.budget import ExecutionBudgetLedger
from agent_runtime.context.manager import RunView
from agent_runtime.contracts import ContextContribution, ExternalWorkRef, RecordTarget, RunControl, RunRequest
from agent_runtime.lifecycle import RunScope
from agent_runtime.observability import IsolatedObserver
from agent_runtime.ports import ApplicationStatePort, TranscriptPort


@dataclass
class RunSession:
    request: RunRequest
    control: RunControl
    scope: RunScope
    transcript: TranscriptPort
    capabilities: CapabilitySession
    application: ApplicationStatePort
    view: RunView
    idle_timeout: float | None = None
    execution_budget: ExecutionBudgetLedger = field(default_factory=ExecutionBudgetLedger)
    policy_calls: int = 0
    completion_policy_calls: int = 0
    extra: dict[str, object] = field(default_factory=dict)
    extra_contributions: list[ContextContribution] = field(default_factory=list)
    external_work: list[ExternalWorkRef] = field(default_factory=list)
    observer: IsolatedObserver | None = None

    async def observe(self, event: object) -> None:
        if self.observer is not None:
            await self.observer.on_event(event)

    @property
    def record_target(self) -> RecordTarget:
        return self.view.record_target

    @record_target.setter
    def record_target(self, value: RecordTarget) -> None:
        self.view.record_target = value
