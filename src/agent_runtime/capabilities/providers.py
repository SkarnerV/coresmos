"""Fixed provider, NoMatch-only resolver chain, and versioned capability sessions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from agent_runtime.capabilities.schema import check_tool_spec
from agent_runtime.contracts import (
    ApplicationSnapshot,
    BindingSetRef,
    CapabilityPolicyRef,
    CapabilitySnapshot,
    MatchKind,
    Resolution,
    RunRequest,
    ToolSpec,
)
from agent_runtime.exceptions import CapabilityError
from agent_runtime.ports import CapabilityProvider


@dataclass
class BindingRegistry:
    """In-process invoker keys keyed by binding version. Credentials never appear here."""

    _versions: dict[str, dict[str, str]]

    def __init__(self) -> None:
        self._versions = {}
        self._seq = 0

    def publish(self, mapping: Mapping[str, str]) -> BindingSetRef:
        self._seq += 1
        version = f"bind-{self._seq}"
        self._versions[version] = dict(mapping)
        return BindingSetRef(version=version)

    def resolve(self, ref: BindingSetRef, tool_name: str) -> str:
        table = self._versions.get(ref.version)
        if table is None or tool_name not in table:
            raise CapabilityError(f"no execution binding for {tool_name} in {ref.version}")
        return table[tool_name]

    def names(self, ref: BindingSetRef) -> frozenset[str]:
        return frozenset(self._versions.get(ref.version, {}))

    def mapping(self, ref: BindingSetRef) -> dict[str, str]:
        table = self._versions.get(ref.version)
        if table is None:
            raise CapabilityError(f"unknown binding set {ref.version}")
        return dict(table)


@runtime_checkable
class BindingSource(Protocol):
    """A provider that owns the registry its snapshots' binding refs resolve against."""

    @property
    def bindings(self) -> BindingRegistry: ...


class FixedCapabilityProvider:
    """Returns the configured snapshot. An empty set stays empty and is still a match."""

    def __init__(
        self,
        tools: Sequence[ToolSpec] = (),
        *,
        bindings: BindingRegistry | None = None,
        invoker_key: str = "default",
        contributions: tuple[object, ...] = (),
    ) -> None:
        for spec in tools:
            check_tool_spec(spec)
        self._tools = tuple(tools)
        self._bindings = bindings if bindings is not None else BindingRegistry()
        mapping = {spec.name: invoker_key for spec in self._tools}
        self._binding_ref = self._bindings.publish(mapping)
        self._snapshot = CapabilitySnapshot(
            version="cap-1",
            tools=self._tools,
            binding_ref=self._binding_ref,
            contributions=tuple(contributions),  # type: ignore[arg-type]
            policy_ref=CapabilityPolicyRef("fixed"),
        )

    @property
    def bindings(self) -> BindingRegistry:
        return self._bindings

    async def resolve(self, request: RunRequest, application: ApplicationSnapshot) -> Resolution:
        del request, application
        return Resolution(kind=MatchKind.MATCHED, snapshot=self._snapshot)


class ResolverChain:
    """Only NoMatch continues. Degraded, rejected, and config errors stop the chain."""

    def __init__(self, providers: Sequence[CapabilityProvider], *, bindings: BindingRegistry | None = None) -> None:
        self._providers = tuple(providers)
        if bindings is not None:
            self._bindings = bindings
        else:
            found: BindingRegistry | None = None
            for provider in self._providers:
                if isinstance(provider, BindingSource):
                    found = provider.bindings
                    break
            self._bindings = found if found is not None else BindingRegistry()

    @property
    def bindings(self) -> BindingRegistry:
        return self._bindings

    async def resolve(self, request: RunRequest, application: ApplicationSnapshot) -> Resolution:
        last = Resolution(kind=MatchKind.NO_MATCH, reason="empty chain")
        for provider in self._providers:
            result = await provider.resolve(request, application)
            if result.kind is MatchKind.NO_MATCH:
                last = result
                continue
            if result.snapshot is not None and isinstance(provider, BindingSource):
                source = provider.bindings
                if source is not self._bindings:
                    binding_ref = self._bindings.publish(source.mapping(result.snapshot.binding_ref))
                    result = replace(result, snapshot=replace(result.snapshot, binding_ref=binding_ref))
            return result
        return last


class CapabilitySession:
    """Per-run activated capability set. Mutations publish a new version; old snapshots stay intact."""

    def __init__(self, initial: CapabilitySnapshot, bindings: BindingRegistry) -> None:
        self._current = initial
        self._bindings = bindings
        self._seq = 1

    @property
    def bindings(self) -> BindingRegistry:
        return self._bindings

    def snapshot(self) -> CapabilitySnapshot:
        return self._current

    def publish(
        self,
        tools: Sequence[ToolSpec],
        *,
        extra_bindings: Mapping[str, str] | None = None,
        contributions: tuple[object, ...] | None = None,
    ) -> CapabilitySnapshot:
        for spec in tools:
            check_tool_spec(spec)
        previous = self._bindings.mapping(self._current.binding_ref)
        mapping = {spec.name: previous.get(spec.name, "default") for spec in tools}
        if extra_bindings:
            mapping.update(extra_bindings)
        binding_ref = self._bindings.publish(mapping)
        self._seq += 1
        snapshot = replace(
            self._current,
            version=f"cap-{self._seq}",
            tools=tuple(tools),
            binding_ref=binding_ref,
            contributions=tuple(contributions) if contributions is not None else self._current.contributions,  # type: ignore[arg-type]
        )
        self._current = snapshot
        return snapshot

    def activate(self, extra: Sequence[ToolSpec]) -> CapabilitySnapshot:
        names = {spec.name: spec for spec in self._current.tools}
        for spec in extra:
            names[spec.name] = spec
        return self.publish(tuple(names.values()))
