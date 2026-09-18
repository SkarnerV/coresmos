"""Run the public contract suite from a wheel-only environment, without pytest."""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import json
from pathlib import Path

import agent_runtime
from agent_runtime.testing import run_contract_suite

PACKAGE = Path(agent_runtime.__file__).resolve()


async def main() -> None:
    names = await run_contract_suite()
    assert len(names) == 20
    extras: dict[str, str] = {}
    for distribution in ("openai", "opentelemetry-api"):
        try:
            extras[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            pass
    if "openai" in extras:
        from agent_runtime.adapters.openai import create_openai_client

        client = create_openai_client(api_key="acceptance-no-network")
        assert client.max_retries == 0
        await client.close()
    if "opentelemetry-api" in extras:
        from agent_runtime.observability import OpenTelemetryObserver, TimingEvent

        await OpenTelemetryObserver().on_event(TimingEvent(name="acceptance", run_id="installed", duration_ms=1))
    print(json.dumps({"package": str(PACKAGE), "scenes_passed": len(names), "scenes": names, "extras": extras}))


if __name__ == "__main__":
    assert "site-packages" in PACKAGE.parts
    assert PACKAGE.with_name("py.typed").is_file()
    assert importlib.util.find_spec("pytest") is None
    asyncio.run(main())
