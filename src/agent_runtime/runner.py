"""Runtime runner: per-run resources, input recording, loop, finalize, and close."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence

from agent_runtime.application import MemoryApplicationState
from agent_runtime.capabilities.providers import BindingRegistry, CapabilitySession, FixedCapabilityProvider
from agent_runtime.context.budget import TokenBudgetPolicy
from agent_runtime.context.manager import DefaultStepProvider, RunView
from agent_runtime.contracts import (
    CompletedEntry,
    RecordTarget,
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
from agent_runtime.exceptions import AgentRuntimeError, CancelledRunError, RecordingError
from agent_runtime.lifecycle import CallbackStream, RunScope, await_despite_cancellation, wait_cancellable
from agent_runtime.observability import IsolatedObserver, NoOpObserver, TimingEvent
from agent_runtime.pipelines.completion import DefaultCompletionPipeline
from agent_runtime.pipelines.model import DefaultModelPipeline
from agent_runtime.pipelines.tools import DefaultToolPipeline
from agent_runtime.ports import (
    ApplicationStatePort,
    CompletionPolicy,
    Compressor,
    ContextContributor,
    ExecutionPorts,
    LowLevelModel,
    ManagedEventStream,
    Observer,
    ResultPolicy,
    ToolInvoker,
    TranscriptPort,
)
from agent_runtime.recording import MemoryTranscript
from agent_runtime.session import RunSession


class DefaultRuntime:
    def __init__(
        self,
        *,
        model: LowLevelModel,
        invoker: ToolInvoker,
        tools: Sequence[ToolSpec],
        bindings: BindingRegistry | None = None,
        idle_timeout: float | None = None,
        completion_policy: CompletionPolicy | None = None,
        result_policy: ResultPolicy | None = None,
        cleanup_timeout: float = 5.0,
        transcript_factory: Callable[[], TranscriptPort] | None = None,
        application_factory: Callable[[RecordTarget], ApplicationStatePort] | None = None,
        contributors: Sequence[ContextContributor] = (),
        compressor: Compressor | None = None,
        observer: Observer | None = None,
        budget: TokenBudgetPolicy | None = None,
    ) -> None:
        self._model = model
        self._invoker = invoker
        self._provider = FixedCapabilityProvider(tools, bindings=bindings)
        self._idle_timeout = idle_timeout
        self._completion_policy = completion_policy
        self._result_policy = result_policy
        self._cleanup_timeout = cleanup_timeout
        self._transcript_factory = transcript_factory or MemoryTranscript
        self._application_factory = application_factory or MemoryApplicationState
        self._contributors = tuple(contributors)
        self._compressor = compressor
        self._observer = observer
        self._budget = budget

    def run(self, request: RunRequest, control: RunControl) -> ManagedEventStream[RuntimeEvent]:
        return CallbackStream(self._run(request, control))

    async def _run(self, request: RunRequest, control: RunControl) -> AsyncIterator[RuntimeEvent]:
        scope = RunScope(control, cleanup_timeout=self._cleanup_timeout)
        transcript: TranscriptPort = self._transcript_factory()
        application: ApplicationStatePort = self._application_factory(request.record_target)
        isolated = IsolatedObserver(self._observer if self._observer is not None else NoOpObserver())
        isolated.start(scope)
        started = time_start()
        snapshot_app = await wait_cancellable(
            application.current(request.record_target),
            control,
            scope,
        )
        initial = (await self._provider.resolve(request, snapshot_app)).snapshot
        if initial is None:
            raise AgentRuntimeError("fixed provider returned no snapshot")
        capabilities = CapabilitySession(initial, self._provider.bindings)
        view = RunView(request=request, record_target=request.record_target)
        session = RunSession(
            request=request,
            control=control,
            scope=scope,
            transcript=transcript,
            capabilities=capabilities,
            application=application,
            view=view,
            idle_timeout=self._idle_timeout,
            observer=isolated,
        )
        ports = ExecutionPorts(
            steps=DefaultStepProvider(
                transcript=transcript,
                capabilities=capabilities,
                application=application,
                view=view,
                contributors=self._contributors,
                budget=self._budget,
                compressor=self._compressor,
                extra_contributions=session.extra_contributions,
            ),
            model=DefaultModelPipeline(self._model, session),
            tools=DefaultToolPipeline(
                self._invoker,
                session,
                self._provider.bindings,
                result_policy=self._result_policy,
            ),
            completion=DefaultCompletionPipeline(session, self._completion_policy),
        )
        terminal: RunStatus | None = None
        yielded_terminal = False
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
                await _finalize(transcript, request.run_id, RunStatus.SUCCEEDED)
                if not control.reason_set(StopReason.CONSUMER_CLOSED):
                    yielded_terminal = True
                    yield RunSucceeded(run_id=request.run_id)
                return
            loop = AgentLoop(ports, scope=scope, limits=request.limits, consumed=request.consumed)
            async for event in loop.run(request):
                if control.reason_set(StopReason.CONSUMER_CLOSED):
                    break
                await _observe(isolated, event)
                yield event
                if isinstance(event, RunWaiting):
                    terminal = RunStatus.WAITING
            if terminal is RunStatus.WAITING:
                await _finalize(transcript, request.run_id, RunStatus.WAITING)
                yielded_terminal = True
                return
            await _finalize(transcript, request.run_id, RunStatus.SUCCEEDED)
            if not control.reason_set(StopReason.CONSUMER_CLOSED):
                yielded_terminal = True
                yield RunSucceeded(run_id=request.run_id)
        except asyncio.CancelledError:
            await await_despite_cancellation(_finalize(transcript, request.run_id, RunStatus.CANCELLED))
            raise
        except CancelledRunError as exc:
            await _finalize(transcript, request.run_id, RunStatus.CANCELLED)
            if not control.reason_set(StopReason.CONSUMER_CLOSED) and not yielded_terminal:
                reason = control.first_reason or StopReason(exc.reason)
                yield RunCancelled(run_id=request.run_id, reason=reason)
        except RecordingError as exc:
            await _finalize(transcript, request.run_id, RunStatus.FAILED)
            if not control.reason_set(StopReason.CONSUMER_CLOSED) and not yielded_terminal:
                yield RunFailed(run_id=request.run_id, error_type=type(exc).__name__, message=str(exc))
        except Exception as exc:
            await _finalize(transcript, request.run_id, RunStatus.FAILED)
            if not control.reason_set(StopReason.CONSUMER_CLOSED) and not yielded_terminal:
                yield RunFailed(run_id=request.run_id, error_type=type(exc).__name__, message=str(exc))
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
                pass


async def _finalize(transcript: TranscriptPort, run_id: str, status: RunStatus) -> None:
    await transcript.finalize_run(run_id=run_id, status=status)


async def _observe(observer: IsolatedObserver | None, event: object) -> None:
    if observer is None:
        return
    await observer.on_event(event)


def time_start() -> float:
    return asyncio.get_running_loop().time()


def time_elapsed_ms(started: float) -> float:
    return (asyncio.get_running_loop().time() - started) * 1000


def assemble_default(
    *,
    model: LowLevelModel,
    invoker: ToolInvoker,
    tools: Sequence[ToolSpec],
    idle_timeout: float | None = None,
    completion_policy: CompletionPolicy | None = None,
    result_policy: ResultPolicy | None = None,
    transcript_factory: Callable[[], TranscriptPort] | None = None,
    application_factory: Callable[[RecordTarget], ApplicationStatePort] | None = None,
    contributors: Sequence[ContextContributor] = (),
    compressor: Compressor | None = None,
    observer: Observer | None = None,
    budget: TokenBudgetPolicy | None = None,
    bindings: BindingRegistry | None = None,
) -> DefaultRuntime:
    return DefaultRuntime(
        model=model,
        invoker=invoker,
        tools=tools,
        bindings=bindings,
        idle_timeout=idle_timeout,
        completion_policy=completion_policy,
        result_policy=result_policy,
        transcript_factory=transcript_factory,
        application_factory=application_factory,
        contributors=contributors,
        compressor=compressor,
        observer=observer,
        budget=budget,
    )
