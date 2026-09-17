from __future__ import annotations

from agent_runtime.adapters.openai import OpenAIChatCompletionsModel, request_payload
from agent_runtime.observability import NoOpObserver, OpenTelemetryObserver


def test_openai_adapter_imports_without_sdk() -> None:
    assert OpenAIChatCompletionsModel is not None
    assert callable(request_payload)


def test_otel_observer_imports_without_sdk() -> None:
    assert NoOpObserver is not None
    assert OpenTelemetryObserver is not None
