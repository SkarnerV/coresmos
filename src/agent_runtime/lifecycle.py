"""Per-run task registry, cancel sources, idle waits, and bounded cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from typing import Any

from agent_runtime.contracts import RunControl, StopReason
from agent_runtime.exceptions import CancelledRunError, CleanupTimeoutError
from agent_runtime.ports import ManagedEventStream

_STREAM_END = object()


class RunScope:
    """Owns tasks and streams created for a single run."""

    def __init__(self, control: RunControl, *, cleanup_timeout: float = 5.0) -> None:
        self.control = control
        self.cleanup_timeout = cleanup_timeout
        self._tasks: set[asyncio.Task[Any]] = set()
        self._streams: list[Any] = []
        self._close_started = False
        self._closed = asyncio.Event()
        self._cleanup_error: BaseException | None = None

    def register_task[T](self, task: asyncio.Task[T]) -> asyncio.Task[T]:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def create_task[T](self, coro: Coroutine[object, object, T], *, name: str | None = None) -> asyncio.Task[T]:
        task = asyncio.create_task(coro, name=name)
        return self.register_task(task)

    def register_stream[T](self, stream: ManagedEventStream[T]) -> ManagedEventStream[T]:
        self._streams.append(stream)
        return stream

    @property
    def tasks(self) -> frozenset[asyncio.Task[object]]:
        return frozenset(self._tasks)

    async def aclose(self) -> None:
        if self._close_started:
            await self._closed.wait()
            if self._cleanup_error is not None:
                raise self._cleanup_error
            return
        self._close_started = True
        errors: list[BaseException] = []
        timed_out = False
        for stream in list(self._streams):
            closer = getattr(stream, "aclose", None)
            if closer is None:
                continue
            try:
                async with asyncio.timeout(self.cleanup_timeout):
                    await closer()
            except TimeoutError:
                timed_out = True
                errors.append(CleanupTimeoutError("stream aclose timed out"))
            except BaseException as exc:  # noqa: BLE001 - cleanup must continue
                errors.append(exc)
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        pending = [task for task in self._tasks if not task.done()]
        if pending:
            _done, still = await asyncio.wait(pending, timeout=self.cleanup_timeout)
            if still:
                timed_out = True
        still_running = [task for task in self._tasks if not task.done()]
        if timed_out or still_running:
            self._cleanup_error = CleanupTimeoutError(
                f"cleanup timed out with {len(still_running)} task(s) still running"
            )
            self._closed.set()
            raise self._cleanup_error
        if errors:
            wrapped = tuple(exc if isinstance(exc, Exception) else Exception(repr(exc)) for exc in errors)
            self._cleanup_error = ExceptionGroup("one or more resource releases failed", wrapped)
            self._closed.set()
            raise self._cleanup_error
        self._closed.set()


async def await_despite_cancellation[T](awaitable: Awaitable[T]) -> T:
    """Await cleanup work even when the current task is already cancelled (Python 3.11+)."""
    task = asyncio.current_task()
    suppressed = 0
    if task is not None:
        while task.cancelling() > 0:
            task.uncancel()
            suppressed += 1
    try:
        return await awaitable
    finally:
        for _ in range(suppressed):
            if task is not None:
                task.cancel()


async def _cancel_and_wait(*tasks: asyncio.Task[Any]) -> None:
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await await_despite_cancellation(asyncio.gather(*pending, return_exceptions=True))


async def wait_next[T](
    stream: ManagedEventStream[T],
    control: RunControl,
    scope: RunScope,
    *,
    idle_timeout: float | None = None,
) -> T | None:
    """Wait for the next event, a cancel source, or inter-event silence."""

    async def _anext() -> T | object:
        try:
            return await anext(stream)
        except StopAsyncIteration:
            return _STREAM_END

    next_task = scope.create_task(_anext(), name="wait-next-event")
    stop_task = scope.create_task(control.wait(), name="wait-stop")
    try:
        done, pending = await asyncio.wait(
            {next_task, stop_task},
            timeout=idle_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        await _cancel_and_wait(*pending)
    except asyncio.CancelledError:
        await await_despite_cancellation(_cancel_and_wait(next_task, stop_task))
        raise

    if next_task in done:
        result = next_task.result()
        if result is _STREAM_END:
            return None
        return result  # type: ignore[return-value]
    if not done:
        raise CancelledRunError(StopReason.DEADLINE.value)
    reason = stop_task.result() if stop_task in done else control.first_reason
    raise CancelledRunError((reason or StopReason.HOST_CANCEL).value)


async def wait_cancellable[T](
    coro: Coroutine[object, object, T],
    control: RunControl,
    scope: RunScope,
) -> T:
    """Wait for a coroutine, aborting when RunControl is set."""
    raise_if_stopped(control)
    work = scope.create_task(coro)
    stop = scope.create_task(control.wait(), name="wait-cancellable-stop")
    try:
        done, pending = await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        await _cancel_and_wait(*pending)
    except asyncio.CancelledError:
        await await_despite_cancellation(_cancel_and_wait(work, stop))
        raise
    if work in done and not work.cancelled():
        return work.result()
    reason = stop.result() if stop.done() and not stop.cancelled() else control.first_reason
    raise CancelledRunError((reason or StopReason.HOST_CANCEL).value)


async def iterate_cancellable[T](
    stream: ManagedEventStream[T],
    control: RunControl,
    scope: RunScope,
    *,
    idle_timeout: float | None = None,
) -> AsyncIterator[T]:
    try:
        while True:
            item = await wait_next(stream, control, scope, idle_timeout=idle_timeout)
            if item is None:
                return
            yield item
    except asyncio.CancelledError:
        raise
    finally:
        closer = getattr(stream, "aclose", None)
        if closer is not None:
            try:
                await await_despite_cancellation(closer())
            except RuntimeError as exc:
                if "already running" not in str(exc):
                    raise
            except asyncio.CancelledError:
                raise


async def sleep_cancellable(control: RunControl, scope: RunScope, delay: float) -> None:
    if delay <= 0:
        if control.is_set():
            raise CancelledRunError((control.first_reason or StopReason.HOST_CANCEL).value)
        return
    stop_task = scope.create_task(control.wait(), name="backoff-stop")
    sleep_task = scope.create_task(asyncio.sleep(delay), name="backoff-sleep")
    done, pending = await asyncio.wait({stop_task, sleep_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if stop_task in done:
        raise CancelledRunError(stop_task.result().value)


def raise_if_stopped(control: RunControl) -> None:
    reason = control.first_reason
    if reason is not None:
        raise CancelledRunError(reason.value)


class CallbackStream[T]:
    """Async-iterator wrapper that records close and optionally notifies a callback."""

    def __init__(
        self,
        iterator: AsyncIterator[T],
        *,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._iterator = iterator
        self._on_close = on_close
        self._closed = False
        self.close_count = 0

    def __aiter__(self) -> CallbackStream[T]:
        return self

    async def __anext__(self) -> T:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await self._iterator.__anext__()
        except StopAsyncIteration:
            await self.aclose()
            raise
        except asyncio.CancelledError:
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.close_count += 1
        closer = getattr(self._iterator, "aclose", None)
        if closer is not None:
            try:
                await await_despite_cancellation(closer())
            except RuntimeError as exc:
                if "already running" not in str(exc):
                    raise
            except asyncio.CancelledError:
                raise
        if self._on_close is not None:
            self._on_close()

    @property
    def closed(self) -> bool:
        return self._closed
