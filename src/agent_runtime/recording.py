"""Memory transcript: committed facts, idempotent ops, and explicit RecordTarget scope."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace

from agent_runtime.contracts import (
    BatchReceipt,
    CallRecord,
    LogicalOpKind,
    Message,
    MessageReceipt,
    RecordTarget,
    Role,
    RunStatus,
    ToolCall,
    ToolResult,
    TranscriptSnapshot,
)
from agent_runtime.exceptions import IdempotencyError, MessageNotFoundError, ReceiptMismatchError, RecordingError
from agent_runtime.ports import TranscriptPort


def _payload_key(value: object) -> object:
    if isinstance(value, Message):
        return (
            value.role,
            value.content,
            tuple((call.call_id, call.name, dict(call.arguments)) for call in value.tool_calls),
            value.tool_call_id,
            value.name,
            value.reasoning,
        )
    if isinstance(value, ToolCall):
        return (value.call_id, value.name, dict(value.arguments))
    if isinstance(value, ToolResult):
        return (value.call_id, value.name, value.output, value.is_error)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_payload_key(item) for item in value)
    return value


@dataclass
class _CommittedOp:
    kind: LogicalOpKind
    payload: object
    message_receipt: MessageReceipt | None = None
    batch_receipt: BatchReceipt | None = None


class MemoryTranscript:
    """In-memory TranscriptPort. Same behavioral contract as a database adapter."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._messages: dict[str, Message] = {}
        self._order: dict[str, list[str]] = {}
        self._ops: dict[str, _CommittedOp] = {}
        self._text_ids: dict[tuple[str, int, str, int], str] = {}
        self._fail_ops: set[str] = set()
        self._version = 0
        self._run_status: dict[str, RunStatus] = {}
        self._seq = 0

    def inject_write_failure(self, logical_op_id: str) -> None:
        self._fail_ops.add(logical_op_id)

    def run_status(self, run_id: str) -> RunStatus | None:
        return self._run_status.get(run_id)

    def _next_id(self, prefix: str, run_id: str) -> str:
        self._seq += 1
        return f"{prefix}:{run_id}:{self._seq}"

    def _check_fail(self, logical_op_id: str) -> None:
        if logical_op_id in self._fail_ops:
            self._fail_ops.discard(logical_op_id)
            raise RecordingError(f"injected write failure for {logical_op_id}")

    def _replay(self, logical_op_id: str, kind: LogicalOpKind, payload: object) -> _CommittedOp:
        existing = self._ops[logical_op_id]
        if existing.kind is not kind or existing.payload != _payload_key(payload):
            raise IdempotencyError(f"logical op {logical_op_id} reused with a different payload")
        return existing

    def _append(self, record_target: RecordTarget, message: Message) -> None:
        assert message.message_id is not None
        self._messages[message.message_id] = message
        self._order.setdefault(record_target.value, []).append(message.message_id)
        self._version += 1

    def _replace(self, message: Message) -> None:
        assert message.message_id is not None
        if message.message_id not in self._messages:
            raise MessageNotFoundError(message.message_id)
        self._messages[message.message_id] = message
        self._version += 1

    def snapshot(self, record_target: RecordTarget) -> TranscriptSnapshot:
        ids = self._order.get(record_target.value, [])
        messages = tuple(self._messages[message_id] for message_id in ids)
        return TranscriptSnapshot(
            version=f"v{self._version}:{record_target.value}", record_target=record_target, messages=messages
        )

    async def record_input(
        self,
        *,
        run_id: str,
        record_target: RecordTarget,
        message: Message,
        logical_op_id: str,
    ) -> MessageReceipt:
        async with self._lock:
            self._check_fail(logical_op_id)
            payload = message
            if logical_op_id in self._ops:
                existing = self._replay(logical_op_id, LogicalOpKind.INPUT, payload)
                assert existing.message_receipt is not None
                return replace(existing.message_receipt, created=False)
            message_id = self._next_id("msg", run_id)
            stored = replace(message, message_id=message_id)
            self._append(record_target, stored)
            receipt = MessageReceipt(
                message_id=message_id,
                run_id=run_id,
                step_no=None,
                record_target=record_target,
                logical_op_id=logical_op_id,
                created=True,
            )
            self._ops[logical_op_id] = _CommittedOp(LogicalOpKind.INPUT, _payload_key(payload), message_receipt=receipt)
            return receipt

    async def record_text_delta(
        self,
        *,
        run_id: str,
        step_no: int,
        record_target: RecordTarget,
        text: str,
        logical_op_id: str,
        attempt: int,
        previous_receipt: MessageReceipt | None = None,
    ) -> MessageReceipt:
        async with self._lock:
            self._check_fail(logical_op_id)
            payload = (text, attempt)
            if logical_op_id in self._ops:
                existing = self._replay(logical_op_id, LogicalOpKind.TEXT_DELTA, payload)
                assert existing.message_receipt is not None
                return replace(existing.message_receipt, created=False)
            if previous_receipt is not None and previous_receipt.message_id not in self._messages:
                raise MessageNotFoundError(previous_receipt.message_id)
            key = (run_id, step_no, record_target.value, attempt)
            message_id = self._text_ids.get(key)
            created = False
            if message_id is None:
                message_id = self._next_id("msg", run_id)
                self._text_ids[key] = message_id
                stored = Message(role=Role.ASSISTANT, content=text, message_id=message_id)
                self._append(record_target, stored)
                created = True
            else:
                current = self._messages.get(message_id)
                if current is None:
                    raise MessageNotFoundError(message_id)
                stored = replace(current, content=(current.content or "") + text)
                self._replace(stored)
            receipt = MessageReceipt(
                message_id=message_id,
                run_id=run_id,
                step_no=step_no,
                record_target=record_target,
                logical_op_id=logical_op_id,
                created=created,
            )
            self._ops[logical_op_id] = _CommittedOp(
                LogicalOpKind.TEXT_DELTA, _payload_key(payload), message_receipt=receipt
            )
            return receipt

    async def record_text_final(
        self,
        *,
        run_id: str,
        step_no: int,
        record_target: RecordTarget,
        text: str | None,
        reasoning: str | None,
        tool_calls: Sequence[ToolCall],
        logical_op_id: str,
        attempt: int,
        previous_receipt: MessageReceipt | None = None,
    ) -> MessageReceipt:
        async with self._lock:
            self._check_fail(logical_op_id)
            payload = (text, reasoning, tuple(tool_calls), attempt)
            if logical_op_id in self._ops:
                existing = self._replay(logical_op_id, LogicalOpKind.TEXT_FINAL, payload)
                assert existing.message_receipt is not None
                return replace(existing.message_receipt, created=False)
            if previous_receipt is not None:
                if previous_receipt.logical_op_id not in self._ops:
                    raise ReceiptMismatchError("previous_receipt is not a committed operation")
                if previous_receipt.message_id not in self._messages:
                    raise MessageNotFoundError(previous_receipt.message_id)
            key = (run_id, step_no, record_target.value, attempt)
            message_id = self._text_ids.get(key)
            created = False
            if message_id is None:
                message_id = self._next_id("msg", run_id)
                self._text_ids[key] = message_id
                stored = Message(
                    role=Role.ASSISTANT,
                    content=text,
                    reasoning=reasoning,
                    tool_calls=tuple(tool_calls),
                    message_id=message_id,
                )
                self._append(record_target, stored)
                created = True
            else:
                current = self._messages.get(message_id)
                if current is None:
                    raise MessageNotFoundError(message_id)
                stored = replace(
                    current,
                    content=text if text is not None else current.content,
                    reasoning=reasoning if reasoning is not None else current.reasoning,
                    tool_calls=tuple(tool_calls) if tool_calls else current.tool_calls,
                )
                self._replace(stored)
            receipt = MessageReceipt(
                message_id=message_id,
                run_id=run_id,
                step_no=step_no,
                record_target=record_target,
                logical_op_id=logical_op_id,
                created=created,
            )
            self._ops[logical_op_id] = _CommittedOp(
                LogicalOpKind.TEXT_FINAL, _payload_key(payload), message_receipt=receipt
            )
            return receipt

    async def record_tool_calls(
        self,
        *,
        run_id: str,
        step_no: int,
        record_target: RecordTarget,
        calls: Sequence[ToolCall],
        assistant_text: str | None,
        reasoning: str | None,
        logical_op_id: str,
        previous_receipt: BatchReceipt | None = None,
        attempt: int = 1,
    ) -> BatchReceipt:
        async with self._lock:
            self._check_fail(logical_op_id)
            payload = (tuple(calls), assistant_text, reasoning)
            if logical_op_id in self._ops:
                existing = self._replay(logical_op_id, LogicalOpKind.TOOL_CALLS, payload)
                assert existing.batch_receipt is not None
                return replace(existing.batch_receipt, created=False)
            if previous_receipt is not None:
                committed = self._ops.get(previous_receipt.logical_op_id)
                if committed is None or committed.batch_receipt != previous_receipt:
                    raise ReceiptMismatchError("previous_receipt does not match a committed batch")
            key = (run_id, step_no, record_target.value, attempt)
            message_id = self._text_ids.get(key)
            if message_id is None:
                message_id = self._next_id("msg", run_id)
                self._text_ids[key] = message_id
                stored = Message(
                    role=Role.ASSISTANT,
                    content=assistant_text,
                    reasoning=reasoning,
                    tool_calls=tuple(calls),
                    message_id=message_id,
                )
                self._append(record_target, stored)
            else:
                current = self._messages.get(message_id)
                if current is None:
                    raise MessageNotFoundError(message_id)
                stored = replace(
                    current,
                    content=assistant_text if assistant_text is not None else current.content,
                    reasoning=reasoning if reasoning is not None else current.reasoning,
                    tool_calls=tuple(calls),
                )
                self._replace(stored)
            receipt = BatchReceipt(
                run_id=run_id,
                step_no=step_no,
                record_target=record_target,
                logical_op_id=logical_op_id,
                calls=tuple(CallRecord(call.call_id, call.name, message_id) for call in calls),
                assistant_message_id=message_id,
                created=True,
            )
            self._ops[logical_op_id] = _CommittedOp(
                LogicalOpKind.TOOL_CALLS, _payload_key(payload), batch_receipt=receipt
            )
            return receipt

    async def record_tool_results(
        self,
        *,
        run_id: str,
        step_no: int,
        record_target: RecordTarget,
        results: Sequence[ToolResult],
        logical_op_id: str,
        previous_receipt: BatchReceipt,
    ) -> tuple[MessageReceipt, ...]:
        async with self._lock:
            self._check_fail(logical_op_id)
            payload = tuple(results)
            if logical_op_id in self._ops:
                existing = self._replay(logical_op_id, LogicalOpKind.TOOL_RESULTS, payload)
                assert existing.message_receipt is not None
                # Replay returns the first receipt; reconstruct from committed messages.
                receipts: list[MessageReceipt] = []
                for result in results:
                    for message_id, message in self._messages.items():
                        if message.tool_call_id == result.call_id:
                            receipts.append(
                                MessageReceipt(
                                    message_id=message_id,
                                    run_id=run_id,
                                    step_no=step_no,
                                    record_target=record_target,
                                    logical_op_id=logical_op_id,
                                    created=False,
                                )
                            )
                            break
                return tuple(receipts)
            committed = self._ops.get(previous_receipt.logical_op_id)
            if committed is None or committed.batch_receipt is None:
                raise ReceiptMismatchError("previous_receipt is not a committed tool-call batch")
            stored = committed.batch_receipt
            if (
                stored.logical_op_id != previous_receipt.logical_op_id
                or stored.run_id != previous_receipt.run_id
                or stored.step_no != previous_receipt.step_no
                or stored.record_target != previous_receipt.record_target
                or stored.calls != previous_receipt.calls
                or stored.assistant_message_id != previous_receipt.assistant_message_id
            ):
                raise ReceiptMismatchError("previous_receipt does not match committed batch identity")
            if stored.run_id != run_id or stored.step_no != step_no or stored.record_target != record_target:
                raise ReceiptMismatchError("previous_receipt is bound to a different run, step, or target")
            known = {record.call_id: record for record in stored.calls}
            pending_messages: list[Message] = []
            receipts_out: list[MessageReceipt] = []
            for result in results:
                record = known.get(result.call_id)
                if record is None:
                    raise ReceiptMismatchError(f"result {result.call_id} is not in the committed batch")
                if record.message_id not in self._messages:
                    raise MessageNotFoundError(record.message_id)
                message_id = self._next_id("msg", run_id)
                pending_messages.append(
                    Message(
                        role=Role.TOOL,
                        content=result.output,
                        tool_call_id=result.call_id,
                        name=result.name,
                        message_id=message_id,
                    )
                )
                receipts_out.append(
                    MessageReceipt(
                        message_id=message_id,
                        run_id=run_id,
                        step_no=step_no,
                        record_target=record_target,
                        logical_op_id=logical_op_id,
                        created=True,
                    )
                )
            for stored_message in pending_messages:
                self._append(record_target, stored_message)
            first = receipts_out[0] if receipts_out else None
            self._ops[logical_op_id] = _CommittedOp(
                LogicalOpKind.TOOL_RESULTS,
                _payload_key(payload),
                message_receipt=first,
            )
            return tuple(receipts_out)

    async def finalize_run(self, *, run_id: str, status: RunStatus) -> None:
        async with self._lock:
            op_id = f"{run_id}:finalize:{status.value}"
            self._check_fail(op_id)
            payload = status.value
            if op_id in self._ops:
                self._replay(op_id, LogicalOpKind.RUN_FINAL, payload)
                return
            self._run_status[run_id] = status
            self._ops[op_id] = _CommittedOp(LogicalOpKind.RUN_FINAL, _payload_key(payload))


def as_transcript_port(transcript: MemoryTranscript) -> TranscriptPort:
    return transcript
