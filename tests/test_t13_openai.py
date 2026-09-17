from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from agent_runtime.adapters.openai import (
    OpenAIChatCompletionsModel,
    create_openai_client,
    normalize_chat_completion_stream,
    request_payload,
)
from agent_runtime.contracts import (
    Message,
    ModelCompleted,
    ModelConfig,
    ModelRequest,
    RecoveryBudget,
    Role,
    RunControl,
    StepIdentity,
    TextDeltaEvent,
    ToolChoice,
    ToolChoiceMode,
)
from agent_runtime.exceptions import AdapterError
from agent_runtime.testing.scenarios import ECHO_TOOL

IDENTITY = StepIdentity("openai", 0, 0)


def _request() -> ModelRequest:
    return ModelRequest(
        messages=(Message(role=Role.USER, content="hi"),),
        tools=(ECHO_TOOL,),
        model=ModelConfig(model="gpt-test", temperature=0.0),
        tool_choice=ToolChoice(mode=ToolChoiceMode.AUTO),
        recovery=RecoveryBudget(max_attempts=1, remaining_attempts=1),
    )


async def _chunks(*items: object) -> AsyncIterator[object]:
    for item in items:
        yield item


def _delta(*, content: str | None = None, tool_calls: object = None, finish: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=tool_calls), finish_reason=finish)],
        usage=None,
    )


async def test_normalize_text_stream() -> None:
    events = [
        event
        async for event in normalize_chat_completion_stream(
            _chunks(_delta(content="Hel"), _delta(content="lo", finish="stop")),
            identity=IDENTITY,
        )
    ]
    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == ["Hel", "lo"]
    completed = events[-1]
    assert isinstance(completed, ModelCompleted)
    assert completed.result.text == "Hello"
    assert completed.result.finish_reason == "stop"


async def test_normalize_assembles_tool_argument_fragments() -> None:
    first = SimpleNamespace(index=0, id="call_1", function=SimpleNamespace(name="echo", arguments='{"text":'))
    second = SimpleNamespace(index=0, id=None, function=SimpleNamespace(name=None, arguments='"hi"}'))
    events = [
        event
        async for event in normalize_chat_completion_stream(
            _chunks(_delta(tool_calls=[first]), _delta(tool_calls=[second], finish="tool_calls")),
            identity=IDENTITY,
        )
    ]
    completed = events[-1]
    assert isinstance(completed, ModelCompleted)
    assert completed.result.tool_calls[0].name == "echo"
    assert dict(completed.result.tool_calls[0].arguments) == {"text": "hi"}


async def test_empty_stream_is_not_effective() -> None:
    events = [event async for event in normalize_chat_completion_stream(_chunks(), identity=IDENTITY)]
    assert events == []


async def test_invalid_tool_arguments_are_protocol_errors() -> None:
    broken = SimpleNamespace(index=0, id="c1", function=SimpleNamespace(name="echo", arguments="{"))
    with pytest.raises(AdapterError):
        async for _event in normalize_chat_completion_stream(_chunks(_delta(tool_calls=[broken])), identity=IDENTITY):
            pass


async def test_adapter_timeout_and_disconnect() -> None:
    class SlowCompletions:
        async def create(self, **kwargs: object) -> AsyncIterator[object]:
            del kwargs
            await asyncio.sleep(10)
            return _chunks()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SlowCompletions()), closed=False)

    async def close() -> None:
        client.closed = True

    client.close = close
    model = OpenAIChatCompletionsModel(client, timeout=0.05, owns_client=False)
    with pytest.raises(AdapterError):
        async for _event in model.stream(_request(), RunControl()):
            pass
    await model.aclose()
    assert client.closed is False


async def test_mid_stream_failure_becomes_adapter_error() -> None:
    class BoomStream:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self) -> BoomStream:
            return self

        async def __anext__(self) -> object:
            raise RuntimeError("socket closed")

        async def aclose(self) -> None:
            self.closed = True

    stream = BoomStream()

    class Completions:
        async def create(self, **kwargs: object) -> BoomStream:
            del kwargs
            return stream

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()), closed=False)
    model = OpenAIChatCompletionsModel(client, timeout=1.0)
    with pytest.raises(AdapterError, match="socket closed"):
        async for _event in model.stream(_request(), RunControl()):
            pass
    assert stream.closed is True
    await model.aclose()
    assert client.closed is False


async def test_owned_client_is_closed() -> None:
    class Completions:
        async def create(self, **kwargs: object) -> AsyncIterator[object]:
            del kwargs
            return _chunks(_delta(content="ok", finish="stop"))

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()), closed=False)

    async def close() -> None:
        client.closed = True

    client.close = close
    model = OpenAIChatCompletionsModel(client, owns_client=True, timeout=1.0)
    events = [event async for event in model.stream(_request(), RunControl())]
    assert any(isinstance(event, ModelCompleted) for event in events)
    await model.aclose()
    assert client.closed is True


def test_request_payload_and_missing_sdk() -> None:
    payload = request_payload(_request(), default_model="gpt-4o-mini")
    assert payload["model"] == "gpt-test"
    assert payload["stream"] is True
    assert payload["tools"][0]["function"]["name"] == "echo"
    try:
        client = create_openai_client(api_key="sk-test")
    except AdapterError as exc:
        assert "openai" in str(exc)
        return
    assert client is not None
