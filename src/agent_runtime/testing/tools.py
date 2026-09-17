"""Controlled tool invoker with observable call counts."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from agent_runtime.contracts import (
    ExternalWorkRef,
    Flow,
    FlowDecision,
    InvocationOutcome,
    PendingRef,
    RecordTarget,
    RunControl,
    ToolCall,
    ToolInvocation,
    ToolResult,
    ToolSpec,
)
from agent_runtime.exceptions import CancelledRunError
from agent_runtime.jsonutil import JsonValue


@dataclass(frozen=True)
class ScriptedToolBehavior:
    output: str = "ok"
    is_error: bool = False
    delay: float = 0.0
    block: bool = False
    flow_hint: Flow | None = None
    pending_ref: PendingRef | None = None
    next_target: RecordTarget | None = None
    activate_tools: tuple[ToolSpec, ...] = ()
    state_payload: Mapping[str, JsonValue] | None = None
    progress: tuple[Mapping[str, JsonValue], ...] = ()
    external_handle: str | None = None
    external_cancellable: bool = False


class ScriptedInvoker:
    """Records real call counts. Declares progress and external work before it blocks."""

    def __init__(
        self,
        by_name: Mapping[str, ScriptedToolBehavior] | None = None,
        *,
        default: ScriptedToolBehavior | None = None,
    ) -> None:
        self.by_name = dict(by_name or {})
        self.default = default or ScriptedToolBehavior()
        self.invoke_count = 0
        self.calls: list[ToolCall] = []
        self._lock = asyncio.Lock()

    async def invoke(self, invocation: ToolInvocation) -> InvocationOutcome:
        call = invocation.call
        control = invocation.control
        async with self._lock:
            self.invoke_count += 1
            self.calls.append(call)
        behavior = self.by_name.get(call.name, self.default)
        for payload in behavior.progress:
            invocation.report_progress(payload)
        if behavior.external_handle is not None:
            invocation.declare_external_work(
                ExternalWorkRef(
                    call_id=call.call_id,
                    tool_name=call.name,
                    handle=behavior.external_handle,
                    cancellable=behavior.external_cancellable,
                )
            )
        if behavior.block:
            reason = await control.wait()
            raise CancelledRunError(reason.value)
        if behavior.delay > 0:
            await _sleep_or_stop(control, behavior.delay)
        hint = None
        if behavior.flow_hint is not None:
            hint = FlowDecision(
                flow=behavior.flow_hint,
                reason=behavior.flow_hint.value,
                pending_ref=behavior.pending_ref,
            )
        return InvocationOutcome(
            result=ToolResult(
                call_id=call.call_id,
                name=call.name,
                output=behavior.output,
                is_error=behavior.is_error,
            ),
            flow_hint=hint,
            next_record_target=behavior.next_target,
            activate_tools=behavior.activate_tools,
            state_payload=behavior.state_payload,
        )


async def _sleep_or_stop(control: RunControl, delay: float) -> None:
    stopper = asyncio.create_task(control.wait())
    sleeper = asyncio.create_task(asyncio.sleep(delay))
    done, pending = await asyncio.wait({stopper, sleeper}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if stopper in done:
        raise CancelledRunError(stopper.result().value)
