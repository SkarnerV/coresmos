"""OpenAI Chat Completions reference adapter. Optional extra: agent-runtime[openai]."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Mapping
from dataclasses import dataclass
from typing import Any

from agent_runtime.contracts import (
    ModelCompleted,
    ModelRequest,
    ModelResult,
    ModelStepEvent,
    RunControl,
    StepIdentity,
    TextDeltaEvent,
    TokenUsage,
    ToolCall,
    ToolChoiceMode,
    UsageSource,
)
from agent_runtime.exceptions import AdapterError
from agent_runtime.jsonutil import JsonValue, freeze_mapping, thaw_json
from agent_runtime.lifecycle import CallbackStream, await_despite_cancellation
from agent_runtime.ports import ManagedEventStream

_STREAM_END = object()
_MIN_CLOSE_SECONDS = 0.001


@dataclass
class _ToolFragment:
    call_id: str = ""
    name: str = ""
    arguments: str = ""


def _first_choice(chunk: object) -> object | None:
    choices = getattr(chunk, "choices", None) or ()
    return choices[0] if choices else None


def _usage_from_chunk(chunk: object, current: TokenUsage) -> TokenUsage:
    raw = getattr(chunk, "usage", None)
    if raw is None:
        return current
    return TokenUsage(
        input_tokens=getattr(raw, "prompt_tokens", None),
        output_tokens=getattr(raw, "completion_tokens", None),
        total_tokens=getattr(raw, "total_tokens", None),
        source=UsageSource.PROVIDER,
    )


def assemble_tool_calls(fragments: dict[int, _ToolFragment]) -> tuple[ToolCall, ...]:
    calls: list[ToolCall] = []
    for index in sorted(fragments):
        item = fragments[index]
        raw_arguments = item.arguments or "{}"
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"invalid tool call arguments at index {index}") from exc
        if not isinstance(parsed, dict):
            raise AdapterError("tool call arguments must be a JSON object")
        calls.append(
            ToolCall(
                call_id=item.call_id or f"call-{index}",
                name=item.name,
                arguments=freeze_mapping(parsed),
            )
        )
    return tuple(calls)


async def normalize_chat_completion_stream(
    chunks: AsyncIterator[object],
    *,
    identity: StepIdentity,
) -> AsyncIterator[ModelStepEvent]:
    text_parts: list[str] = []
    tools: dict[int, _ToolFragment] = {}
    finish_reason: str | None = None
    usage = TokenUsage()
    async for chunk in chunks:
        usage = _usage_from_chunk(chunk, usage)
        choice = _first_choice(chunk)
        if choice is None:
            continue
        delta = getattr(choice, "delta", None)
        content = getattr(delta, "content", None) if delta is not None else None
        if content:
            text_parts.append(str(content))
            yield TextDeltaEvent(identity=identity, text=str(content), attempt=1)
        tool_deltas = getattr(delta, "tool_calls", None) if delta is not None else None
        for tool_delta in tool_deltas or ():
            index = int(getattr(tool_delta, "index", 0) or 0)
            acc = tools.setdefault(index, _ToolFragment())
            tool_id = getattr(tool_delta, "id", None)
            if tool_id:
                acc.call_id = str(tool_id)
            function = getattr(tool_delta, "function", None)
            if function is not None:
                name = getattr(function, "name", None)
                if name:
                    acc.name = str(name)
                arguments = getattr(function, "arguments", None)
                if arguments:
                    acc.arguments += str(arguments)
        reason = getattr(choice, "finish_reason", None)
        if reason:
            finish_reason = str(reason)
    result = ModelResult(
        text="".join(text_parts) or None,
        tool_calls=assemble_tool_calls(tools),
        usage=usage,
        finish_reason=finish_reason,
    )
    if result.is_effective():
        yield ModelCompleted(identity=identity, result=result)


async def _release(resource: object) -> None:
    closer = getattr(resource, "aclose", None) or getattr(resource, "close", None)
    if closer is None:
        return
    result = closer()
    if hasattr(result, "__await__"):
        await result


async def _release_within(resource: object, budget_seconds: float) -> bool:
    """Release a resource inside an explicit budget. False means the close did not finish in time."""
    try:
        async with asyncio.timeout(max(budget_seconds, _MIN_CLOSE_SECONDS)):
            await _release(resource)
    except TimeoutError:
        return False
    except Exception:
        # A resource that reports its own close failure is still no longer owned here.
        return True
    return True


async def _await_deadline[T](awaitable: Awaitable[T], deadline: float) -> T:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError("adapter timeout")
    return await asyncio.wait_for(awaitable, timeout=remaining)


def _json_object(value: Mapping[str, JsonValue] | Mapping[str, object]) -> dict[str, object]:
    thawed = thaw_json(value)  # type: ignore[arg-type]
    if not isinstance(thawed, dict):
        raise AdapterError("expected a JSON object")
    return thawed


class OpenAIChatCompletionsModel:
    """Reference LowLevelModel. Vendor SDK types stay inside this adapter.

    The timeout bounds each phase on its own: SDK create and chunk reads share one deadline,
    and releasing the stream gets the same budget again so cleanup can never wait forever.
    """

    def __init__(
        self,
        client: Any,
        *,
        timeout: float = 60.0,
        owns_client: bool = False,
        default_model: str | None = None,
    ) -> None:
        self._client = client
        self._timeout = timeout
        self._owns_client = owns_client
        self._default_model = default_model
        self._active_streams: list[Any] = []

    def stream(self, request: ModelRequest, control: RunControl) -> ManagedEventStream[ModelStepEvent]:
        del control
        return CallbackStream(self._stream(request))

    def _attempt_timeout(self, request: ModelRequest) -> float:
        """Bound the attempt by whichever comes first: the adapter timeout or the run deadline."""
        if request.deadline_monotonic is None:
            return self._timeout
        remaining = request.deadline_monotonic - time.monotonic()
        return max(_MIN_CLOSE_SECONDS, min(self._timeout, remaining))

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelStepEvent]:
        create = self._client.chat.completions.create
        identity = StepIdentity(run_id="openai", step_no=0, model_round=0)
        kwargs = request_payload(request, default_model=self._default_model)
        budget = self._attempt_timeout(request)
        deadline = asyncio.get_running_loop().time() + budget
        queue: asyncio.Queue[object] = asyncio.Queue()
        opened: list[Any] = []

        async def produce() -> None:
            # The producer owns reading; this generator owns the close, so the stream is
            # never released twice and a cancelled producer cannot block on cleanup.
            try:
                async with asyncio.timeout(budget):
                    stream = await create(**kwargs)
                    opened.append(stream)
                    self._active_streams.append(stream)
                    async for event in normalize_chat_completion_stream(stream, identity=identity):
                        await queue.put(event)
                await queue.put(_STREAM_END)
            except Exception as exc:
                await queue.put(exc)

        producer = asyncio.create_task(produce(), name="openai-stream-producer")
        drained = False
        try:
            while True:
                item = await _await_deadline(queue.get(), deadline)
                if item is _STREAM_END:
                    drained = True
                    return
                if isinstance(item, AdapterError):
                    raise item
                if isinstance(item, Exception):
                    raise AdapterError(str(item)) from item
                yield item  # type: ignore[misc]
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(str(exc)) from exc
        finally:
            released = await await_despite_cancellation(self._release_producer(producer, opened))
            if drained and not released:
                raise AdapterError("stream close exceeded the adapter timeout")

    async def _release_producer(self, producer: asyncio.Task[None], opened: list[Any]) -> bool:
        """Cancel the read task and release its stream, each inside the close budget."""
        released = True
        if not producer.done():
            producer.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(producer, return_exceptions=True), timeout=self._close_timeout)
        except TimeoutError:
            released = False
        for stream in list(opened):
            try:
                if not await _release_within(stream, self._close_timeout):
                    released = False
            finally:
                # A stream that timed out is not reported as released, but this adapter stops
                # owning it so later cleanup cannot inherit an unbounded wait.
                if stream in self._active_streams:
                    self._active_streams.remove(stream)
        return released

    @property
    def _close_timeout(self) -> float:
        return max(self._timeout, _MIN_CLOSE_SECONDS)

    async def aclose(self) -> None:
        for stream in list(self._active_streams):
            await _release_within(stream, self._close_timeout)
        self._active_streams.clear()
        if self._owns_client:
            await _release_within(self._client, self._close_timeout)


def create_openai_client(*, api_key: str, timeout: float = 60.0, **extra: Any) -> Any:
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise AdapterError("install agent-runtime[openai] to use the reference adapter") from exc
    return AsyncOpenAI(api_key=api_key, max_retries=0, timeout=timeout, **extra)


def request_payload(request: ModelRequest, *, default_model: str | None = None) -> dict[str, Any]:
    model = request.model.model
    if model == "synthetic" and default_model:
        model = default_model
    messages: list[dict[str, Any]] = []
    for message in request.messages:
        item: dict[str, Any] = {"role": message.role.value}
        if message.content is not None:
            item["content"] = message.content
        if message.tool_calls:
            item["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(_json_object(call.arguments))},
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id:
            item["tool_call_id"] = message.tool_call_id
        if message.name:
            item["name"] = message.name
        messages.append(item)
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": _json_object(spec.parameters),
                },
            }
            for spec in request.tools
        ]
    if request.tool_choice.mode is ToolChoiceMode.NONE:
        payload["tool_choice"] = "none"
    elif request.tool_choice.mode is ToolChoiceMode.REQUIRED:
        payload["tool_choice"] = "required"
    elif request.tool_choice.mode is ToolChoiceMode.NAMED and request.tool_choice.name:
        payload["tool_choice"] = {"type": "function", "function": {"name": request.tool_choice.name}}
    if request.model.temperature is not None:
        payload["temperature"] = request.model.temperature
    return payload
