# Upgrade notes

## 0.1.0

First public `agent-runtime` release.

- Package: `agent-runtime`, import `agent_runtime`, Python `>=3.12`
- Direct dependencies: `jsonschema>=4.26,<5`, `referencing>=0.37,<0.38`
- Optional extras: `openai` (`openai>=2.45,<3`), `otel` (`opentelemetry-api>=1.43,<2`)
- Public assembly: `assemble_default` / `DefaultRuntime`
- Default policies: `WAIT > FINISH > CONTINUE`; ordinary tool errors are model-visible; partial model output does not retry unless `RecoveryBudget.retry_partial_output=True`

There is no previous public API. Subsequent 0.1.x releases will record breaking contract changes here.

### Assembly and invoker contract (this revision)

- `assemble_default` accepts `capability_provider`, `ports` (`PortOverrides`), and `summaries`. A `ResolverChain` can be passed as the capability provider; only `NoMatch` continues the chain.
- `ToolInvoker.invoke` takes a single `ToolInvocation`. The previous `(call, binding, control)` signature is not called.
- `StopReason.IDLE_TIMEOUT` is raised for inter-event silence. A wall-clock run deadline is `ExecutionLimits.deadline_seconds` / `RunControl.arm_deadline` and reports `StopReason.DEADLINE`.
- `BatchReceipt.recording_required=False` means the recorder must not write a model-visible call/result pair. `MemoryTranscript(unrecorded_tools=...)` is the reference.
- Context compression may drop oldest plain turns after whole tool-call groups. A `SummarizingCompressor` can be injected; its summarizer is a `Summarizer`, typically `ModelSummarizer`.

### Verified extras combinations

| Install | Expected |
| --- | --- |
| `agent-runtime` | Import, synthetic loop, contract tests |
| `agent-runtime[openai]` | Reference Chat Completions adapter importable |
| `agent-runtime[otel]` | `OpenTelemetryObserver` importable; host still configures the SDK |
| `agent-runtime[openai,otel]` | Both extras together |

Lower bounds above are the declared compatible floor. The locked development extra set is `uv.lock`.
