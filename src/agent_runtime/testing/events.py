"""Collect runtime events for assertions."""

from __future__ import annotations

from agent_runtime.contracts import RuntimeEvent


class EventRecorder:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []
        self.closed = False

    def record(self, event: RuntimeEvent) -> None:
        self.events.append(event)

    def of_type[T](self, kind: type[T]) -> list[T]:
        return [event for event in self.events if isinstance(event, kind)]
