# Adapter guide

This document describes how to plug a host into `agent-runtime` without changing `AgentLoop`.

## Public surface

Install the library, then assemble a runtime from four replaceable low-level ports:

- `LowLevelModel` — one request, a stream of `ModelStepEvent`, no recovery of its own
- `ToolInvoker` — one `ToolInvocation`, one `InvocationOutcome`. Report progress and non-cancellable external work through the invocation; do not assume the runtime can undo work that already left the process.
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

`DefaultRuntime` / `assemble_default` also accept `capability_provider`, `ports`, `summaries`, `result_policy`, `completion_policy`, `bindings`, `idle_timeout`, and `phase_idle_timeout`. Pass a `ResolverChain` as `capability_provider` when several catalogs should compete; only an explicit `NoMatch` continues the chain. Replace one phase with `PortOverrides` instead of subclassing the loop. Do not subclass `AgentLoop` to inject these.

## Resource ownership

- Shared clients, connection pools, and tracers belong to the host or assembly root.
- Per-run tasks, model streams, and tool waits belong to `RunScope` and are closed with a bounded timeout.
- `OpenAIChatCompletionsModel(owns_client=False)` never closes a shared SDK client. Set `owns_client=True` only for a client created for that adapter instance.
- User stop, host cancel, consumer close, idle timeout, and the run deadline stay distinct on `RunControl`. `ExecutionLimits.deadline_seconds` arms the wall-clock deadline when the run starts. Cleanup must not convert host `CancelledError` into `RunFailed`.

## Call / result barrier

The default tool pipeline is:

1. Validate the whole batch against the current capability snapshot.
2. Record tool calls and publish the committed snapshot.
3. Invoke tools in order.
4. Record results and required application state.
5. Only then return a `FlowDecision`.

A recording or required-state failure after a successful external call must not replay that call. Ordinary tool errors stay model-visible. `WAIT` outranks `FINISH`, which outranks `CONTINUE`. `ToolBatchEntry.continue_after=False` ends the run after tools unless `FINISH` / `WAIT` already applied.

`InvocationOutcome.next_record_target` takes effect from the **next** step. The current batch's calls and results stay on the target the model decided against. After a switch, the next `prepare()` reads only the new target; targets are never merged. The v0.1 scene for this is target-change-then-terminate; continuing after a switch is supported and leaves the new target's history as the model view.

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

`agent_runtime.testing.harness` binds a factory `(model, invoker, tools, **kwargs) -> Runtime` to the design's contract scenes. The module does not import pytest. A host adapter proves it satisfies the runtime by forwarding the keyword arguments those scenes pass (`transcript_factory`, `observer`, `capability_provider`, and so on) and calling `run_contract_suite(factory)`:

```python
from agent_runtime.testing.harness import run_contract_suite, scene_tool_then_text, assert_success

await run_contract_suite(host_factory)
result = await scene_tool_then_text(host_factory)
assert_success(result)
```

The suite covers the rows in the integration design §13: plain text, tool-then-text, record failure, wait, capability version pinning, message identity, empty-response recovery, completion protocol errors, stop/close, budget, isolation, observer failure, and record-target change. Independent extra regressions live in `acceptance/test_runtime_contracts.py`.

## Internal adapters

Company Transcript, invoker, capability, and model adapters should satisfy the ports above and run the same harness. They must not import Unibot orchestrators or treat nearby reference repositories as a verified baseline.
