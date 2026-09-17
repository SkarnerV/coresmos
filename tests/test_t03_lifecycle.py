from __future__ import annotations

import asyncio

from agent_runtime.contracts import RunControl, StopReason
from agent_runtime.exceptions import CleanupTimeoutError
from agent_runtime.lifecycle import RunScope, sleep_cancellable
from agent_runtime.testing.models import ScriptedModel, ScriptedTurn


async def test_two_scopes_cancel_independently() -> None:
    left = RunControl()
    right = RunControl()
    left.request_user_stop()
    assert left.first_reason is StopReason.USER_STOP
    assert right.first_reason is None
    right.request_host_cancel()
    assert right.first_reason is StopReason.HOST_CANCEL
    assert left.reason_set(StopReason.USER_STOP)
    assert not left.reason_set(StopReason.HOST_CANCEL)


async def test_double_close_is_safe() -> None:
    scope = RunScope(RunControl())
    await scope.aclose()
    await scope.aclose()


async def test_one_cleanup_failure_still_closes_other_streams() -> None:
    scope = RunScope(RunControl())
    closed: list[str] = []

    class Boom:
        async def aclose(self) -> None:
            closed.append("boom")
            raise RuntimeError("boom")

    class Ok:
        async def aclose(self) -> None:
            closed.append("ok")

    scope.register_stream(Boom())  # type: ignore[arg-type]
    scope.register_stream(Ok())  # type: ignore[arg-type]
    try:
        await scope.aclose()
    except ExceptionGroup:
        pass
    assert closed == ["boom", "ok"]


async def test_cleanup_timeout_is_not_success() -> None:
    control = RunControl()
    scope = RunScope(control, cleanup_timeout=0.05)
    started = asyncio.Event()
    running = True

    async def never_end() -> None:
        started.set()
        while running:
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()

    scope.create_task(never_end(), name="stuck")
    await started.wait()
    try:
        await scope.aclose()
        raised = False
    except CleanupTimeoutError:
        raised = True
    running = False
    await asyncio.sleep(0.1)
    assert raised


async def test_user_stop_during_silence_closes_model_stream() -> None:
    model = ScriptedModel([ScriptedTurn(silence=True)])
    control = RunControl()
    stream = model.stream(request=_empty_request(), control=control)

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        control.request_user_stop()

    stopper = asyncio.create_task(stop_soon())
    try:
        async for _event in stream:
            raise AssertionError("silence should not yield")
    except Exception:
        pass
    await stream.aclose()
    await stopper
    assert model.close_count >= 1
    assert model.call_count == 1


async def test_backoff_stop_cancels_sleep() -> None:
    control = RunControl()
    scope = RunScope(control)

    async def stop() -> None:
        await asyncio.sleep(0.02)
        control.request_user_stop()

    stopper = asyncio.create_task(stop())
    try:
        await sleep_cancellable(control, scope, 10)
        stopped = False
    except Exception:
        stopped = True
    await stopper
    await scope.aclose()
    assert stopped


def _empty_request():  # noqa: ANN201
    from agent_runtime.contracts import ModelConfig, ModelRequest, RecoveryBudget, ToolChoice

    return ModelRequest(messages=(), tools=(), model=ModelConfig(), tool_choice=ToolChoice(), recovery=RecoveryBudget())
