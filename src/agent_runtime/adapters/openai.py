"""OpenAI Chat Completions reference adapter. Optional extra: agent-runtime[openai]."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
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
from agent_runtime.jsonutil import freeze_mapping
from agent_runtime.lifecycle import CallbackStream
from agent_runtime.ports import ManagedEventStream


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


class OpenAIChatCompletionsModel:
    """Reference LowLevelModel. Vendor SDK types stay inside this adapter."""

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

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelStepEvent]:
        create = self._client.chat.completions.create
        identity = StepIdentity(run_id="openai", step_no=0, model_round=0)
        kwargs = request_payload(request, default_model=self._default_model)
        stream = None
        try:
            async with asyncio.timeout(self._timeout):
                stream = await create(**kwargs)
                self._active_streams.append(stream)
                async for event in normalize_chat_completion_stream(stream, identity=identity):
                    yield event
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(str(exc)) from exc
        finally:
            if stream is not None:
                await _release(stream)
                if stream in self._active_streams:
                    self._active_streams.remove(stream)

    async def aclose(self) -> None:
        for stream in list(self._active_streams):
            await _release(stream)
        self._active_streams.clear()
        if self._owns_client:
            await _release(self._client)


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
                    "function": {"name": call.name, "arguments": json.dumps(dict(call.arguments))},
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
                    "parameters": dict(spec.parameters),
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
