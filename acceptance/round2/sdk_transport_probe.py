"""Use the real SDK and HTTPX MockTransport; never connect to a model endpoint."""

from __future__ import annotations

import asyncio
import json
from importlib.metadata import version
from pathlib import Path

import httpx

import agent_runtime
from agent_runtime.adapters.openai import OpenAIChatCompletionsModel, create_openai_client
from agent_runtime.contracts import Message, ModelCompleted, ModelConfig, ModelRequest, Role, RunControl, ToolCall
from agent_runtime.testing.scenarios import ECHO_TOOL


async def main() -> None:
    assert "site-packages" in Path(agent_runtime.__file__).parts
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        chunk = {
            "id": "mock-completion",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "acceptance-model",
            "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
        }
        body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=body)

    client = create_openai_client(
        api_key="acceptance-no-network",
        base_url="https://runtime-acceptance.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    model = OpenAIChatCompletionsModel(client, timeout=1, owns_client=True)
    config = ModelConfig(model="acceptance-model")
    user = Message(role=Role.USER, content="hello")
    nested = ToolCall("nested", "echo", {"item": {"id": 1}})
    cases = {
        "text_control": ModelRequest(messages=(user,), tools=(), model=config),
        "tool_schema": ModelRequest(messages=(user,), tools=(ECHO_TOOL,), model=config),
        "nested_history": ModelRequest(
            messages=(Message(role=Role.ASSISTANT, tool_calls=(nested,)),), tools=(), model=config
        ),
    }
    results = []
    try:
        for name, request in cases.items():
            before = len(requests)
            try:
                events = [event async for event in model.stream(request, RunControl())]
                assert any(isinstance(event, ModelCompleted) for event in events)
                results.append({"case": name, "passed": True, "http_calls": len(requests) - before})
            except Exception as exc:
                results.append(
                    {
                        "case": name,
                        "passed": False,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "http_calls": len(requests) - before,
                    }
                )
    finally:
        await model.aclose()
    print(json.dumps({"openai": version("openai"), "results": results}, indent=2))
    if not all(item["passed"] for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
