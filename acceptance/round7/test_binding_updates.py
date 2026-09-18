"""Acceptance of E01 across capability updates and shared registries."""

from __future__ import annotations

import asyncio

import pytest

from agent_runtime.capabilities import BindingRegistry, CapabilitySession
from agent_runtime.contracts import CapabilitySnapshot, ToolSpec
from agent_runtime.exceptions import CapabilityError
from agent_runtime.testing.scenarios import ECHO_TOOL

OTHER_TOOL = ToolSpec("other", "other tool", {"type": "object", "properties": {}})
THIRD_TOOL = ToolSpec("third", "third tool", {"type": "object", "properties": {}})


def make_session(backend: str = "selected-backend", bindings: BindingRegistry | None = None) -> CapabilitySession:
    registry = bindings if bindings is not None else BindingRegistry()
    initial = CapabilitySnapshot(
        version="cap-1",
        tools=(ECHO_TOOL,),
        binding_ref=registry.publish({"echo": backend}),
    )
    return CapabilitySession(initial, registry)


def test_repeated_activation_preserves_custom_bindings_and_old_versions() -> None:
    session = make_session()
    initial = session.snapshot()
    with_other = session.publish((ECHO_TOOL, OTHER_TOOL), extra_bindings={"other": "other-backend"})
    with_third = session.activate((THIRD_TOOL,))
    repeated = session.activate((OTHER_TOOL,))

    versions = (initial, with_other, with_third, repeated)
    assert len({snapshot.binding_ref for snapshot in versions}) == 4
    assert [session.bindings.resolve(snapshot.binding_ref, "echo") for snapshot in versions] == ["selected-backend"] * 4
    assert session.bindings.names(initial.binding_ref) == {"echo"}
    assert session.bindings.names(with_other.binding_ref) == {"echo", "other"}
    for snapshot in versions[1:]:
        assert session.bindings.resolve(snapshot.binding_ref, "other") == "other-backend"
    for snapshot in versions[2:]:
        assert session.bindings.resolve(snapshot.binding_ref, "third") == "default"


def test_explicit_override_and_removal_leave_previous_bindings_intact() -> None:
    session = make_session()
    initial = session.snapshot()
    overridden = session.publish(
        (ECHO_TOOL, OTHER_TOOL), extra_bindings={"echo": "override-backend", "other": "other-backend"}
    )
    removed = session.publish((OTHER_TOOL,))
    reactivated = session.activate((ECHO_TOOL,))

    assert session.bindings.resolve(initial.binding_ref, "echo") == "selected-backend"
    assert session.bindings.resolve(overridden.binding_ref, "echo") == "override-backend"
    assert session.bindings.names(removed.binding_ref) == {"other"}
    with pytest.raises(CapabilityError):
        session.bindings.resolve(removed.binding_ref, "echo")
    assert session.bindings.resolve(reactivated.binding_ref, "echo") == "default"
    for snapshot in (overridden, removed, reactivated):
        assert session.bindings.resolve(snapshot.binding_ref, "other") == "other-backend"


async def test_concurrent_sessions_keep_bindings_during_repeated_activation() -> None:
    registry = BindingRegistry()
    sessions = [make_session(backend, registry) for backend in ("first-backend", "second-backend")]
    barrier = asyncio.Barrier(2)

    async def activate(session: CapabilitySession):
        snapshots = [session.snapshot()]
        for tool in (OTHER_TOOL, THIRD_TOOL):
            snapshots.append(session.activate((tool,)))
            await barrier.wait()
        return snapshots

    results = await asyncio.wait_for(asyncio.gather(*(activate(session) for session in sessions)), timeout=2)
    assert len({snapshot.binding_ref for snapshots in results for snapshot in snapshots}) == 6
    for snapshots, backend in zip(results, ("first-backend", "second-backend"), strict=True):
        assert [registry.resolve(snapshot.binding_ref, "echo") for snapshot in snapshots] == [backend] * 3
        assert registry.names(snapshots[0].binding_ref) == {"echo"}
        assert registry.names(snapshots[1].binding_ref) == {"echo", "other"}
        assert registry.resolve(snapshots[2].binding_ref, "other") == "default"
        assert registry.resolve(snapshots[2].binding_ref, "third") == "default"
