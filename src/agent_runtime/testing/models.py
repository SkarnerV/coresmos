"""Scripted low-level model for synthetic runs and contract tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from agent_runtime.contracts import (
    ModelCompleted,
    ModelRequest,
    ModelResult,
    ModelStepEvent,
    ReasoningDeltaEvent,
    RunControl,
    StepIdentity,
    TextDeltaEvent,
    ToolCall,
)
from agent_runtime.exceptions import AdapterError, CancelledRunError
from agent_runtime.lifecycle import CallbackStream
from agent_runtime.ports import ManagedEventStream


@dataclass(frozen=True)
class ScriptedTurn:
    deltas: tuple[str, ...] = ()
    text: str | None = None
    reasoning: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    error: str | None = None
    silence: bool = False
    empty: bool = False
    delay: float = 0.0
    trailing_after_complete: bool = False
    duplicate_complete: bool = False


class ScriptedModel:
    def __init__(self, turns: Sequence[ScriptedTurn] = ()) -> None:
        self._turns = list(turns)
        self._index = 0
        self._lock = asyncio.Lock()
        self.call_count = 0
        self.close_count = 0
        self.requests: list[ModelRequest] = []

    def stream(self, request: ModelRequest, control: RunControl) -> ManagedEventStream[ModelStepEvent]:
        async def agen() -> AsyncIterator[ModelStepEvent]:
            async with self._lock:
                self.call_count += 1
                self.requests.append(request)
                if self._index >= len(self._turns):
                    turn = ScriptedTurn(empty=True)
                else:
                    turn = self._turns[self._index]
                    self._index += 1
            identity = StepIdentity(run_id="scripted", step_no=0, model_round=0)
            if turn.delay > 0:
                stopper = asyncio.create_task(control.wait())
                sleeper = asyncio.create_task(asyncio.sleep(turn.delay))
                done, pending = await asyncio.wait({stopper, sleeper}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if stopper in done:
                    raise CancelledRunError(stopper.result().value)
            if turn.silence:
                reason = await control.wait()
                raise CancelledRunError(reason.value)
            if turn.error is not None:
                raise AdapterError(turn.error)
            for delta in turn.deltas:
                yield TextDeltaEvent(identity=identity, text=delta, attempt=1)
            if turn.reasoning:
                yield ReasoningDeltaEvent(identity=identity, text=turn.reasoning, attempt=1)
            if turn.empty:
                return
            result = ModelResult(
                text=turn.text,
                reasoning=turn.reasoning,
                tool_calls=turn.tool_calls,
            )
            if result.is_effective():
                yield ModelCompleted(identity=identity, result=result)
                if turn.duplicate_complete:
                    yield ModelCompleted(identity=identity, result=result)
                if turn.trailing_after_complete:
                    yield TextDeltaEvent(identity=identity, text="trailing", attempt=1)

        def _on_close() -> None:
            self.close_count += 1

        return CallbackStream(agen(), on_close=_on_close)
