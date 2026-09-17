from __future__ import annotations

import pytest

from agent_runtime.application import MemoryApplicationState
from agent_runtime.capabilities import (
    CapabilitySession,
    FixedCapabilityProvider,
    ResolverChain,
    validate_tool_call,
)
from agent_runtime.contracts import (
    ApplicationSnapshot,
    MatchKind,
    Message,
    ModelConfig,
    ModelEntry,
    RecordTarget,
    Resolution,
    Role,
    RunControl,
    RunFailed,
    RunRequest,
    ToolCall,
    ToolSpec,
)
from agent_runtime.exceptions import CapabilityError, SchemaValidationError
from agent_runtime.runner import assemble_default
from agent_runtime.testing.models import ScriptedModel, ScriptedTurn
from agent_runtime.testing.scenarios import collect_run
from agent_runtime.testing.tools import ScriptedInvoker


def _request() -> RunRequest:
    return RunRequest(
        run_id="r",
        input_items=(Message(role=Role.USER, content="hi"),),
        record_target=RecordTarget("t"),
        entry=ModelEntry(),
        model=ModelConfig(),
    )


ECHO = ToolSpec(
    name="echo",
    description="echo",
    parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
)


class _NoMatch:
    async def resolve(self, request: RunRequest, application: ApplicationSnapshot):  # noqa: ANN201
        del request, application
        from agent_runtime.contracts import Resolution

        return Resolution(kind=MatchKind.NO_MATCH, reason="skip")


class _Degraded:
    async def resolve(self, request: RunRequest, application: ApplicationSnapshot):  # noqa: ANN201
        del request, application
        from agent_runtime.contracts import Resolution

        return Resolution(kind=MatchKind.DEGRADED, reason="partial")


class _Matched:
    def __init__(self, provider: FixedCapabilityProvider) -> None:
        self.provider = provider

    async def resolve(self, request: RunRequest, application: ApplicationSnapshot):  # noqa: ANN201
        return await self.provider.resolve(request, application)


class _Rejected:
    async def resolve(self, request: RunRequest, application: ApplicationSnapshot):  # noqa: ANN201
        del request, application
        return Resolution(kind=MatchKind.REJECTED, reason="no permission")


class _ConfigError:
    async def resolve(self, request: RunRequest, application: ApplicationSnapshot):  # noqa: ANN201
        del request, application
        return Resolution(kind=MatchKind.CONFIG_ERROR, reason="broken catalog")


async def test_matched_stops_chain_and_degraded_is_not_nomatch() -> None:
    app = await MemoryApplicationState(RecordTarget("t")).current(RecordTarget("t"))
    request = _request()
    matched = FixedCapabilityProvider((ECHO,))
    chain = ResolverChain((_NoMatch(), _Matched(matched), _NoMatch()))
    result = await chain.resolve(request, app)
    assert result.kind is MatchKind.MATCHED
    assert result.snapshot is not None
    degraded_chain = ResolverChain((_Degraded(), _Matched(matched)))
    degraded = await degraded_chain.resolve(request, app)
    assert degraded.kind is MatchKind.DEGRADED


async def test_empty_fixed_stays_empty() -> None:
    provider = FixedCapabilityProvider(())
    result = await provider.resolve(
        _request(), await MemoryApplicationState(RecordTarget("t")).current(RecordTarget("t"))
    )
    assert result.kind is MatchKind.MATCHED
    assert result.snapshot is not None
    assert result.snapshot.tools == ()


async def test_publish_does_not_mutate_old_snapshot() -> None:
    provider = FixedCapabilityProvider((ECHO,))
    snapshot = (
        await provider.resolve(_request(), await MemoryApplicationState(RecordTarget("t")).current(RecordTarget("t")))
    ).snapshot
    assert snapshot is not None
    session = CapabilitySession(snapshot, provider.bindings)
    extra = ToolSpec(
        name="other",
        description="other",
        parameters={"type": "object", "properties": {}},
    )
    updated = session.activate((extra,))
    assert snapshot.version != updated.version
    assert snapshot.tools == (ECHO,)
    assert {spec.name for spec in updated.tools} == {"echo", "other"}
    assert "secret" not in snapshot.binding_ref.version


async def test_schema_and_params_fail_clearly() -> None:
    bad = ToolSpec(name="bad", description="bad", parameters={"$schema": "https://json-schema.org/draft-04/schema"})
    with pytest.raises(SchemaValidationError):
        FixedCapabilityProvider((bad,))
    with pytest.raises(SchemaValidationError):
        validate_tool_call(ECHO, ToolCall(call_id="c", name="echo", arguments={"text": 1}))
    with pytest.raises(CapabilityError):
        provider = FixedCapabilityProvider((ECHO,))
        result = await provider.resolve(
            _request(), await MemoryApplicationState(RecordTarget("t")).current(RecordTarget("t"))
        )
        assert result.snapshot is not None
        provider.bindings.resolve(result.snapshot.binding_ref, "missing")


async def test_rejected_and_config_error_do_not_fall_through() -> None:
    app = await MemoryApplicationState(RecordTarget("t")).current(RecordTarget("t"))
    request = _request()
    matched = FixedCapabilityProvider((ECHO,))
    rejected = await ResolverChain((_Rejected(), _Matched(matched))).resolve(request, app)
    assert rejected.kind is MatchKind.REJECTED
    broken = await ResolverChain((_ConfigError(), _Matched(matched))).resolve(request, app)
    assert broken.kind is MatchKind.CONFIG_ERROR


async def test_resolver_chain_is_injectable_and_uses_its_bindings() -> None:
    provider = FixedCapabilityProvider((ECHO,))
    chain = ResolverChain((_NoMatch(), provider))
    runtime = assemble_default(
        model=ScriptedModel([ScriptedTurn(text="ok")]),
        invoker=ScriptedInvoker(),
        capability_provider=chain,
    )
    events = await collect_run(_request(), RunControl(), runtime)
    assert any(event.__class__.__name__ == "RunSucceeded" for event in events)


async def test_rejected_resolution_fails_the_run() -> None:
    runtime = assemble_default(
        model=ScriptedModel([ScriptedTurn(text="nope")]),
        invoker=ScriptedInvoker(),
        capability_provider=ResolverChain((_Rejected(),)),
    )
    events = await collect_run(_request(), RunControl(), runtime)
    failed = next(event for event in events if isinstance(event, RunFailed))
    assert failed.error_type == CapabilityError.__name__
    assert "rejected" in failed.message
