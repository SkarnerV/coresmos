"""Verify the rebuilt wheel's public scenes and optional imports, without a model service."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from importlib.metadata import version
from pathlib import Path

import agent_runtime
from agent_runtime.adapters.openai import create_openai_client
from agent_runtime.observability import OpenTelemetryObserver, TimingEvent
from agent_runtime.testing.harness import (
    assert_failed,
    assert_success,
    assert_waiting,
    scene_plain_text,
    scene_record_failure,
    scene_tool_then_text,
    scene_tool_updates_context,
    scene_wait_and_stop,
)

PACKAGE_FILE = Path(agent_runtime.__file__).resolve()


async def main() -> None:
    package = PACKAGE_FILE
    assert "site-packages" in package.parts
    assert package.with_name("py.typed").is_file()
    assert importlib.util.find_spec("pytest") is None
    plain = await scene_plain_text()
    assert_success(plain)
    tool = await scene_tool_then_text()
    assert_success(tool)
    assert tool.model.call_count == 2 and tool.invoker.invoke_count == 1
    changed = await scene_tool_updates_context()
    assert_success(changed)
    assert {spec.name for spec in changed.model.requests[1].tools} == {"echo", "other"}
    waiting = await scene_wait_and_stop()
    assert_waiting(waiting)
    assert waiting.model.call_count == 0 and waiting.invoker.invoke_count == 1
    failed = await scene_record_failure()
    assert_failed(failed)
    assert failed.model.call_count == 0 and failed.invoker.invoke_count == 0
    extras = {}
    if importlib.util.find_spec("openai") is not None:
        client = create_openai_client(api_key="acceptance-no-network")
        assert client.max_retries == 0
        await client.close()
        extras["openai"] = version("openai")
    if importlib.util.find_spec("opentelemetry") is not None:
        await OpenTelemetryObserver().on_event(TimingEvent(name="check", run_id="installed", duration_ms=1))
        extras["opentelemetry-api"] = version("opentelemetry-api")
    print(json.dumps({"package": str(package), "cwd": str(Path.cwd()), "scenes_passed": 5, "extras": extras}))


if __name__ == "__main__":
    asyncio.run(main())
