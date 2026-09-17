"""Optional live smoke test for the OpenAI Chat Completions adapter.

Not collected by default CI. Requires AGENT_RUNTIME_OPENAI_API_KEY.
"""

from __future__ import annotations

import asyncio
import os
import sys

from agent_runtime.adapters.openai import OpenAIChatCompletionsModel, create_openai_client
from agent_runtime.contracts import (
    Message,
    ModelCompleted,
    ModelConfig,
    ModelRequest,
    RecoveryBudget,
    Role,
    RunControl,
    TextDeltaEvent,
    ToolChoice,
)


async def main() -> int:
    api_key = os.environ.get("AGENT_RUNTIME_OPENAI_API_KEY")
    if not api_key:
        print("set AGENT_RUNTIME_OPENAI_API_KEY to run this smoke test", file=sys.stderr)
        return 2
    client = create_openai_client(api_key=api_key, timeout=30.0)
    model = OpenAIChatCompletionsModel(client, timeout=30.0, owns_client=True, default_model="gpt-4o-mini")
    request = ModelRequest(
        messages=(Message(role=Role.USER, content="Reply with the single word ping."),),
        tools=(),
        model=ModelConfig(model="gpt-4o-mini"),
        tool_choice=ToolChoice(),
        recovery=RecoveryBudget(max_attempts=1, remaining_attempts=1),
    )
    text = []
    completed = False
    stream = model.stream(request, RunControl())
    try:
        async for event in stream:
            if isinstance(event, TextDeltaEvent):
                text.append(event.text)
            if isinstance(event, ModelCompleted):
                completed = True
                print(event.result.text or "".join(text))
    finally:
        await stream.aclose()
        await model.aclose()
    return 0 if completed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
