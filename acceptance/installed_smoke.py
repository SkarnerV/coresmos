"""Run with the isolated acceptance environment, outside the source directory."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from importlib.metadata import version
from pathlib import Path

import agent_runtime
from agent_runtime.testing.scenarios import run_tool_then_text

package_file = Path(agent_runtime.__file__).resolve()
assert "site-packages" in package_file.parts, package_file
assert package_file.with_name("py.typed").is_file()
assert importlib.util.find_spec("openai") is None
assert importlib.util.find_spec("opentelemetry") is None
assert importlib.util.find_spec("pytest") is None
events = asyncio.run(run_tool_then_text())
print(
    json.dumps(
        {
            "version": version("agent-runtime"),
            "package_file": str(package_file),
            "cwd": str(Path.cwd()),
            "py_typed": True,
            "extras_installed": False,
            "events": [event.kind for event in events],
        },
        indent=2,
    )
)
