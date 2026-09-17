# Agent Runtime 第三轮验收报告（B01–B07 修复后）

验收日期：2026-09-17。环境：macOS 15.6 arm64、Python 3.12.13。修复前代码基线：`648a5b7dfa49c594f4b615c2ff3d23bcc793ed6a`。依据：[修复计划](agent-runtime-fix-plan.md)。

**结论：B01–B07 已修复并通过本机公共门槛检查；完整公共 v0.1 仍不标记通过。** 本机 84 项 pytest、Ruff、format、mypy、四种安装组合、两个 OpenAI 版本的 MockTransport 场景全部通过。GitHub CI 矩阵（Linux 3.12/3.13/3.14 与 Windows 3.12）未在本环境实际执行。

本报告接续下方第二轮记录和[第一轮验收报告](agent-runtime-acceptance-report.md)。I01–I07 公司内部接入仍不在本次公共库范围内。

**1. 修复对应关系**

| 编号 | 修复 |
| --- | --- |
| B04 回执重放 | `_CommittedOp` 保存完整结果回执和 run/step/target/logical-op 身份。首次提交与重放共用身份、payload；提供 `previous_receipt` 时一并核验。重放返回已保存回执，不再按 `call_id` 全局搜索。 |
| B05 WAIT 终态 | 向外发布 `RunWaiting` 前完成 `WAITING` 最终化。成功、失败、取消、WAIT、CompletedEntry 统一为最终化 → 观测 → 对外事件。最终化失败不发布成功或等待终态。 |
| B07 执行预算 | `remaining_attempts == 0` 时禁止模型请求。分配阶段检查恢复入口累计 token；每次模型尝试计入 prompt、工具 schema、调用参数和预留输出。Provider `TokenUsage` 单独记录。 |
| B03 初始化清理 | observer、scope、应用快照和能力解析放入统一 `try/finally`。初始化异常后取消并等待已创建任务，不遗留 observer-pump。 |
| B06 终态观测 | `RunSucceeded`、`RunFailed`、`RunCancelled`、`RunWaiting` 经统一发布函数进入 observer 队列。观察者异常、阻塞和队列满不改变运行控制结果。 |
| B01 JSON 转换 | 适配器边界对工具 schema 和嵌套参数调用 `thaw_json()`。 |
| B02 流超时 | 固定生产任务持有 SDK 流和 `asyncio.timeout`；每次读取使用剩余 deadline。取消时等待底层流退出。共享 client 不会被误关闭。 |

**2. 本机验证结果**

| 检查 | 结果 |
| --- | --- |
| B01–B07 回归 | 10 passed（含预算参数化的两项） |
| 完整 pytest | **84 passed** |
| Ruff check / format | 通过；54 个 Python 文件格式通过 |
| mypy | 通过；32 个源文件 |
| 基础包独立安装 | 通过；源码目录外 `-I` 运行 5 个公开 Harness 场景；无 pytest、无 extra |
| openai extra（锁文件 2.54.0） | 通过；5 个场景 |
| openai==2.45.0 下界 | 通过；5 个场景 |
| otel extra（锁文件 1.44.0） | 通过；5 个场景 |
| openai+otel 组合 | 通过；OpenAI 2.54.0、OTel API 1.44.0 |
| SDK MockTransport 2.54.0 | 纯文本、工具 schema、嵌套历史均通过，各 1 次 HTTP 调用 |
| SDK MockTransport 2.45.0 | 同上，全部通过 |
| CI 平台矩阵 | **未执行**（Linux 3.12/3.13/3.14、Windows 3.12） |

**3. 命令**

```bash
uv run pytest -p no:cacheprovider --tb=short
uv run pytest -p no:cacheprovider acceptance/round2/test_reacceptance_contracts.py --tb=short
uv run ruff check src tests examples acceptance
uv run ruff format --check src tests examples acceptance
uv run mypy src/agent_runtime
uv build --out-dir acceptance/round2/dist
```

从源码目录外验证已安装制品（示例）：

```bash
python -I acceptance/round2/installed_scenes.py
python -I acceptance/round2/sdk_transport_probe.py
```

**4. 制品哈希（SHA-256）**

| 制品 | SHA-256 |
| --- | --- |
| `acceptance/round2/dist/agent_runtime-0.1.0-py3-none-any.whl` | `FF65AA2648251098299B5807D91C8D9255582EDBBF3B783766141719B2E19707` |
| `acceptance/round2/dist/agent_runtime-0.1.0.tar.gz` | `19A5479A859BBCE1D651CF86034A638C3991DD4AA509C417B1AD9278BDAECAA4` |

wheel 由本次重建的 sdist 构建。

**5. 复验材料**

- [B01–B07 契约用例](acceptance/round2/test_reacceptance_contracts.py)
- [全套结果：84 passed](acceptance/round2/full-results.txt) / [JUnit](acceptance/round2/full-results.xml)
- [契约 10 passed](acceptance/round2/contract-results.txt)
- [SDK 2.54.0](acceptance/round2/sdk-locked-results.txt)、[SDK 2.45.0](acceptance/round2/sdk-floor-results.txt)
- [基础包场景](acceptance/round2/installed-base-env.txt)、[openai](acceptance/round2/installed-openai-env.txt)、[openai 2.45.0](acceptance/round2/installed-openai-floor-env.txt)、[otel](acceptance/round2/installed-otel-env.txt)、[组合 extra](acceptance/round2/installed-combined-env.txt)

**6. 未完成项**

- 未取得 Linux Python 3.12/3.13/3.14 与 Windows Python 3.12 的 CI 实际结果。
- Hatchling 隔离构建依赖仍是版本范围，未见精确锁定。
- 内部接入 I01–I07 继续单独验收。

因此：**不在本轮标记完整公共 v0.1 通过。** 取得 CI 矩阵实际结果后才能关闭该项。

---

# Agent Runtime 第二轮验收报告

验收日期：2026-09-17。环境：Windows、Python 3.12.13。代码基线：`62ffa5ddabd43ff8f272c18c76bd020a574343b1`。

**结论：上一轮 11 个复现用例全部通过，但完整 v0.1 仍不通过验收。** 本轮新增边界用例发现 7 类问题，最终全套结果为 **74 passed、10 failed**。其中真实 OpenAI SDK 的工具请求在 HTTP 发送前失败，回执重放、WAIT 最终化和生命周期仍有阻断问题。

本报告接续[第一轮验收报告](C:/codebase/coresmos/agent-runtime-acceptance-report.md)，保留历史结论。依据仍为集成设计、技术选型和 T01–T15 开发任务。I01–I07 公司内部接入不在本次公共库验收范围内。

**1. 已确认的修复与交付进展**

| 项目 | 本轮结果 |
| --- | --- |
| 上一轮 A01–A10 对应的 11 个复现用例 | 全部通过；A02 的首次提交校验已补齐，但重放路径仍有 B04 问题 |
| 本轮开始时的测试基线 | 74 passed，包含上述 11 项；基线覆盖率 89% |
| 加入本轮边界用例后的完整套件 | 74 passed、10 failed，无收集或导入错误 |
| Ruff / format / mypy | 全部通过；54 个 Python 文件格式通过，32 个源文件类型检查通过 |
| wheel / sdist 重建 | 通过，wheel 由新 sdist 构建 |
| 基础包独立安装 | 通过；未安装 pytest 和两个可选 extra，源码目录外使用 `-I` 运行 |
| openai 单独安装 | 通过；验证声明下界 `openai==2.45.0` |
| otel 单独安装 | 通过；验证声明下界 `opentelemetry-api==1.43.0` |
| 两个 extra 组合安装 | 通过；主 SDK 版本为锁文件中的 OpenAI 2.54.0、OTel API 1.44.0 |
| 安装制品后的公开 Harness 场景 | 四个安装环境各 5 个场景通过：纯文本、工具后文本、能力更新、WAIT、输入记录失败 |
| 真实 SDK + 本地 HTTP mock | 两个 OpenAI 版本均为纯文本通过、工具 schema 和嵌套历史参数失败，见 B01 |

新增的观测模块、OpenAI 适配器、Harness、adapter-guide、upgrade-notes 和 CI 平台矩阵确实已经提交，不能再按上一轮“文件缺失”评价。但“存在实现、可以导入、能够安装”和“契约验收通过”是不同结果。

**2. 本轮问题**

**B01 · P1：参考适配器无法发送常规工具 schema 或包含嵌套参数的历史。**

- 位置：[adapters/openai.py:203](C:/codebase/coresmos/src/agent_runtime/adapters/openai.py:203)、[adapters/openai.py:220](C:/codebase/coresmos/src/agent_runtime/adapters/openai.py:220)。关联 T13。
- 契约对象将嵌套 JSON 冻结为 MappingProxyType / tuple；适配器仅做顶层 `dict(...)` 转换。`ECHO_TOOL` 的 properties 仍然包含 mappingproxy，历史工具参数中的嵌套对象同样未还原。
- 复现：普通工具 schema 的 payload 不能进行 JSON 编码；嵌套历史参数在 `request_payload()` 内直接抛出 TypeError。
- 使用已安装 wheel、真实 OpenAI 2.54.0 和 2.45.0、HTTPX MockTransport 再验证：纯文本对照请求成功，工具 schema 返回 `AdapterError: Object of type mappingproxy is not JSON serializable`，嵌套历史返回同信息的 TypeError；两个失败场景的 HTTP 调用次数均为 0。
- 修复要求：在协议边界递归还原 JSON 数据，包括 schema、工具参数和嵌套数组；在真实 SDK 请求编码路径上增加回归，而非只检查 payload 顶层字段。
- 用例：`test_openai_tool_schema_is_json_serializable`、`test_openai_history_supports_nested_tool_arguments`；另见 `sdk_transport_probe.py`。

**B02 · P1：模型输出第一段文本后，适配器 timeout 在 Runtime 中失效。**

- 位置：[adapters/openai.py:158](C:/codebase/coresmos/src/agent_runtime/adapters/openai.py:158)。关联 T03、T13。
- 复现：设置适配器 timeout 为 0.05 秒，底层先输出一段文本，再阻塞下一 chunk。在真实默认 Runtime 装配下，0.3 秒后仍未退出，必须手动释放阻塞。
- 原因：`asyncio.timeout` 跨越异步生成器的 yield，而 Runtime 每次读取下一事件使用新任务；超时绑定的是首次进入上下文的任务，后续等待没有得到相同的保护。
- 修复要求：让超时所有权与实际等待任务一致，例如对每次读取执行带剩余期限的等待，或由生命周期固定的生产任务承担整个流。验证首包等待、包间静默、取消及关闭，不能只测 SDK create 阶段超时。
- 用例：`test_openai_timeout_covers_wait_after_first_delta_in_runtime`。

**B03 · P1：初始应用快照读取失败会遗留 observer-pump 任务。**

- 位置：[runner.py:92](C:/codebase/coresmos/src/agent_runtime/runner.py:92)、[runner.py:137](C:/codebase/coresmos/src/agent_runtime/runner.py:137)。关联 T03、T11、T12。
- 复现：应用的首次 `current()` 抛出异常；模型调用次数为 0，运行结束后仍有一个 `observer-pump` 在等待队列。
- 原因：观察者任务已启动，但初始快照读取、能力解析和装配位于 try/finally 之前，失败时不进入资源清理。
- 修复要求：从创建首个本次运行资源开始保护整个初始化、运行及最终化过程；初始化失败、停止和取消都必须清理已创建的资源。
- 用例：`test_initialization_failure_does_not_leak_observer_task`。测试在断言后主动回收泄漏任务，避免污染后续用例。

**B04 · P1：回执重放绕过身份校验，并可能返回其他步骤或目标的消息 ID。**

- 位置：[recording.py:324](C:/codebase/coresmos/src/agent_runtime/recording.py:324)、[recording.py:330](C:/codebase/coresmos/src/agent_runtime/recording.py:330)。关联 T04；属于上一轮 A02 的剩余问题。
- 复现一：有效结果已经提交后，复用同一个 logical_op_id 和 payload，但传入错误 run / step / target，调用仍然成功；重放分支在新补的回执核验之前返回。
- 复现二：同一运行的两个步骤、两个目标都出现 call_id=c1；重放第二步结果，本应返回 `msg:round2:4`，实际返回第一步的 `msg:round2:2`。
- 原因：重放仅校验 payload，并在全部消息中按 call_id 查找首个匹配，而不是保存和返回该逻辑操作实际提交的完整回执。
- 修复要求：首次提交与重放使用相同的身份核验；保存该操作全部已提交结果回执，重放时保持消息身份一致，不能跨步骤或目标重新搜索。
- 用例：`test_result_replay_revalidates_run_step_and_target`、`test_result_replay_returns_original_message_of_same_step_and_target`。

**B05 · P1：WAIT 事件先于运行状态提交，消费者交接后关闭会丢失最终化。**

- 位置：[runner.py:159](C:/codebase/coresmos/src/agent_runtime/runner.py:159)。关联 T05、T11、T14。
- 复现：工具返回 WAIT；宿主收到 `RunWaiting` 后退出消费并显式关闭 Runtime 流。
- 实际：发布 WAIT 时 Transcript 的 run_status 为 None。代码在 yield 恢复后才设置 terminal 并提交 WAITING，因此消费者按终态交接后关闭，提交不会发生。
- 修复要求：在向外发布 RunWaiting 前完成必需最终化；发布失败或最终化失败必须有明确结果。宿主不应为了保证 WAIT 落库而再拉取一次事件。
- 用例：`test_wait_status_commits_before_waiting_event_is_published`。

**B06 · P2：观察者收不到 Runner 自己产生的运行终态。**

- 位置：[runner.py:169](C:/codebase/coresmos/src/agent_runtime/runner.py:169)，失败和取消分支也采用相同的直接 yield 方式。关联 T12。
- 复现：无阻塞、无异常、队列未满的观察者参与一次正常文本运行；消费者收到了 RunSucceeded，观察者没有收到。
- 原因：`_observe` 用于 RunStarted 和 loop 事件，但 Runner 的成功、失败和取消终态没有进入观察队列。最终 TimingEvent 只有耗时，不能替代运行结果。
- 修复要求：通过统一的终态发布路径完成观测与外部输出，并补齐成功、失败、取消、CompletedEntry 的关联和终态测试。
- 用例：`test_observer_receives_run_terminal_event`。

**B07 · P1：耗尽的尝试预算和恢复入口的 token 预算仍允许模型调用。**

- 位置：[pipelines/model.py:35](C:/codebase/coresmos/src/agent_runtime/pipelines/model.py:35)、[core.py:101](C:/codebase/coresmos/src/agent_runtime/core.py:101)。关联 T02、T05、T08。
- 复现一：`max_attempts=0, remaining_attempts=0`，仍调用模型 1 次。`max(1, remaining_attempts)` 强行恢复了一次调用额度。
- 复现二：恢复入口传入 `ConsumedBudget.estimated_tokens=100`，限制 `max_estimated_tokens=10`，仍调用模型 1 次；分配阶段只检查 steps / model_rounds。
- 修复要求：预算为零或恢复时已超限应在发起请求前结束；明确维护并执行累计 token 预算，不能把 T07 的单次 prompt 预算当作累计执行预算。
- 用例：`test_exhausted_budget_does_not_start_another_model_request` 的两个参数场景。

**3. T12–T15 的复验状态**

| 任务 | 已补齐 | 尚未满足 |
| --- | --- | --- |
| T12 观测 | 有界队列、观察者隔离、TimingEvent、OTel API 适配器及测试 | B03 初始化清理与 B06 终态观测 |
| T13 参考适配器 | Chat Completions 适配器、可选导入、SDK 工厂、示例、部分协议测试 | B01 请求编码、B02 Runtime 内流超时；现有替身测试未覆盖真实 SDK 编码 |
| T14 公共契约支持 | 工厂绑定 Harness、5 个公开场景；上一轮验收用例已纳入默认 pytest | 本轮 10 项失败；目标切换终止及完整故障矩阵仍需通过可替换实现和安装制品验证 |
| T15 制品与文档 | adapter-guide、upgrade-notes、wheel/sdist、四种安装组合、CI 平台矩阵配置 | 本次仅实际运行 Windows 3.12；未取得 Linux 3.12/3.13/3.14 的执行结果；Hatchling 隔离构建依赖仍是版本范围，未见精确锁定 |

可选依赖下界的安装和合成场景已通过，但 OpenAI 2.45.0 的真实请求编码仍失败，因此不能将“下界安装通过”表述为“完整协议兼容通过”。其他直接依赖的全部下界组合没有在本轮分别解析和执行。

本地 HTTP mock 使用官方文档中的 chunk、delta、finish_reason 结构；未调用真实模型端点。[OpenAI Chat Completions 流式事件说明](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)。

**4. 复现材料**

- [新增契约用例](C:/codebase/coresmos/acceptance/round2/test_reacceptance_contracts.py)
- [全套结果：74 passed、10 failed](C:/codebase/coresmos/acceptance/round2/full-results.txt) / [JUnit](C:/codebase/coresmos/acceptance/round2/full-results.xml)
- [初始 74 项通过及覆盖率](C:/codebase/coresmos/acceptance/round2/baseline-results.txt)
- [真实 SDK 探针](C:/codebase/coresmos/acceptance/round2/sdk_transport_probe.py)、[2.54.0 结果](C:/codebase/coresmos/acceptance/round2/sdk-locked-results.txt)、[2.45.0 结果](C:/codebase/coresmos/acceptance/round2/sdk-floor-results.txt)
- [安装制品场景脚本](C:/codebase/coresmos/acceptance/round2/installed_scenes.py)、[组合 extra 场景结果](C:/codebase/coresmos/acceptance/round2/installed-combined-env.txt)
- [源文件校验清单](C:/codebase/coresmos/acceptance/round2/source-manifest.json)

项目目录中的复现命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --tb=short
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider acceptance/round2/test_reacceptance_contracts.py --tb=short
```

从源码目录外复现已安装制品的 SDK 请求编码问题：

```powershell
Set-Location -LiteralPath $env:TEMP
& 'C:\codebase\coresmos\acceptance\round2\combined-env\Scripts\python.exe' -I 'C:\codebase\coresmos\acceptance\round2\sdk_transport_probe.py'
```

两个命令类型的失败均为本报告描述的行为失败。SDK 探针使用内存 MockTransport，HTTP 调用计数表示进入本地 mock 的次数。

| 本轮制品 | SHA-256 |
| --- | --- |
| `acceptance/round2/dist/agent_runtime-0.1.0-py3-none-any.whl` | `8F990E3F93EE38A3AF92B55AF099D761613A47EACF020C7D2E873B97C4C9712C` |
| `acceptance/round2/dist/agent_runtime-0.1.0.tar.gz` | `CAB491D6A4F0C3986ABA13E0E192AAC15B1B0B2396FB2294161A271BCF9BC807` |

**5. 复验门槛与本次改动**

先修复 B01–B05、B07，并补齐 B06 的终态观测，再要求 84 项 pytest 同时通过、两个 SDK 版本的三个 MockTransport 场景全部通过。保留当前四种安装组合验证，取得兼容矩阵的实际执行结果后再判定完整公共 v0.1 通过；内部接入继续单独验收。

本轮仅新增本报告和 acceptance/round2 中的复现材料、日志及隔离环境。运行库、原有测试、第一轮验收报告和设计文档均未修改。安装阶段的一次自动审批用量错误已通过后续获批的单项命令解决；可选依赖已安装完成，没有将该环境问题计作代码缺陷。
