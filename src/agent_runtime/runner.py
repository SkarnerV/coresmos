"""Runtime runner: per-run resources, input recording, loop, finalize, and close."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass

from agent_runtime.application import MemoryApplicationState
from agent_runtime.capabilities.providers import (
    BindingRegistry,
    BindingSource,
    CapabilitySession,
    FixedCapabilityProvider,
)
from agent_runtime.context.budget import TokenBudgetPolicy, execution_budget
from agent_runtime.context.manager import DefaultStepProvider, MemorySummaryProjection, RunView
from agent_runtime.contracts import (
    CapabilitySnapshot,
    CompletedEntry,
    MatchKind,
    RecordTarget,
    Resolution,
    RunCancelled,
    RunControl,
    RunFailed,
    RunRequest,
    RunStarted,
    RunStatus,
    RunSucceeded,
    RuntimeEvent,
    RunWaiting,
    StopReason,
    ToolSpec,
)
from agent_runtime.core import AgentLoop
from agent_runtime.exceptions import CancelledRunError, CapabilityError, RecordingError
from agent_runtime.lifecycle import CallbackStream, RunScope, await_despite_cancellation, wait_cancellable
from agent_runtime.observability import IsolatedObserver, NoOpObserver, TimingEvent
from agent_runtime.pipelines.completion import DefaultCompletionPipeline
from agent_runtime.pipelines.model import DefaultModelPipeline
from agent_runtime.pipelines.tools import DefaultToolPipeline
from agent_runtime.ports import (
    ApplicationStatePort,
    CapabilityProvider,
    CompletionPolicy,
    CompletionPort,
    Compressor,
    ContextContributor,
    ExecutionPorts,
    LowLevelModel,
    ManagedEventStream,
    ModelStepPort,
    Observer,
    ProjectionInvalidation,
    ResultPolicy,
    StepProvider,
    SummaryProjectionPort,
    ToolBatchPort,
    ToolInvoker,
    TranscriptPort,
)
from agent_runtime.recording import MemoryTranscript
from agent_runtime.session import RunSession

_LOG = logging.getLogger("agent_runtime")


@dataclass(frozen=True)
class PortOverrides:
    """Per-run port factories for hosts that take over a phase's semantics.

    Each factory receives the run's assembly root, so a replacement port reaches the same
    transcript, capability session, scope and control the default pipelines use. Replacing one
    port keeps the rest of the runner: input recording, finalization, observation, cancellation
    and close propagation still happen in one place.
    """

    steps: Callable[[RunSession], StepProvider] | None = None
    model: Callable[[RunSession], ModelStepPort] | None = None
    tools: Callable[[RunSession], ToolBatchPort] | None = None
    completion: Callable[[RunSession], CompletionPort] | None = None


class DefaultRuntime:
    def __init__(
        self,
        *,
        model: LowLevelModel,
        invoker: ToolInvoker,
        tools: Sequence[ToolSpec] = (),
        bindings: BindingRegistry | None = None,
        capability_provider: CapabilityProvider | None = None,
        idle_timeout: float | None = None,
        phase_idle_timeout: float | None = None,
        completion_policy: CompletionPolicy | None = None,
        result_policy: ResultPolicy | None = None,
        cleanup_timeout: float = 5.0,
        transcript_factory: Callable[[], TranscriptPort] | None = None,
        application_factory: Callable[[RecordTarget], ApplicationStatePort] | None = None,
        contributors: Sequence[ContextContributor] = (),
        compressor: Compressor | None = None,
        summaries: SummaryProjectionPort | None = None,
        observer: Observer | None = None,
        budget: TokenBudgetPolicy | None = None,
        ports: PortOverrides | None = None,
    ) -> None:
        self._model = model
        self._invoker = invoker
        self._provider: CapabilityProvider = (
            capability_provider
            if capability_provider is not None
            else FixedCapabilityProvider(tools, bindings=bindings)
        )
        self._bindings = _binding_registry(self._provider, bindings)
        self._idle_timeout = idle_timeout
        self._phase_idle_timeout = phase_idle_timeout
        self._completion_policy = completion_policy
        self._result_policy = result_policy
        self._cleanup_timeout = cleanup_timeout
        self._transcript_factory = transcript_factory or MemoryTranscript
        self._application_factory = application_factory or MemoryApplicationState
        self._contributors = tuple(contributors)
        self._compressor = compressor
        # Projections outlive a single run: they are a cache over committed facts, not run state.
        self._summaries = summaries if summaries is not None else MemorySummaryProjection()
        self._observer = observer
        self._budget = budget
        self._ports = ports or PortOverrides()

    def run(self, request: RunRequest, control: RunControl) -> ManagedEventStream[RuntimeEvent]:
        return CallbackStream(self._run(request, control))

    async def _run(self, request: RunRequest, control: RunControl) -> AsyncIterator[RuntimeEvent]:
        if request.limits.deadline_seconds is not None:
            control.arm_deadline(seconds=request.limits.deadline_seconds)
        scope = RunScope(control, cleanup_timeout=self._cleanup_timeout)
        transcript: TranscriptPort = self._transcript_factory()
        application: ApplicationStatePort = self._application_factory(request.record_target)
        isolated = IsolatedObserver(self._observer if self._observer is not None else NoOpObserver())
        started = time_start()
        yielded_terminal = False
        session: RunSession | None = None
        try:
            isolated.start(scope)
            snapshot_app = await wait_cancellable(
                application.current(request.record_target),
                control,
                scope,
            )
            initial = accept_resolution(await self._provider.resolve(request, snapshot_app))
            capabilities = CapabilitySession(initial, self._bindings)
            view = RunView(request=request, record_target=request.record_target)
            ledger = execution_budget(request.limits, request.consumed)
            session = RunSession(
                request=request,
                control=control,
                scope=scope,
                transcript=transcript,
                capabilities=capabilities,
                application=application,
                view=view,
                idle_timeout=self._idle_timeout,
                execution_budget=ledger,
                observer=isolated,
            )
            ports = self._assemble(session)
            try:
                started_event = RunStarted(run_id=request.run_id, record_target=request.record_target)
                await _observe(isolated, started_event)
                yield started_event
                for index, message in enumerate(request.input_items):
                    await transcript.record_input(
                        run_id=request.run_id,
                        record_target=request.record_target,
                        message=message,
                        logical_op_id=f"{request.run_id}:input:{index}",
                    )
                if isinstance(request.entry, CompletedEntry):
                    event = await _commit_terminal(
                        transcript,
                        isolated,
                        request.run_id,
                        RunStatus.SUCCEEDED,
                        RunSucceeded(run_id=request.run_id),
                    )
                    if not control.reason_set(StopReason.CONSUMER_CLOSED):
                        yielded_terminal = True
                        yield event
                    return
                loop = AgentLoop(
                    ports,
                    scope=scope,
                    limits=request.limits,
                    consumed=request.consumed,
                    budget=ledger,
                    phase_idle_timeout=self._phase_idle_timeout,
                )
                async for event in loop.run(request):
                    if control.reason_set(StopReason.CONSUMER_CLOSED):
                        break
                    if isinstance(event, RunWaiting):
                        waiting = await _commit_terminal(transcript, isolated, request.run_id, RunStatus.WAITING, event)
                        yielded_terminal = True
                        if not control.reason_set(StopReason.CONSUMER_CLOSED):
                            yield waiting
                        return
                    await _observe(isolated, event)
                    yield event
                succeeded = await _commit_terminal(
                    transcript,
                    isolated,
                    request.run_id,
                    RunStatus.SUCCEEDED,
                    RunSucceeded(run_id=request.run_id),
                )
                if not control.reason_set(StopReason.CONSUMER_CLOSED):
                    yielded_terminal = True
                    yield succeeded
            except asyncio.CancelledError:
                await await_despite_cancellation(_finalize(transcript, request.run_id, RunStatus.CANCELLED))
                raise
            except CancelledRunError as exc:
                cancelled = await _commit_terminal(
                    transcript,
                    isolated,
                    request.run_id,
                    RunStatus.CANCELLED,
                    RunCancelled(
                        run_id=request.run_id,
                        reason=control.first_reason or StopReason(exc.reason),
                        external_work=tuple(session.external_work) if session is not None else (),
                    ),
                )
                if not control.reason_set(StopReason.CONSUMER_CLOSED) and not yielded_terminal:
                    yielded_terminal = True
                    yield cancelled
            except RecordingError as exc:
                await _invalidate_projection(ports.steps, session.record_target)
                failed = await _commit_terminal(
                    transcript,
                    isolated,
                    request.run_id,
                    RunStatus.FAILED,
                    RunFailed(run_id=request.run_id, error_type=type(exc).__name__, message=str(exc)),
                )
                if not control.reason_set(StopReason.CONSUMER_CLOSED) and not yielded_terminal:
                    yielded_terminal = True
                    yield failed
            except Exception as exc:
                failed = await _commit_terminal(
                    transcript,
                    isolated,
                    request.run_id,
                    RunStatus.FAILED,
                    RunFailed(run_id=request.run_id, error_type=type(exc).__name__, message=str(exc)),
                )
                if not control.reason_set(StopReason.CONSUMER_CLOSED) and not yielded_terminal:
                    yielded_terminal = True
                    yield failed
        except CancelledRunError as exc:
            cancelled = await _commit_terminal(
                transcript,
                isolated,
                request.run_id,
                RunStatus.CANCELLED,
                RunCancelled(
                    run_id=request.run_id,
                    reason=control.first_reason or StopReason(exc.reason),
                    external_work=tuple(session.external_work) if session is not None else (),
                ),
            )
            if not control.reason_set(StopReason.CONSUMER_CLOSED) and not yielded_terminal:
                yielded_terminal = True
                yield cancelled
        except Exception as exc:
            failed = await _commit_terminal(
                transcript,
                isolated,
                request.run_id,
                RunStatus.FAILED,
                RunFailed(run_id=request.run_id, error_type=type(exc).__name__, message=str(exc)),
            )
            if not control.reason_set(StopReason.CONSUMER_CLOSED) and not yielded_terminal:
                yielded_terminal = True
                yield failed
        finally:
            await await_despite_cancellation(
                _observe(
                    isolated,
                    TimingEvent(name="run", run_id=request.run_id, duration_ms=time_elapsed_ms(started)),
                )
            )
            await await_despite_cancellation(isolated.aclose())
            if not yielded_terminal and not control.reason_set(StopReason.CONSUMER_CLOSED):
                control.notify_consumer_closed()
            try:
                await await_despite_cancellation(scope.aclose())
            except Exception:
                _LOG.debug("run scope cleanup failed", exc_info=True)

    def _assemble(self, session: RunSession) -> ExecutionPorts:
        overrides = self._ports
        steps = (
            overrides.steps(session)
            if overrides.steps is not None
            else DefaultStepProvider(
                transcript=session.transcript,
                capabilities=session.capabilities,
                application=session.application,
                view=session.view,
                contributors=self._contributors,
                budget=self._budget,
                compressor=self._compressor,
                summaries=self._summaries,
                extra_contributions=session.extra_contributions,
                control=session.control,
            )
        )
        return ExecutionPorts(
            steps=steps,
            model=(
                overrides.model(session) if overrides.model is not None else DefaultModelPipeline(self._model, session)
            ),
            tools=(
                overrides.tools(session)
                if overrides.tools is not None
                else DefaultToolPipeline(
                    self._invoker,
                    session,
                    self._bindings,
                    result_policy=self._result_policy,
                )
            ),
            completion=(
                overrides.completion(session)
                if overrides.completion is not None
                else DefaultCompletionPipeline(session, self._completion_policy)
            ),
        )


def accept_resolution(resolution: Resolution) -> CapabilitySnapshot:
    """Require a snapshot. Degraded still runs; the other kinds are failures, not empty sets.

    A no-match, a permission rejection and a configuration error are never quietly turned into
    "this run has no tools", because that would grant a run fewer capabilities than the host
    asked for without telling anyone.
    """
    if resolution.kind in (MatchKind.MATCHED, MatchKind.DEGRADED):
        if resolution.snapshot is None:
            raise CapabilityError(f"{resolution.kind.value} resolution carried no capability snapshot")
        return resolution.snapshot
    raise CapabilityError(f"capability resolution {resolution.kind.value}: {resolution.reason or 'no reason given'}")


def _binding_registry(provider: CapabilityProvider, explicit: BindingRegistry | None) -> BindingRegistry:
    """Bindings must resolve against the registry that published this provider's refs."""
    if explicit is not None:
        return explicit
    if isinstance(provider, BindingSource):
        return provider.bindings
    return BindingRegistry()


async def _invalidate_projection(steps: StepProvider, target: RecordTarget) -> None:
    if not isinstance(steps, ProjectionInvalidation):
        return
    try:
        await steps.invalidate_summary(target)
    except Exception:
        # Invalidation is cleanup; it must not replace the commit failure being reported.
        _LOG.debug("summary invalidation failed", exc_info=True)


async def _finalize(transcript: TranscriptPort, run_id: str, status: RunStatus) -> None:
    await transcript.finalize_run(run_id=run_id, status=status)


async def _observe(observer: IsolatedObserver | None, event: object) -> None:
    if observer is None:
        return
    await observer.on_event(event)


async def _commit_terminal(
    transcript: TranscriptPort,
    observer: IsolatedObserver,
    run_id: str,
    status: RunStatus,
    event: RuntimeEvent,
) -> RuntimeEvent:
    await _finalize(transcript, run_id, status)
    await _observe(observer, event)
    return event


def time_start() -> float:
    return asyncio.get_running_loop().time()


def time_elapsed_ms(started: float) -> float:
    return (asyncio.get_running_loop().time() - started) * 1000


def assemble_default(
    *,
    model: LowLevelModel,
    invoker: ToolInvoker,
    tools: Sequence[ToolSpec] = (),
    capability_provider: CapabilityProvider | None = None,
    idle_timeout: float | None = None,
    phase_idle_timeout: float | None = None,
    completion_policy: CompletionPolicy | None = None,
    result_policy: ResultPolicy | None = None,
    transcript_factory: Callable[[], TranscriptPort] | None = None,
    application_factory: Callable[[RecordTarget], ApplicationStatePort] | None = None,
    contributors: Sequence[ContextContributor] = (),
    compressor: Compressor | None = None,
    summaries: SummaryProjectionPort | None = None,
    observer: Observer | None = None,
    budget: TokenBudgetPolicy | None = None,
    bindings: BindingRegistry | None = None,
    cleanup_timeout: float = 5.0,
    ports: PortOverrides | None = None,
) -> DefaultRuntime:
    """Default assembly. Pass `capability_provider` for a resolver chain, `ports` to take over a phase.

    When a custom provider publishes its own binding refs, pass the same `BindingRegistry` here
    so tool execution resolves against the registry those refs came from.
    """
    return DefaultRuntime(
        model=model,
        invoker=invoker,
        tools=tools,
        bindings=bindings,
        capability_provider=capability_provider,
        idle_timeout=idle_timeout,
        phase_idle_timeout=phase_idle_timeout,
        completion_policy=completion_policy,
        result_policy=result_policy,
        cleanup_timeout=cleanup_timeout,
        transcript_factory=transcript_factory,
        application_factory=application_factory,
        contributors=contributors,
        compressor=compressor,
        summaries=summaries,
        observer=observer,
        budget=budget,
        ports=ports,
    )
