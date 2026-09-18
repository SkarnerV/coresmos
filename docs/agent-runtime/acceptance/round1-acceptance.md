# Agent Runtime 开发验收报告

验收日期：2026-09-17。对象：当前工作区的 `agent-runtime 0.1.0`；环境：Windows、Python 3.12.13。

**结论：完整 v0.1 验收不通过。** 基础工程检查和安装后的首个合成闭环通过，但独立验收发现 10 类行为问题，11 个复现用例全部失败；T12–T15 的交付也尚未齐备。

验收依据为 [集成设计](../integration-design.md)、[技术选型](../technology-selection.md) 和 [开发任务清单](../development-tasks.md)。公司内部 I01–I07 没有在本次工作区提供对应实现与环境，状态为未验收；Unibot 仅是参考，不能代替实际内部接入证明。

**1. 已执行检查**

| 检查 | 实际结果 |
| --- | --- |
| 原有 pytest 套件及覆盖率 | 41 passed，覆盖率 88%；有 1 条 pytest 缓存写入权限警告 |
| 原有代码 Ruff lint / format | 通过，41 个文件格式检查通过 |
| mypy | 通过，30 个源文件 |
| 独立行为验收 | 11 failed；见下文 A01–A10 |
| 新增验收脚本 Ruff lint / format | 通过 |
| 离线重建 sdist，再由 sdist 构建 wheel | 通过 |
| 将 wheel 安装到独立虚拟环境 | 通过；未安装 openai、otel、pytest |
| 从源码目录外，以 Python `-I` 执行已安装包 | 通过；确认导入来自隔离环境的 site-packages，`py.typed` 存在 |
| 安装后“模型 → 工具 → 文本完成” | 通过；2 次模型调用、1 次工具执行、成功终态 |

原有测试通过不能证明下列契约成立。独立用例保留真实默认装配、记录器和执行管线，只控制模型、工具和应用状态等边界；异步用例使用 Event 明确进入待验收阶段，并在结束时释放阻塞。所有用例均因目标行为断言失败，未出现导入或测试收集错误。

**2. 需要修复的行为问题**

**A01 · P1：工具结果批次失败后留下部分已提交历史。**

- 位置：[recording.py:352](../../../src/agent_runtime/recording.py:352)。关联 T04、T09。
- 复现：先记录 c1、c2；提交结果时第一项为有效 c1，第二项为未知调用。第二项抛出 `ReceiptMismatchError`，但第一项已经追加，快照从 v1 变成 v2。
- 影响：调用方收到提交失败，却能读到半批结果；再次提交可能重复追加，后续上下文失去完整配对。
- 修复要求：先完整核验批次并准备提交内容，再统一发布已提交视图；任何校验或提交失败都保持之前的快照和幂等状态。
- 用例：`test_failed_result_batch_does_not_publish_partial_history`。

**A02 · P1：工具结果回执没有核验完整身份和内容。**

- 位置：[recording.py:344](../../../src/agent_runtime/recording.py:344)。关联 T04。
- 复现一：保留真实 `logical_op_id`，把回执中的调用替换为未记录的 ID，结果仍被接受。
- 复现二：把另一 run / step / target 的提交与原回执组合，结果仍写入新的目标。
- 原因：只检查该 op 存在，随后使用调用方提供的 `previous_receipt.calls`；同 op ID 的比较不能证明回执内容有效。
- 修复要求：以记录器内部保存的事实核验回执、消息存在性和 run / step / target / call 身份，并将这些身份纳入幂等校验。
- 用例：`test_result_receipt_cannot_add_an_unrecorded_call`、`test_result_receipt_is_bound_to_its_run_step_and_target`。

**A03 · P1：宿主任务取消被流关闭异常覆盖。**

- 位置：[lifecycle.py:107](../../../src/agent_runtime/lifecycle.py:107)、[lifecycle.py:144](../../../src/agent_runtime/lifecycle.py:144)。关联 T03、T11。
- 复现：底层模型等待下一段输出时，对消费 Runtime 的任务执行 `cancel()`。
- 实际：返回 `RunFailed(RuntimeError, "aclose(): asynchronous generator is already running")`，没有传播 `CancelledError`。
- 原因：外层等待被取消时，内部 `__anext__` 任务尚未退出，就调用底层流的 `aclose()`。
- 修复要求：在取消路径中取消并等待所拥有的事件等待任务，再关闭流；清理异常不能覆盖宿主取消语义。
- 用例：`test_host_task_cancellation_propagates_and_closes_model`。

**A04 · P1：准备下一步时，用户停止不能中断阻塞的 provider / 应用快照读取。**

- 位置：[core.py:132](../../../src/agent_runtime/core.py:132)；初始工具入口的第 99 行同样直接等待 prepare。关联 T03、T07、T11。
- 复现：初始装配正常完成，让 step preparation 中的 `application.current()` 阻塞，再发出 `request_user_stop()`。
- 实际：运行在 0.5 秒观察窗口内没有退出，必须由测试释放应用调用后才能结束；模型调用次数为零，证明已进入准备阶段而非模型阶段。
- 修复要求：准备、provider 等异步等待也应受 RunControl / deadline 控制，并取消、等待本次运行拥有的任务。
- 用例：`test_user_stop_interrupts_step_preparation`。

**A05 · P1：`ToolBatchEntry.continue_after=False` 仍然调用模型。**

- 位置：[core.py:118](../../../src/agent_runtime/core.py:118)。关联 T05、T11。
- 复现：工具入口关闭继续执行，工具成功、结果政策返回默认 CONTINUE。
- 实际：工具执行 1 次后，仍额外调用模型 1 次。
- 修复要求：FINISH / WAIT 按设计优先处理；其余情况执行入口约定，`continue_after=False` 时结束运行。
- 用例：`test_tool_entry_continue_after_false_does_not_call_model`。

**A06 · P1：Runtime 把流式事件缓存到整个步骤结束后才交给消费者。**

- 位置：[core.py:36](../../../src/agent_runtime/core.py:36)、[core.py:133](../../../src/agent_runtime/core.py:133)。关联 T05、T08、T09、T11。
- 复现：模型先输出文本 delta，再等待外部信号；在释放完成信号前，Runtime 消费者收不到该 delta。
- 原因：`consume_strict()` 将所有事件放入列表，正常耗尽后才返回，AgentLoop 随后统一 yield。工具事件使用相同路径。
- 影响：首段文本和工具进度延迟到整步完成，长流还会持续积累缓存。
- 修复要求：即时转发普通事件，保留完成状态校验；只有推进下一阶段需要等待完成事件之后的正常耗尽。
- 用例：`test_text_delta_is_visible_before_model_finishes`。

**A07 · P2：工具返回的上下文贡献被默认结果政策丢弃。**

- 位置：[pipelines/tools.py:37](../../../src/agent_runtime/pipelines/tools.py:37)、[context/manager.py:91](../../../src/agent_runtime/context/manager.py:91)。关联 T07、T09、T14。
- 复现：Invoker 返回带 `contributions` 的 `InvocationOutcome`，工具正常完成，进入第二次模型请求。
- 实际：下一请求没有这项贡献。默认政策处理 activate_tools、state_payload 和 flow_hint，却不保存 contributions；StepProvider 也没有相应读取来源。
- 修复要求：明确贡献的作用域、版本和提交位置，在下一轮重建中合并，保持当前批次快照稳定。
- 用例：`test_tool_context_contribution_reaches_next_request`。

**A08 · P1：压缩漏算工具调用参数，准备后的超预算请求仍被放行。**

- 位置：[context/compress.py:42](../../../src/agent_runtime/context/compress.py:42)、[context/manager.py:115](../../../src/agent_runtime/context/manager.py:115)。关联 T07。
- 复现：完整历史工具组包含 6,000 字符的调用参数，总预算 512，预留输出 16。
- 实际：公共估算器算出消息需要 1,505 tokens，可用预算只有 455，`prepare()` 仍成功返回。
- 原因：压缩器只累计 content / reasoning；prepare 最后的完整估算结果没有用于预算判断。
- 修复要求：压缩与最终校验使用一致口径，包含工具调用名称和参数；无法满足预算时明确失败，不能继续发起请求。
- 用例：`test_prepared_request_budget_includes_tool_call_arguments`。

**A09 · P1：历史投影会保留缺失结果的 assistant 工具调用。**

- 位置：[context/compress.py:12](../../../src/agent_runtime/context/compress.py:12)。关联 T07。
- 复现：assistant 调用 c1、c2，只有 c1 的结果，随后是下一条 user 消息。
- 实际：投影原样保留两个调用和一个结果，将不完整关系送入下一模型上下文。
- 修复要求：按完整、相邻、唯一的调用 / 结果组校验和修复；不能只删除孤立的 tool 结果而保留悬空调用。原始事实保留在 Transcript 中。
- 用例：`test_history_projection_does_not_leave_unmatched_assistant_calls`。

**A10 · P2：部分输出失败后默认自动重试，与技术选型约定不一致。**

- 位置：[pipelines/model.py:109](../../../src/agent_runtime/pipelines/model.py:109)。关联 T02、T08。
- 依据：[技术选型第 128 行](../technology-selection.md:128) 约定默认保留可识别的未完成输出并结束失败，恢复需要显式启用。
- 复现：模型先输出 partial delta，再抛出 AdapterError；宿主未配置恢复部分输出。
- 实际：自动发起第 2 次模型调用。将不同尝试标注不同身份，不能替代“是否允许继续尝试”的政策。
- 修复要求：部分输出后的默认路径终止；需要恢复时提供显式政策，并覆盖记录、事件与最终状态断言。
- 用例：`test_partial_output_failure_does_not_retry_without_opt_in`。

**3. 任务交付核对**

| 范围 | 验收状态 | 尚缺内容 |
| --- | --- | --- |
| T01–T02 工程与公共契约 | 基础检查通过 | 不代表所有契约字段与政策都已实现；见 A05、A10 |
| T03–T11 默认运行链路 | 主流程通过，完整验收未通过 | A01–A10 中的生命周期、记录、上下文、入口与流式问题 |
| T12 观测与 OTel | 未完成 | 当前只有 NoOpObserver；没有默认运行链路接入、耗时关联、故障隔离或 OTel 观察者 |
| T13 模型参考适配器 | 未完成 | adapters 目录只有 `__init__.py`；缺少选定 SDK 的参考适配器、本地协议测试和 smoke 示例 |
| T14 公共契约 Harness | 部分完成 | 有 ScriptedModel / ScriptedInvoker 和现有测试；未提供可通过工厂绑定其他实现的契约 Harness，独立故障场景仍失败 |
| T15 制品和接入交付 | 部分完成 | wheel / sdist 和基础隔离安装通过；缺少 adapter-guide、版本说明、extras 组合及依赖下界验证、完整兼容矩阵 |
| I01–I07 内部接入 | 未验收 | 本工作区未提供实际内部适配与业务回归证据 |

当前 CI 只配置 Ubuntu / Python 3.12，未配置计划中的 Linux 3.13 / 3.14 和 Windows 3.12 矩阵，也未执行制品安装和 extras 组合测试。本次 Windows 3.12 本地结果不能代替其余平台结果。

公开装配也需要在 T14 接入验收时补查：DefaultRuntime 固定创建 FixedCapabilityProvider / DefaultStepProvider，assemble_default 没有暴露 ContextContributor、压缩器或观察者注入。仅存在协议或独立类，不足以证明内部适配可以直接复用默认装配。此项为静态检查结果，未计入 11 个失败用例。

**4. 复现材料与命令**

- [独立验收用例](../../../acceptance/test_runtime_contracts.py)
- [失败输出](../../../acceptance/contract-results.txt) / [JUnit 结果](../../../acceptance/contract-results.xml)
- [隔离安装 smoke 脚本](../../../acceptance/installed_smoke.py)
- [源文件 SHA-256 清单](../../../acceptance/source-manifest.json)

在项目目录执行下列 PowerShell 命令可复现契约问题。当前预期结果是 11 failed；修复后应全部通过。

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider acceptance/test_runtime_contracts.py --tb=short
```

验收用例位于独立 acceptance 目录，当前 pytest 的 `testpaths=["tests"]` 不会自动收集它们。修复时应将对应回归纳入正式契约套件和 CI。

本次构建及安装使用以下命令，随后在系统临时目录运行 smoke 脚本：

```powershell
uv build --offline --out-dir acceptance/dist
.\.venv\Scripts\python.exe -m venv --without-pip acceptance/.venv
uv pip install --offline --python acceptance/.venv/Scripts/python.exe acceptance/dist/agent_runtime-0.1.0-py3-none-any.whl
Set-Location -LiteralPath $env:TEMP
& 'C:\codebase\coresmos\acceptance\.venv\Scripts\python.exe' -I 'C:\codebase\coresmos\acceptance\installed_smoke.py'
```

| 本次重新构建的制品 | SHA-256 |
| --- | --- |
| `acceptance/dist/agent_runtime-0.1.0-py3-none-any.whl` | `684272AA4AEA68DCE41474E2BBC99D9B3C13DFFE0E9E5FCE1E63F84EE189D8AB` |
| `acceptance/dist/agent_runtime-0.1.0.tar.gz` | `A119140A684B713EE1172B46A46A3ECE13566FB7488BCBC9AF5A263F87AA955E` |

**5. 复验门槛**

优先修复记录完整性与回执核验、取消和关闭、执行入口与实时事件，再完成上下文、预算和部分输出政策。复验需原有 41 项测试与上述独立用例同时通过，并补齐 T12–T15 的公开交付及对应验证。公司内部验收继续按 I01–I07 单独记录。

本次未修改运行库实现、原有测试或前三份设计文档；新增的是验收报告、独立用例及验收产物。本次没有进行真实模型联网调用、公司服务调用或生产切换。
