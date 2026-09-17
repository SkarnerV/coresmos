# Adapter guide

This document describes how to plug a host into `agent-runtime` without changing `AgentLoop`.

## Public surface

Install the library, then assemble a runtime from four replaceable low-level ports:

- `LowLevelModel` — one request, a stream of `ModelStepEvent`, no recovery of its own
- `ToolInvoker` — one call, one `InvocationOutcome`
- `TranscriptPort` — committed facts, receipts, and snapshots
- `ApplicationStatePort` — versioned snapshots and required state commits

Default implementations (`ScriptedModel`, `ScriptedInvoker`, `MemoryTranscript`, `MemoryApplicationState`) are for tests and the first closed loop. Hosts replace them at `assemble_default(...)`.

```python
from agent_runtime import assemble_default, RunRequest, RunControl, ModelEntry, RecordTarget

runtime = assemble_default(
    model=host_model,
    invoker=host_invoker,
    tools=exposed_tools,
    transcript_factory=host_transcript_factory,
    application_factory=host_application_factory,
    contributors=(host_contributor,),
    compressor=host_compressor,
    observer=host_observer,
    budget=host_budget,
)
stream = runtime.run(request, RunControl())
```

`DefaultRuntime` / `assemble_default` also accept `result_policy`, `completion_policy`, `bindings`, and `idle_timeout`. Do not subclass the loop to inject these.

## Resource ownership

- Shared clients, connection pools, and tracers belong to the host or assembly root.
- Per-run tasks, model streams, and tool waits belong to `RunScope` and are closed with a bounded timeout.
- `OpenAIChatCompletionsModel(owns_client=False)` never closes a shared SDK client. Set `owns_client=True` only for a client created for that adapter instance.
- User stop, host cancel, consumer close, and deadline stay distinct on `RunControl`. Cleanup must not convert host `CancelledError` into `RunFailed`.

## Call / result barrier

The default tool pipeline is:

1. Validate the whole batch against the current capability snapshot.
2. Record tool calls and publish the committed snapshot.
3. Invoke tools in order.
4. Record results and required application state.
5. Only then return a `FlowDecision`.

A recording or required-state failure after a successful external call must not replay that call. Ordinary tool errors stay model-visible. `WAIT` outranks `FINISH`, which outranks `CONTINUE`. `ToolBatchEntry.continue_after=False` ends the run after tools unless `FINISH` / `WAIT` already applied.

## Errors and cancel

| Failure | Effect |
| --- | --- |
| Empty / reasoning-only model output | Request-level retry; does not consume a new model round |
| Partial output then `AdapterError` | Fail the run unless `RecoveryBudget.retry_partial_output=True` |
| Missing / trailing completion events | `CompletionProtocolError`, run fails |
| Illegal tool-result receipt | `ReceiptMismatchError`; previous snapshot is unchanged |
| User stop during `prepare()` | Cancels the in-flight provider / application await |
| Observer throw / block / full queue | Dropped or isolated; tools are not replayed |

## Reference model adapter

`agent_runtime.adapters.openai.OpenAIChatCompletionsModel` is the Chat Completions reference path (extra `openai`). It:

- normalizes text deltas, fragmented tool arguments, finish reason, and provider usage
- sets SDK `max_retries=0` so only the model pipeline retries
- times out the stream and closes that stream on run close

Protocol tests live in `tests/test_t13_openai.py`. A live smoke example is `examples/openai_chat_completions_smoke.py`; it is not part of the default CI gate.

## Observability

Pass any `Observer` to `assemble_default`. The runtime wraps it in `IsolatedObserver` (bounded queue). `OpenTelemetryObserver` depends only on `opentelemetry-api`; the host configures the SDK. Importing `agent_runtime` does not install logging or tracing global state.

## Contract harness

`agent_runtime.testing.harness` binds a factory `(model, invoker, tools, **kwargs) -> Runtime` to synthetic scenes. The module does not import pytest. Use it for the default runtime or a host adapter:

```python
from agent_runtime.testing.harness import scene_tool_then_text, assert_success

result = await scene_tool_then_text(host_factory)
assert_success(result)
```

Scenes included: plain text, tool-then-text, tool-updated capabilities, WAIT, and input-record failure. Independent regressions for recording, streaming, cancel, budget, and partial output live in `acceptance/test_runtime_contracts.py` and are collected by pytest.

## Internal adapters

Company Transcript, invoker, capability, and model adapters should satisfy the ports above and run the same harness. They must not import Unibot orchestrators or treat nearby reference repositories as a verified baseline.
