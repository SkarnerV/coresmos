"""In-memory application snapshot owner used by synthetic runs."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from agent_runtime.contracts import ApplicationSnapshot, RecordTarget
from agent_runtime.jsonutil import JsonValue, freeze_mapping


class MemoryApplicationState:
    def __init__(self, record_target: RecordTarget) -> None:
        self._lock = asyncio.Lock()
        self._seq = 1
        self._data: dict[str, ApplicationSnapshot] = {
            record_target.value: ApplicationSnapshot(version="app-1", record_target=record_target)
        }
        self.fail_next = False

    async def current(self, record_target: RecordTarget) -> ApplicationSnapshot:
        async with self._lock:
            existing = self._data.get(record_target.value)
            if existing is None:
                snapshot = ApplicationSnapshot(version="app-1", record_target=record_target)
                self._data[record_target.value] = snapshot
                return snapshot
            return existing

    async def commit(
        self,
        record_target: RecordTarget,
        payload: Mapping[str, JsonValue],
    ) -> ApplicationSnapshot:
        async with self._lock:
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("injected application commit failure")
            self._seq += 1
            snapshot = ApplicationSnapshot(
                version=f"app-{self._seq}",
                record_target=record_target,
                payload=freeze_mapping(payload),
            )
            self._data[record_target.value] = snapshot
            return snapshot
