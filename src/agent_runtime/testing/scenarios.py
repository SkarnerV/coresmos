"""Synthetic closed-loop scenarios used by examples and tests."""

from __future__ import annotations

from agent_runtime.contracts import (
    Message,
    ModelEntry,
    RecordTarget,
    Role,
    RunControl,
    RunRequest,
    RunSucceeded,
    RuntimeEvent,
    ToolCall,
    ToolSpec,
)
from agent_runtime.jsonutil import freeze_mapping
from agent_runtime.lifecycle import await_despite_cancellation
from agent_runtime.runner import assemble_default
from agent_runtime.testing.models import ScriptedModel, ScriptedTurn
from agent_runtime.testing.tools import ScriptedInvoker, ScriptedToolBehavior

ECHO_TOOL = ToolSpec(
    name="echo",
    description="Echo text back",
    parameters=freeze_mapping(
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }
    ),
)


async def collect_run(request: RunRequest, control: RunControl, runtime: object) -> list[RuntimeEvent]:
    events: list[RuntimeEvent] = []
    stream = runtime.run(request, control)  # type: ignore[attr-defined]
    try:
        async for event in stream:
            events.append(event)
    finally:
        closer = getattr(stream, "aclose", None)
        if closer is not None:
            await await_despite_cancellation(closer())
    return events


async def run_tool_then_text(*, run_id: str = "run-tool-text") -> list[RuntimeEvent]:
    """Model selects echo, the tool runs, then the model produces a final text answer."""
    model = ScriptedModel(
        [
            ScriptedTurn(tool_calls=(ToolCall(call_id="call-1", name="echo", arguments={"text": "hi"}),)),
            ScriptedTurn(text="done: hi", deltas=("done", ": hi")),
        ]
    )
    invoker = ScriptedInvoker({"echo": ScriptedToolBehavior(output="hi")})
    runtime = assemble_default(model=model, invoker=invoker, tools=(ECHO_TOOL,))
    request = RunRequest(
        run_id=run_id,
        input_items=(Message(role=Role.USER, content="echo hi"),),
        record_target=RecordTarget("chat-1"),
        entry=ModelEntry(),
    )
    events = await collect_run(request, RunControl(), runtime)
    if not any(isinstance(event, RunSucceeded) for event in events):
        raise AssertionError("tool-then-text scenario did not succeed")
    if model.call_count != 2:
        raise AssertionError(f"expected two model attempts across rounds, got {model.call_count}")
    if invoker.invoke_count != 1:
        raise AssertionError(f"expected one tool invoke, got {invoker.invoke_count}")
    return events
