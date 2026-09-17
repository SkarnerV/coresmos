"""Synthetic scenario: the model calls an ordinary tool, then produces text."""

from __future__ import annotations

import asyncio
import sys

from agent_runtime.testing.scenarios import run_tool_then_text


async def main() -> None:
    events = await run_tool_then_text()
    for event in events:
        kind = getattr(event, "kind", type(event).__name__)
        print(kind)


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
