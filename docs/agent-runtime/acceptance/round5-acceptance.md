# Agent Runtime 第五轮独立验收报告

验收日期：2026-09-18（Asia/Shanghai）。本机：Windows、Python 3.12.13。
验收提交：`e21dadf135ccf499aa1eeda30461044d6dc95e5c`。
对照：[集成设计](../integration-design.md)、[开发任务](../development-tasks.md)、[上一轮验收及修复记录](round4-acceptance.md)。

**结论：仍不通过公共 v0.1 验收。** 上一轮 C01–C03 的原复现路径已通过；本次新增功能的组合路径发现 4 类问题（3 项 P1、1 项 P2）。提交自带的 108 项测试全部通过，加入本轮 6 项复现后为 **108 passed、6 failed**，覆盖率 92%。

最新提交的 GitHub CI 矩阵已核实全部通过。新构建 wheel 在四种依赖组合下分别通过 20 个公开 Harness 场景，两个 OpenAI SDK 版本的请求序列化场景也通过。这些已有场景尚未覆盖下面的组合问题。

## 1. 待修复问题

### D01 / P1：压缩投影跨运行命中错误历史

位置：`src/agent_runtime/runner.py:127`；`src/agent_runtime/context/manager.py:147–157,199–201`。

触发条件：复用同一个默认 Runtime、使用相同 RecordTarget，第一次运行发生压缩；第二次运行使用新的默认 MemoryTranscript，但输入条数相同。两个记录器都从自己的版本计数起点开始，因此历史版本相同。

复现结果：第一次输入为很长的旧历史和 `first question`；第二次输入为 `short history`、`second question`，本来无需压缩。第二次真正发给模型的消息却只有 **`first question`**。两次运行均返回成功，模型共调用两次。

原因：默认投影存储提升到了 Runtime 实例级别，跨运行保留；缓存 key 使用历史、能力和应用的版本号、贡献摘要及可用预算，缺少实际 prompt 的内容指纹或事实存储命名空间。不同事实存储的相同版本被当作同一输入。

影响：当前请求被旧请求替换，模型读取错误的上下文，并带入上一运行的消息身份。共享 Runtime 的默认装配无法保证运行隔离。

修复方向：明确投影缓存的事实来源作用域，并用实际模型输入的稳定摘要或等价的全局唯一事实版本防止冲突；保留相同输入下的合法缓存复用。

复现：`test_new_run_does_not_reuse_another_transcripts_compressed_messages`。

### D02 / P1：能力链回退后仍使用被跳过提供者的执行绑定

位置：`src/agent_runtime/capabilities/providers.py:95–105`。

触发条件：ResolverChain 中两个提供者各自持有 BindingRegistry。第一个返回 NoMatch，第二个返回 Matched；二者都暴露 `echo`，但分别绑定到 `skipped-backend` 和 `selected-backend`。

复现结果：运行成功且工具被调用一次，但实际解析出的执行器为 **`skipped-backend`**，而非选中的 `selected-backend`。

原因：链在构造时固定使用第一个 BindingSource 的 registry，与最终匹配的提供者无关。独立 BindingRegistry 的版本均从 `bind-1` 开始，同名工具使该错误不会被 `_validate_batch()` 检出。

影响：能力选择与执行器分派不一致，可能执行错误后端；如果名称不同，则会拒绝本来合法的调用。

修复方向：确保整个链发布的绑定引用在执行端唯一且可正确解析。可以要求并校验所有提供者使用同一 registry，或使用带来源作用域的引用；混用独立 registry 时至少应显式拒绝，不能静默采用第一个。选中后替换共享全局 registry 也需要防止并发运行串扰。

复现：`test_resolver_fallback_executes_the_selected_providers_binding`。

### D03 / P1：初始化能力解析不响应停止和运行期限

位置：`src/agent_runtime/runner.py:152`。

触发条件：注入的异步 CapabilityProvider 正在等待目录、权限或网络结果。在此期间发出用户停止、宿主取消，或运行的 `deadline_seconds=0.05` 到期。

复现结果：三种情况在等待 300 ms 后都没有结束，只有测试手动放行 provider 后任务才结束；模型调用数均为 0。

原因：应用快照读取走 `wait_cancellable()`，紧接着的能力解析却直接 `await self._provider.resolve(...)`。该等待没有监听 RunControl；新提供者不接收 RunControl 参数，也不能自行遵守此运行的停止信号。

影响：外部能力来源阻塞会让整次运行停在初始化阶段，用户停止、宿主取消和声明的墙钟期限均不能及时结束等待。

修复方向：把能力解析纳入同一运行作用域的可取消等待，取消并等待实际解析任务退出，保留正确的取消来源和终态。

复现：`test_stop_interrupts_initial_capability_resolution`，按 USER_STOP、HOST_CANCEL、DEADLINE 参数化为三项。

### D04 / P2：无需记录分支跳过了回执真实性校验

位置：`src/agent_runtime/recording.py:508–510`。

触发条件：先取得正常记录批次的 BatchReceipt，再将其 `recording_required` 改为 False，作为 `record_tool_results()` 的 previous_receipt 传入。

复现结果：记录器直接返回，未抛出 `ReceiptMismatchError`，应当记录的结果被静默跳过。

原因：该分支在读取已提交操作、比较稳定回执身份及核验 run/step/target 之前返回。是否无需记录由调用方提供的标志决定，没有与真实提交的批次核对。

影响：显式要求的 previous_receipt 校验在新分支上失效；错误回执不再可靠地阻止结果丢失。该问题属于记录器公共接口，默认工具管线的正常回执路径不直接触发它。

修复方向：先核验已提交批次及完整稳定身份，再依据已提交批次的 `recording_required` 决定是否跳过；合法的无需记录批次仍应保持不写模型历史的行为。

复现：`test_recording_required_flag_cannot_bypass_receipt_validation`。

## 2. 已通过的复验

| 项目 | 本轮结果 |
| --- | --- |
| C01 合法回执重放 | 原用例通过，重放产生的 `created=False` 不再被错误拒绝 |
| C02 模型重试累计预算 | 原空响应、异常重试预算用例通过，每次尝试预留累计估算 |
| C03 SDK 流关闭期限 | 原 50 ms 关闭阻塞用例通过，未再无限等待该可取消资源 |
| 上一轮其他终态用例 | 成功、失败、取消、WAIT、CompletedEntry 及 WAIT 最终化失败用例通过 |
| 提交自带测试 | 108 passed，覆盖率 92% |
| Ruff check / format / mypy | 通过；含本轮材料的 58 个 Python 文件格式检查，32 个源文件类型检查 |
| 可安装公共 Harness | 基础、OpenAI、OTel、组合四个环境，各 20 个场景通过，无 pytest 依赖 |
| OpenAI SDK | 2.45.0 和 2.54.0 各 3 个 HTTPX MockTransport 场景通过，每个场景一次 HTTP handler 调用 |
| CI | Ubuntu Python 3.12/3.13/3.14、Windows Python 3.12 和 extras job 全部 success |

CI 的 `headSha` 已核对为本轮提交：[run 35250239665](https://github.com/SkarnerV/coresmos/actions/runs/35250239665)。该次 CI 使用提交自带的测试，本轮新增的六项用例尚未进入远端 CI。

## 3. 制品与可复现证据

本轮由源码重建 sdist，再从 sdist 构建 wheel。复用上一轮四个独立安装环境，将 agent-runtime 强制替换为本轮 wheel，保留已安装依赖版本。从源码目录外以 `python -I` 运行公共 Harness。

已逐文件核对源码、wheel、四个已安装包中的 32 个 `.py` 和 `py.typed`，33 个文件全部一致。SDK 检查使用本地 HTTP mock，未调用真实模型服务。

| 制品 | SHA-256 |
| --- | --- |
| `agent_runtime-0.1.0-py3-none-any.whl` | `ef14f56a19c21cf8f461a7b17143beb0e5ccceb7a141f869c152204ec3b34e78` |
| `agent_runtime-0.1.0.tar.gz` | `9c1a56c426c28c2deb155c5af7dcec6a48ba33dbd4c883eef1610d496c177896` |

| 材料 | 链接 |
| --- | --- |
| 新增六项复现用例 | [test_integration_boundaries.py](../../../acceptance/round5/test_integration_boundaries.py) |
| 提交自带测试基线 | [baseline-results.txt](../../../acceptance/round5/baseline-results.txt) |
| 新增用例结果 | [boundary-results.txt](../../../acceptance/round5/boundary-results.txt) |
| 完整套件：108 passed、6 failed | [full-results.txt](../../../acceptance/round5/full-results.txt)、[JUnit](../../../acceptance/round5/full-results.xml) |
| 静态检查 | [lint-results.txt](../../../acceptance/round5/lint-results.txt) |
| 安装后的 20 场景验证入口 | [installed_suite.py](../../../acceptance/round5/installed_suite.py) |
| 四个安装环境 | [基础](../../../acceptance/round5/installed-base.txt)、[OpenAI 2.45.0](../../../acceptance/round5/installed-openai-floor.txt)、[OTel 1.43.0](../../../acceptance/round5/installed-otel-floor.txt)、[OpenAI 2.54.0 + OTel 1.44.0](../../../acceptance/round5/installed-combined.txt) |
| 两个 SDK 版本 | [2.45.0](../../../acceptance/round5/sdk-floor.txt)、[2.54.0](../../../acceptance/round5/sdk-locked.txt) |
| 来源与哈希核对 | [source-manifest.json](../../../acceptance/round5/source-manifest.json)、[核对脚本](../../../acceptance/round5/artifact_manifest.py) |
| CI 原始结果 | [ci-results.json](../../../acceptance/round5/ci-results.json) |

在仓库根目录复现：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider acceptance/round5/test_integration_boundaries.py --tb=short
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --tb=short --cov=agent_runtime --cov-report=term-missing
.\.venv\Scripts\ruff.exe check src tests examples acceptance
.\.venv\Scripts\ruff.exe format --check src tests examples acceptance
.\.venv\Scripts\mypy.exe src/agent_runtime
uv build --offline --out-dir acceptance/round5/dist
gh run view 35250239665 --json databaseId,headSha,status,conclusion,url,jobs
```

安装制品验证示例，其余环境分别替换为 `base-env`、`otel-env`、`combined-env`：

```powershell
uv pip install --offline --no-deps --reinstall-package agent-runtime --python acceptance/round2/openai-env/Scripts/python.exe acceptance/round5/dist/agent_runtime-0.1.0-py3-none-any.whl
Push-Location $env:TEMP
& C:\codebase\coresmos\acceptance\round2\openai-env\Scripts\python.exe -I C:\codebase\coresmos\acceptance\round5\installed_suite.py
& C:\codebase\coresmos\acceptance\round2\openai-env\Scripts\python.exe -I C:\codebase\coresmos\acceptance\round2\sdk_transport_probe.py
Pop-Location
```

本轮交付为新增验收用例、验证脚本、日志及报告，运行库源码与原验收记录保持原样。修复 D01–D04 后应通过当前 114 项用例，并在包含新增用例的提交上重新核对 CI 与安装制品。公司内部接入 I01–I07 仍单独验收；T15 的构建依赖精确锁定要求仍未在本提交中改变。
