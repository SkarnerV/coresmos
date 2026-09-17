from __future__ import annotations

from agent_runtime.contracts import Flow, FlowDecision, RecordTarget, ToolCall
from agent_runtime.jsonutil import freeze_json
from agent_runtime.policies import aggregate_flow_decisions
from agent_runtime.ports import TranscriptPort
from agent_runtime.recording import MemoryTranscript


def test_frozen_nested_json_does_not_share_caller_storage() -> None:
    raw: dict[str, object] = {"items": ["a", {"k": 1}]}
    frozen = freeze_json(raw)
    raw["items"].append("b")  # type: ignore[union-attr]
    raw["items"][1]["k"] = 2  # type: ignore[index]
    assert frozen["items"] == ("a", {"k": 1})  # type: ignore[index]


def test_tool_call_copies_arguments() -> None:
    payload = {"text": "hi", "tags": ["x"]}
    call = ToolCall(call_id="c1", name="echo", arguments=payload)
    payload["tags"].append("y")  # type: ignore[union-attr]
    assert list(call.arguments["tags"]) == ["x"]  # type: ignore[arg-type]


def test_memory_transcript_satisfies_port_without_subclass() -> None:
    transcript: TranscriptPort = MemoryTranscript()
    assert hasattr(transcript, "record_input")


def test_flow_priority_wait_outranks_finish_and_continue() -> None:
    decided = aggregate_flow_decisions(
        (
            FlowDecision(flow=Flow.CONTINUE),
            FlowDecision(flow=Flow.FINISH, reason="done"),
            FlowDecision(flow=Flow.WAIT, reason="need host", pending_ref=None),
        )
    )
    assert decided.flow is Flow.WAIT


def test_record_target_is_opaque() -> None:
    assert RecordTarget("session-9").value == "session-9"
