"""Read-only observers. Hosts configure exporters; importing this module is a no-op."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent_runtime.lifecycle import RunScope, await_despite_cancellation
from agent_runtime.ports import Observer

_LOG = logging.getLogger("agent_runtime")
_SENTINEL = object()


@dataclass(frozen=True)
class TimingEvent:
    name: str
    run_id: str
    duration_ms: float
    step_no: int | None = None
    call_id: str | None = None
    attempt: int | None = None
    capability_version: str | None = None


class NoOpObserver:
    async def on_event(self, event: object) -> None:
        del event


class IsolatedObserver:
    """Bounded-queue observer. Failures, blocks, and a full queue never change control flow."""

    def __init__(
        self,
        inner: Observer,
        *,
        maxsize: int = 256,
        offer_timeout: float = 0.05,
    ) -> None:
        self._inner = inner
        self._queue: asyncio.Queue[object] = asyncio.Queue(maxsize=maxsize)
        self._offer_timeout = offer_timeout
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self, scope: RunScope) -> None:
        if self._task is None:
            self._task = scope.create_task(self._pump(), name="observer-pump")

    async def on_event(self, event: object) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            return

    async def aclose(self) -> None:
        self._closed = True
        try:
            self._queue.put_nowait(_SENTINEL)
        except asyncio.QueueFull:
            pass
        if self._task is not None:
            task = self._task
            try:
                await await_despite_cancellation(asyncio.wait_for(asyncio.shield(task), timeout=self._offer_timeout))
            except (TimeoutError, asyncio.CancelledError, Exception):
                task.cancel()
                await await_despite_cancellation(asyncio.gather(task, return_exceptions=True))

    async def _pump(self) -> None:
        while True:
            item = await self._queue.get()
            if item is _SENTINEL:
                return
            try:
                await asyncio.wait_for(self._inner.on_event(item), timeout=self._offer_timeout)
            except Exception:
                _LOG.debug("observer failed", exc_info=True)


class OpenTelemetryObserver:
    """Optional OTel adapter. Depends only on opentelemetry-api; the host configures the SDK."""

    def __init__(self, tracer: Any | None = None) -> None:
        self._tracer = tracer
        self._spans: dict[str, Any] = {}

    async def on_event(self, event: object) -> None:
        tracer = self._tracer
        if tracer is None:
            tracer = _tracer()
        if tracer is None:
            return
        kind = getattr(event, "kind", type(event).__name__)
        run_id = getattr(event, "run_id", None) or getattr(getattr(event, "identity", None), "run_id", None)
        span = tracer.start_span(str(kind))
        if run_id is not None:
            span.set_attribute("run.id", str(run_id))
        identity = getattr(event, "identity", None)
        if identity is not None:
            span.set_attribute("step.no", int(identity.step_no))
            span.set_attribute("model.round", int(identity.model_round))
        if isinstance(event, TimingEvent):
            span.set_attribute("duration.ms", event.duration_ms)
            if event.call_id:
                span.set_attribute("call.id", event.call_id)
            if event.attempt is not None:
                span.set_attribute("attempt", event.attempt)
        span.end()


def _tracer() -> Any | None:
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    return trace.get_tracer("agent_runtime")


def as_observer(observer: Observer) -> Observer:
    return observer


def timed(
    name: str,
    run_id: str,
    *,
    step_no: int | None = None,
    call_id: str | None = None,
    attempt: int | None = None,
    capability_version: str | None = None,
) -> Callable[[], TimingEvent]:
    started = time.perf_counter()

    def finish() -> TimingEvent:
        duration_ms = (time.perf_counter() - started) * 1000
        return TimingEvent(
            name=name,
            run_id=run_id,
            duration_ms=duration_ms,
            step_no=step_no,
            call_id=call_id,
            attempt=attempt,
            capability_version=capability_version,
        )

    return finish
