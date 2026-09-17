from __future__ import annotations

from dataclasses import replace

import pytest

from agent_runtime.contracts import CallRecord, Message, RecordTarget, Role, ToolCall, ToolResult
from agent_runtime.exceptions import MessageNotFoundError, ReceiptMismatchError, RecordingError
from agent_runtime.recording import MemoryTranscript


async def test_delta_and_final_share_message_in_same_step_and_target() -> None:
    transcript = MemoryTranscript()
    target = RecordTarget("t1")
    delta = await transcript.record_text_delta(
        run_id="r",
        step_no=1,
        record_target=target,
        text="hel",
        logical_op_id="r:1:t1:a1:delta:1",
        attempt=1,
    )
    final = await transcript.record_text_final(
        run_id="r",
        step_no=1,
        record_target=target,
        text="hello",
        reasoning=None,
        tool_calls=(),
        logical_op_id="r:1:t1:a1:final",
        attempt=1,
        previous_receipt=delta,
    )
    assert delta.message_id == final.message_id
    snapshot = transcript.snapshot(target)
    assert snapshot.messages[0].content == "hello"


async def test_new_step_and_target_are_isolated() -> None:
    transcript = MemoryTranscript()
    first = await transcript.record_text_final(
        run_id="r",
        step_no=1,
        record_target=RecordTarget("a"),
        text="one",
        reasoning=None,
        tool_calls=(),
        logical_op_id="op-a",
        attempt=1,
    )
    second = await transcript.record_text_final(
        run_id="r",
        step_no=2,
        record_target=RecordTarget("a"),
        text="two",
        reasoning=None,
        tool_calls=(),
        logical_op_id="op-b",
        attempt=1,
    )
    other = await transcript.record_text_final(
        run_id="r",
        step_no=1,
        record_target=RecordTarget("b"),
        text="other",
        reasoning=None,
        tool_calls=(),
        logical_op_id="op-c",
        attempt=1,
    )
    assert first.message_id != second.message_id != other.message_id
    assert [msg.content for msg in transcript.snapshot(RecordTarget("a")).messages] == ["one", "two"]
    assert [msg.content for msg in transcript.snapshot(RecordTarget("b")).messages] == ["other"]


async def test_duplicate_submit_does_not_recreate() -> None:
    transcript = MemoryTranscript()
    message = Message(role=Role.USER, content="hi")
    first = await transcript.record_input(
        run_id="r", record_target=RecordTarget("t"), message=message, logical_op_id="in-1"
    )
    again = await transcript.record_input(
        run_id="r", record_target=RecordTarget("t"), message=message, logical_op_id="in-1"
    )
    assert first.message_id == again.message_id
    assert again.created is False
    assert len(transcript.snapshot(RecordTarget("t")).messages) == 1


async def test_missing_message_and_illegal_receipt_are_rejected() -> None:
    transcript = MemoryTranscript()
    with pytest.raises(MessageNotFoundError):
        await transcript.record_text_delta(
            run_id="r",
            step_no=1,
            record_target=RecordTarget("t"),
            text="x",
            logical_op_id="d",
            attempt=1,
            previous_receipt=type("R", (), {"message_id": "missing", "logical_op_id": "nope"})(),  # type: ignore[arg-type]
        )
    with pytest.raises(ReceiptMismatchError):
        await transcript.record_tool_results(
            run_id="r",
            step_no=1,
            record_target=RecordTarget("t"),
            results=(ToolResult(call_id="c", name="echo", output="ok"),),
            logical_op_id="res",
            previous_receipt=type(  # type: ignore[arg-type]
                "B",
                (),
                {
                    "logical_op_id": "no-such",
                    "calls": (),
                    "run_id": "r",
                    "step_no": 1,
                    "record_target": RecordTarget("t"),
                    "assistant_message_id": None,
                    "recording_required": True,
                    "created": True,
                },
            )(),
        )


async def test_injected_failure_does_not_commit() -> None:
    transcript = MemoryTranscript()
    transcript.inject_write_failure("in-fail")
    with pytest.raises(RecordingError):
        await transcript.record_input(
            run_id="r",
            record_target=RecordTarget("t"),
            message=Message(role=Role.USER, content="nope"),
            logical_op_id="in-fail",
        )
    assert transcript.snapshot(RecordTarget("t")).messages == ()


async def test_tool_calls_and_results_pair_on_existing_assistant_message() -> None:
    transcript = MemoryTranscript()
    target = RecordTarget("t")
    calls = (ToolCall(call_id="c1", name="echo", arguments={"text": "hi"}),)
    await transcript.record_text_final(
        run_id="r",
        step_no=1,
        record_target=target,
        text=None,
        reasoning=None,
        tool_calls=calls,
        logical_op_id="final",
        attempt=1,
    )
    batch = await transcript.record_tool_calls(
        run_id="r",
        step_no=1,
        record_target=target,
        calls=calls,
        assistant_text=None,
        reasoning=None,
        logical_op_id="calls",
        attempt=1,
    )
    receipts = await transcript.record_tool_results(
        run_id="r",
        step_no=1,
        record_target=target,
        results=(ToolResult(call_id="c1", name="echo", output="hi"),),
        logical_op_id="results",
        previous_receipt=batch,
    )
    messages = transcript.snapshot(target).messages
    assert batch.assistant_message_id == messages[0].message_id
    assert receipts[0].created is True
    assert messages[-1].role is Role.TOOL
    assert messages[-1].tool_call_id == "c1"


async def test_failed_result_batch_does_not_publish_partial_history() -> None:
    transcript = MemoryTranscript()
    target = RecordTarget("t")
    receipt = await transcript.record_tool_calls(
        run_id="r",
        step_no=1,
        record_target=target,
        calls=(
            ToolCall(call_id="c1", name="echo", arguments={"text": "a"}),
            ToolCall(call_id="c2", name="echo", arguments={"text": "b"}),
        ),
        assistant_text=None,
        reasoning=None,
        logical_op_id="calls",
    )
    before = transcript.snapshot(target)
    with pytest.raises(ReceiptMismatchError):
        await transcript.record_tool_results(
            run_id="r",
            step_no=1,
            record_target=target,
            results=(
                ToolResult(call_id="c1", name="echo", output="ok"),
                ToolResult(call_id="unknown", name="echo", output="bad"),
            ),
            logical_op_id="results",
            previous_receipt=receipt,
        )
    assert transcript.snapshot(target) == before


async def test_result_receipt_is_bound_to_stored_batch_identity() -> None:
    transcript = MemoryTranscript()
    target = RecordTarget("t")
    receipt = await transcript.record_tool_calls(
        run_id="r",
        step_no=1,
        record_target=target,
        calls=(ToolCall(call_id="c1", name="echo", arguments={"text": "a"}),),
        assistant_text=None,
        reasoning=None,
        logical_op_id="calls",
    )
    forged = replace(receipt, calls=(CallRecord("unrecorded", "echo", receipt.calls[0].message_id),))
    with pytest.raises(ReceiptMismatchError):
        await transcript.record_tool_results(
            run_id="r",
            step_no=1,
            record_target=target,
            results=(ToolResult(call_id="unrecorded", name="echo", output="ok"),),
            logical_op_id="results",
            previous_receipt=forged,
        )
    with pytest.raises(ReceiptMismatchError):
        await transcript.record_tool_results(
            run_id="other",
            step_no=9,
            record_target=RecordTarget("other"),
            results=(ToolResult(call_id="c1", name="echo", output="ok"),),
            logical_op_id="other-results",
            previous_receipt=receipt,
        )
