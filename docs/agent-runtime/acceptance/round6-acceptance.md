# Agent Runtime 第六轮验收报告

验收日期：2026-09-18（Asia/Shanghai）。环境：Windows、Python 3.12.13。
验收对象：任务「修复 Agent Runtime 选型问题」在 `e21dadf135ccf499aa1eeda30461044d6dc95e5c` 基础上的四个未提交源码修改。
对照：[第五轮报告](round5-acceptance.md)、[集成设计](../integration-design.md)。

**结论：D01–D04 的原复现用例全部通过；完整公共 v0.1 验收仍有一项 P1 遗留问题。** 能力更新会丢失已有工具的非默认执行绑定。这段逻辑在基线提交中已经存在，并非本次四处修复引入；本轮通过后续模型轮次的组合用例确认了它。

加入新用例前为 **114 passed**；新增八项用例为 **7 passed、1 failed**；最终完整套件为 **121 passed、1 failed**，覆盖率 **92%**。本轮仅新增验收用例、核对脚本、日志和报告，保留引用任务的源码改动及此前验收材料。

## 1. E01 / P1：激活新能力后，已有工具被重新绑定到 default

位置：`src/agent_runtime/capabilities/providers.py:157–159`。

触发路径：ResolverChain 跳过第一个提供者，选中第二个提供者，将 `echo` 绑定到 `selected-backend`；第一次执行 `echo` 的结果通过 `InvocationOutcome.activate_tools` 激活另一工具 `other`；下一模型轮次再次调用 `echo`。

实际结果：两次执行解析出的后端依次为 **`selected-backend`、`default`**，预期应均为 `selected-backend`。复现经过真实 Runtime、工具管线、能力会话和记录流程；模型调用三次、工具调用两次，运行仍返回成功，因此错误路由不会通过终态暴露。

原因：`CapabilitySession.publish()` 只读取旧绑定的工具名称，随后把旧工具和传入工具的执行器全部赋为 `default`。`activate()` 合并已有与新增工具后调用该方法，未传入恢复旧绑定的覆盖参数。D02 的修复使初始能力选择正确，但未覆盖能力更新后的映射保留。

归因：已与 `git show HEAD:src/agent_runtime/capabilities/providers.py` 对照，这段重置逻辑在 `e21dadf` 中已存在；不能将其标记为本次修复引入的回归。

修复建议：发布新快照时保留仍有效工具的旧映射值，仅为新增且未显式指定绑定的工具设置默认值，再应用显式覆盖。旧版本快照应继续可解析。验收时应覆盖非默认执行器、连续激活及并发运行的绑定隔离。

复现用例：[test_activating_another_tool_preserves_the_selected_backend](../../../acceptance/round6/test_fix_followups.py)。

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider acceptance/round6/test_fix_followups.py::test_activating_another_tool_preserves_the_selected_backend --tb=short
```

## 2. 已通过的检查

| 范围 | 结果 |
| --- | --- |
| D01 投影缓存 | 原跨记录器复现通过；补测相同 run ID、相同消息 ID 但不同消息内容，模型收到正确的新输入 |
| D02 能力链绑定 | 原回退选中用例通过；补测两个并发运行分别选中不同提供者，隐式和显式链 registry 均保持正确绑定 |
| D03 初始化取消 | USER_STOP、HOST_CANCEL、DEADLINE 三个原复现全部通过 |
| D04 回执真实性 | 原伪造 `recording_required` 用例通过；补测无需记录批次的 run、step、target 不匹配均被拒绝，合法重放仍可通过 |
| 原有全部测试 | 114 项通过，包含第五轮的六项复现 |
| Ruff check / format | 通过；60 个 Python 文件格式正确 |
| mypy | 32 个源文件检查通过 |
| 安装后的公开 Harness | 基础、OpenAI 2.45.0、OTel 1.43.0、OpenAI 2.54.0 + OTel 1.44.0 四个环境，各 20 个场景通过 |
| 真实 SDK 序列化 | OpenAI 2.45.0 和 2.54.0 各三个 HTTPX MockTransport 场景通过，每场景一次 HTTP handler 调用 |

四个安装环境均使用本轮重建的 wheel，在源码目录外通过 `python -I` 运行公开 Harness，并检查包来自 site-packages、包含 `py.typed` 且环境未安装 pytest。SDK 检查采用本地 HTTP mock，未调用真实模型服务。

## 3. 源码、制品与证据

本轮从工作区源码构建 sdist，再从 sdist 构建 wheel。已逐文件核对源码、wheel 及四个安装包中的 32 个 `.py` 与 `py.typed`，33 个文件全部一致；验收结束时的源码 diff 与保存的补丁逐字节一致。

| 对象 | SHA-256 |
| --- | --- |
| 未提交源码补丁 | `a2e549af69d3f8cf6bf833e6d02a12df0474789349484d55ef5aaff092bca0d8` |
| `agent_runtime-0.1.0-py3-none-any.whl` | `3a40b760f15a50072550f449e1d75040f1fecff4a9d49d1d7835a1af9f4d6585` |
| `agent_runtime-0.1.0.tar.gz` | `65b98c2a5a7a2dcb779a4bb1b7ec9cf1e70bc4360de5a571cf0189b06c4910dd` |

| 材料 | 链接 |
| --- | --- |
| 本轮八项补测 | [test_fix_followups.py](../../../acceptance/round6/test_fix_followups.py) |
| 原有 114 项测试 | [baseline-results.txt](../../../acceptance/round6/baseline-results.txt)、[JUnit](../../../acceptance/round6/baseline-results.xml) |
| 新增用例：7 passed、1 failed | [followup-results.txt](../../../acceptance/round6/followup-results.txt)、[JUnit](../../../acceptance/round6/followup-results.xml) |
| 完整套件：121 passed、1 failed | [final-results.txt](../../../acceptance/round6/final-results.txt)、[JUnit](../../../acceptance/round6/final-results.xml) |
| 静态检查 | [Ruff](../../../acceptance/round6/ruff-results.txt)、[格式](../../../acceptance/round6/format-results.txt)、[mypy](../../../acceptance/round6/mypy-results.txt) |
| 四个安装环境 | [基础](../../../acceptance/round6/installed-base.txt)、[OpenAI](../../../acceptance/round6/installed-openai-floor.txt)、[OTel](../../../acceptance/round6/installed-otel-floor.txt)、[组合](../../../acceptance/round6/installed-combined.txt) |
| SDK 版本检查 | [2.45.0](../../../acceptance/round6/sdk-floor.txt)、[2.54.0](../../../acceptance/round6/sdk-locked.txt) |
| 审核源码与制品核对 | [源码补丁](../../../acceptance/round6/source-changes.patch)、[manifest](../../../acceptance/round6/source-manifest.json)、[核对脚本](../../../acceptance/round6/artifact_manifest.py) |

源码修复与本轮验收材料尚未提交。`e21dadf` 的已有远端 CI 仅覆盖基线提交，不代表这些本地修改已通过 CI；修复 E01 后仍需在包含修复和验收用例的提交上核对 CI。公司内部接入 I01–I07 仍单独验收；此前记录的 T15 构建依赖精确锁定要求不在本轮四处修复范围内。
