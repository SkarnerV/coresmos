# 设计缺口补齐记录（Harness、装配、压缩、取消）

补齐日期：2026-09-18。环境：macOS 15.6 arm64、Python 3.12.13。对照：[集成设计](agent-runtime-integration-design.md) §4–§14。

**结果：设计审查列出的 6 项缺口已在库内落地。** 完整测试 **108 passed**，覆盖率 **92%**；Ruff 与 Mypy 通过。契约套件现有 20 个可绑定场景（覆盖设计 §13 的 13 类行为，外加目标切换后继续、幂等重放等）。公司内部接入与 CI 矩阵仍不在本轮范围。

## 落地内容

**1. 可注入装配（§7、§12）** · `assemble_default` / `DefaultRuntime` 接受 `capability_provider`、`summaries` 与 `PortOverrides`。`ResolverChain` 只在 `NoMatch` 时继续，并从第一个 `BindingSource` 取得绑定表；`REJECTED` / `CONFIG_ERROR` 会使运行失败，而不是变成空工具集。宿主替换单个端口时仍走同一套记录、最终化、观测和关闭。

**2. 契约 Harness（§13）** · `run_contract_suite(factory)` 绑定同一套场景到默认实现或宿主工厂。场景覆盖：普通文本、工具后继续、记录失败、等待、能力版本钉扎、消息身份、空响应恢复、完成协议错误、停止/关闭、预算、运行隔离、观测失败、记录目标变更。工厂必须转发 `transcript_factory`、`observer` 等关键字，否则场景无法验证替换实现。

**3. 记录目标变更（§14 场景 3）** · 批次的调用与结果留在模型决策时的目标上；`next_record_target` 从下一步生效。目标从不合并。终止场景不再调用模型；若政策返回 CONTINUE，下一步只读取新目标的历史。

**4. 上下文压缩与投影（§7）** · 默认裁剪在没有工具组时继续丢掉最老的普通轮次（系统消息与当前最后一条除外）。`SummarizingCompressor` 通过 `Summarizer` 注入，参考实现 `ModelSummarizer` 走 `LowLevelModel`。投影按输入 key 复用；`ContextContributor` 可声明 `cache_key` 避免每轮打外部源。跨模块记录失败时使投影失效。

**5. 取消与期限（§11）** · `ExecutionLimits.deadline_seconds` 在运行开始时 `arm_deadline`；空闲超时改为 `StopReason.IDLE_TIMEOUT`，与墙钟 `DEADLINE` 分开。工具调用在任务上等待，停止会打断进行中的 invoke。适配器通过 `ToolInvocation.declare_external_work` / `report_progress` 声明不可立即取消的外部工作；取消终态带上这些引用。`ModelRequest.deadline_monotonic` 由步骤准备写入，OpenAI 适配器取适配器超时与运行期限的较小值。

**6. 公共表面清理** · 删除名不副实的 `invalidate()` / 未使用辅助函数；`BatchReceipt.recording_required=False` 有参考实现（`MemoryTranscript(unrecorded_tools=...)`）；`agent_runtime` 根导出覆盖运行入口、事件、流控与错误类型。`ToolInvoker.invoke` 现接收单个 `ToolInvocation`。

## 验证

| 检查 | 结果 |
| --- | --- |
| 契约套件 `run_contract_suite()` | 20 scenes passed |
| 完整 pytest（`tests` + `acceptance`） | **108 passed**，覆盖率 92% |
| Ruff check / format | 通过（55 个文件） |
| Mypy `src/agent_runtime` | Success: no issues found in 32 source files |

## 仍不属于本库的部分

- CI 平台矩阵需在包含本次改动的提交上重跑。
- 公司内部 I01–I07 适配与业务回归。
- v0.1 已声明不做的：工作流图、多 Agent、持久化检查点、跨 worker 锁、数据库事务。

下方保留 C01–C03 修复记录与原始验收报告。

---



修复日期：2026-09-18。环境：macOS 15.6 arm64、Python 3.12.13。被修复基线：`0e4ef2bfeb66d5fd13d62a98dcf47ea57639ebc7`。

**结果：C01–C03 已修复，本机 94 项用例全部通过（覆盖率 90%）。** 本轮新增的 10 项边界用例由 4 failed 变为全部通过；原有 84 项保持通过。CI 需在包含本次修复的提交上重跑。

## 修复内容

**C01 · 重放回执按稳定身份比较（`recording.py`）**

新增 `_receipt_identity()`，只比较 run、step、target、logical-op、calls 与消息 ID，排除 `created` 这类返回状态。首次提交与重放共用同一函数，`record_tool_calls()` 和 `record_tool_results()` 的回执核验也改为同一规则。错误 run/step/target、伪造 calls 和不同 payload 仍被拒绝。

**C02 · 每次模型尝试预留累计 token（`context/budget.py`、`core.py`、`pipelines/model.py`、`session.py`、`runner.py`）**

新增运行级 `ExecutionBudgetLedger`，与单次提示词预算 `TokenBudgetPolicy` 明确分离。`runner` 为每次运行建立一份账本，同时交给 `AgentLoop` 和 `RunSession`；模型管线在每次尝试发起请求前调用 `reserve()`，剩余不足即抛出 `BudgetExhaustedError`。重试不增加模型轮次，provider `TokenUsage` 仍单独记录。核心分配阶段保留 `raise_if_exhausted()`，用于拦截恢复入口已超预算的情况。

**C03 · 关闭操作纳入明确期限（`adapters/openai.py`）**

生产任务只负责创建与读取，流的释放由生成器统一持有，避免双重关闭和取消后的无界等待。新增 `_release_within()`，让取消生产任务和释放流各自受 close 预算约束；读取阶段正常耗尽但关闭超时时抛出可识别的 `AdapterError`，读取阶段本身已失败时不覆盖原错误。超时的流不再由适配器持有，`aclose()` 同样受期限约束，共享 client 不被关闭。

## 验证

| 检查 | 结果 | 材料 |
| --- | --- | --- |
| 本轮 10 项边界用例 | 10 passed | [输出](acceptance/round4/fix-boundary-results.txt) |
| 完整测试 | **94 passed**，覆盖率 90% | [输出](acceptance/round4/fix-full-results.txt)、[JUnit](acceptance/round4/fix-full-results.xml) |
| Ruff check / format / mypy | 通过；55 个文件格式、32 个源文件类型检查 | [输出](acceptance/round4/fix-lint-results.txt) |
| sdist → wheel 重建 | 通过 | [哈希](acceptance/round4/fix-artifact-hashes.txt) |
| 四种安装组合各 5 个 Harness 场景 | 全部通过（基础、OpenAI 2.45.0、OTel 1.43.0、组合） | [输出](acceptance/round4/fix-installed-scenes.txt) |
| OpenAI 2.54.0 / 2.45.0 HTTP mock | 各 3 个场景通过，各进入 handler 一次 | [locked](acceptance/round4/fix-sdk-locked.txt)、[floor](acceptance/round4/fix-sdk-floor.txt) |

补充边界核对（不计入用例数）：单轮成功运行只预留一次估算；上限放宽到两次估算时重试被允许；关闭超时后无残留任务且 `aclose()` 正常返回。

制品 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `agent_runtime-0.1.0-py3-none-any.whl` | `db9326352dafb1b657ed8ea230b0de25bc3dc65360cca0dbcbf16f4417094536` |
| `agent_runtime-0.1.0.tar.gz` | `1a7e7470aa816863aab8855518286a3ea2c98a63b248a16017521937cfa8a9a9` |

## 未完成项

- CI 平台矩阵需在包含本次修复与新增用例的提交上重跑。
- 构建依赖 Hatchling 的精确锁定未改变。
- 公司内部接入 I01–I07 仍需单独验收。

下方保留原始验收报告，不修改其结论与失败证据。

---

# Agent Runtime 最新提交独立验收报告

验收日期：2026-09-18（Asia/Shanghai）。本机：Windows、Python 3.12.13。
代码基线：`0e4ef2bfeb66d5fd13d62a98dcf47ea57639ebc7`。
依据：[集成设计](agent-runtime-integration-design.md)、[B01–B07 修复计划](agent-runtime-fix-plan.md)及[历史验收报告](agent-runtime-reacceptance-report.md)。

**结论：仍不通过公共 v0.1 验收。** 原有 84 项全部通过；本轮新增 10 项边界用例，6 项通过、4 项失败，完整结果为 **90 passed、4 failed**，覆盖率 90%。剩余问题归为 3 类，分别关联 B04、B07、B02。最新提交中的“B01–B07 已修复”结论需要收窄。

GitHub CI 平台矩阵已核实通过，历史报告中的“未取得 CI 实际结果”可以关闭。本轮新增用例尚未提交，因此不在该次远端 CI 的覆盖范围内。

## 1. 剩余问题

### C01 / P1：合法重放回执被当作不同身份拒绝（B04）

位置：`src/agent_runtime/recording.py:104–105`，由 `record_tool_results()` 的重放分支调用。

最小复现：先记录 tool calls 和 results；随后以相同参数、相同 logical-op 重放 calls，得到库返回的 `created=False` BatchReceipt；再使用该回执重放原 results。第二次 results 提交抛出 `ReceiptMismatchError`。

原因：`_replay()` 直接比较整个 `previous_receipt` dataclass，包含 `created`。原回执的 `created=True` 与重放回执的 `created=False` 不相等，但两者指向同一已提交操作。首次 results 写入已有逻辑按稳定身份比较，重放分支采用了不同规则。

影响：宿主按 calls → results 顺序重放已提交步骤时，合法恢复链路失败。新保存的结果回执集合解决了跨步骤返回错误消息的问题，但完整的幂等恢复仍不可用。

修复方向：使用统一的稳定身份比较，核验 run、step、target、logical-op、calls 和消息 ID，排除 `created` 这类返回状态；保留对错误身份和不同 payload 的拒绝。

复现用例：`test_replay_results_accepts_receipt_returned_by_replayed_calls`。

### C02 / P1：步骤内模型重试绕过累计 token 预算（B07）

位置：`src/agent_runtime/core.py:122–126,177`；`src/agent_runtime/pipelines/model.py:39–51`。

最小复现：输入 40 个字符，默认估算器计 10 个 prompt token；预留输出 1 token，运行上限设为 11，恢复允许 2 次尝试。第一次返回空结果或抛出可重试 AdapterError 后，模型实际均被调用 **2 次**，尽管预算只允许一次。

原因：`consume_estimated(prepared)` 只在进入模型步骤前执行一次；模型 pipeline 内部的重试循环没有再次检查或累计。修复计划要求“每次模型尝试”计入预算，当前实现实际按模型步骤计费。

影响：空响应恢复、请求失败重试仍可超出 `max_estimated_tokens`；`remaining_attempts == 0` 和恢复入口已超预算的拦截虽已生效，无法覆盖该路径。

修复方向：在每次真实模型调用前使用同一运行级预算账本预留本次估算值，剩余不足时抛出 `BudgetExhaustedError`。重试仍不增加模型轮次，也不要重复加入 provider usage。

复现用例：`test_recovery_attempt_reserves_its_own_token_budget[empty/error]`，两项均失败。

### C03 / P1：SDK 流关闭仍可无限等待（B02）

位置：`src/agent_runtime/adapters/openai.py:191–198,215–222`。

最小复现：SDK 正常返回文本并结束迭代，随后其 `aclose()` 等待资源释放；adapter timeout 设为 50 ms。等待 300 ms 后 Runtime 收集任务仍未完成，只有测试主动释放 fake SDK 资源后才返回。

原因：生产任务的 `_release(stream)` 位于 `asyncio.timeout()` 外；消费侧取消生产任务后的 `gather()` 和补偿 `_release(stream)` 也没有关闭期限。正常结束或超时后的清理仍会落入无界等待。

影响：配置的 adapter timeout 无法保证调用返回，运行可能停在清理阶段；外层 RunScope 尚未得到控制权，不能依靠后续 scope 清理解决当前等待。

修复方向：将关闭操作纳入明确期限，并在清理超时后返回可识别的错误；处理好生产任务取消与流关闭的所有权，避免无期限等待。保持共享 client 不被关闭。

复现用例：`test_adapter_deadline_also_bounds_stream_close`。该用例会在 finally 中释放测试资源，未留下常驻任务。

## 2. B01–B07 复验状态

| 编号 | 本轮结论 | 证据 |
| --- | --- | --- |
| B01 | 通过 | 已安装新 wheel 的 OpenAI 2.45.0、2.54.0，文本、工具 schema、嵌套历史参数共 6 个真实 SDK + MockTransport 场景均通过 |
| B02 | 部分修复 | 原有创建超时和首个 delta 后超时用例通过；关闭边界仍有 C03 |
| B03 | 原问题通过 | 初始化快照异常后的 observer 任务泄漏回归通过 |
| B04 | 部分修复 | 原有错误 run/step/target 拒绝、跨步骤结果消息 ID 重放用例通过；合法重放链新增 C01 |
| B05 | 通过本轮复验 | WAIT 发布前已提交；WAIT 最终化失败只发布失败；终态后立即关闭不会丢失状态 |
| B06 | 通过本轮复验 | 成功、失败、RunControl 用户停止、WAIT、CompletedEntry、WAIT 最终化失败均观察到对应已提交终态 |
| B07 | 部分修复 | 零剩余尝试、恢复入口已超预算均阻止请求；步骤内重试仍有 C02 |

本轮 6 项新增正向终态用例同时校验：宿主收到终态时已提交、关闭后状态保持、observer 收到一次对应终态且可读到已提交状态。

## 3. 验证结果与证据

| 检查 | 结果 | 材料 |
| --- | --- | --- |
| 提交自带完整测试 | 84 passed，覆盖率 90% | [基线输出](acceptance/round4/baseline-results.txt) |
| 新增边界用例 | 6 passed、4 failed | [用例](acceptance/round4/test_fix_boundaries.py)、[输出](acceptance/round4/boundary-results.txt) |
| 加入新增用例后的完整测试 | 90 passed、4 failed，覆盖率 90% | [完整输出](acceptance/round4/full-results.txt)、[JUnit](acceptance/round4/full-results.xml) |
| Ruff check / format / mypy | 通过；55 个 Python 文件格式、32 个源文件类型检查 | [检查输出](acceptance/round4/lint-results.txt) |
| sdist → wheel | 重建成功 | [哈希与源码清单](acceptance/round4/source-manifest.json) |
| 基础安装，无 extras | 5 个公开 Harness 场景通过 | [结果](acceptance/round4/installed-base.txt) |
| OpenAI 下界 2.45.0 | 5 个 Harness 场景通过 | [结果](acceptance/round4/installed-openai-floor.txt) |
| OTel API 下界 1.43.0 | 5 个 Harness 场景通过 | [结果](acceptance/round4/installed-otel-floor.txt) |
| OpenAI 2.54.0 + OTel API 1.44.0 | 5 个 Harness 场景通过 | [结果](acceptance/round4/installed-combined.txt) |
| OpenAI 2.45.0 HTTP mock | 3 个请求场景通过，各进入 HTTP handler 一次 | [结果](acceptance/round4/sdk-floor.txt) |
| OpenAI 2.54.0 HTTP mock | 同上 | [结果](acceptance/round4/sdk-locked.txt) |
| CI 矩阵及 extras | 5 个 job 全部 success | [GitHub run](https://github.com/SkarnerV/coresmos/actions/runs/35243101597)、[原始结果](acceptance/round4/ci-results.json) |

CI 已核对 `headSha` 等于本轮完整提交 SHA，包含 Ubuntu Python 3.12、3.13、3.14，Windows Python 3.12，以及 Ubuntu Python 3.12 extras job。新增边界用例需随修复提交后再进入 CI。

安装验证复用上一轮四个独立环境，强制替换为本轮新构建 wheel；从系统临时目录以 `python -I` 运行原公开验证脚本。已逐文件核对源码、wheel 和四个环境中的 32 个 `.py` 加 `py.typed`，33 个文件全部匹配。SDK 请求使用本地 HTTPX MockTransport，未调用真实模型服务。

制品 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `agent_runtime-0.1.0-py3-none-any.whl` | `4e3f6e078f92e738416faed730cb9d35c3ee395ac1a46eef9aa89ee397e221f9` |
| `agent_runtime-0.1.0.tar.gz` | `e17c89f728b2542d100811adae230fd5ce0e0b9a64d886d63de77be194032e4d` |

## 4. 复现命令与验收范围

在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider acceptance/round4/test_fix_boundaries.py --tb=short
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --tb=short --cov=agent_runtime --cov-report=term-missing
.\.venv\Scripts\ruff.exe check src tests examples acceptance
.\.venv\Scripts\ruff.exe format --check src tests examples acceptance
.\.venv\Scripts\mypy.exe src/agent_runtime
uv build --offline --out-dir acceptance/round4/dist
gh run view 35243101597 --json databaseId,headSha,status,conclusion,url,jobs
```

安装制品复验示例（其余环境分别替换为 `base-env`、`otel-env`、`combined-env`）：

```powershell
uv pip install --offline --no-deps --reinstall-package agent-runtime --python acceptance/round2/openai-env/Scripts/python.exe acceptance/round4/dist/agent_runtime-0.1.0-py3-none-any.whl
Push-Location $env:TEMP
& C:\codebase\coresmos\acceptance\round2\openai-env\Scripts\python.exe -I C:\codebase\coresmos\acceptance\round2\installed_scenes.py
& C:\codebase\coresmos\acceptance\round2\openai-env\Scripts\python.exe -I C:\codebase\coresmos\acceptance\round2\sdk_transport_probe.py
Pop-Location
```

本轮新增验收用例、日志和报告，未修改运行库实现或新增依赖。历史报告保留。公司内部接入 I01–I07 仍需单独验收；构建依赖 Hatchling 的精确锁定未在本提交中改变。

关闭本轮验收需要：修复 C01–C03、通过本轮 94 项用例，并在包含新增用例的提交上重跑 CI 和相应安装制品验证。
