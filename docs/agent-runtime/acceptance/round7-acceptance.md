# Agent Runtime 第七轮验收报告

验收日期：2026-09-18（Asia/Shanghai）。环境：Windows、Python 3.12.13。
验收对象：任务「修复 Agent Runtime 选型问题」在 `e21dadf135ccf499aa1eeda30461044d6dc95e5c` 基础上的未提交源码修改。
与[第六轮验收](round6-acceptance.md)保存的源码逐文件比较，仅 `src/agent_runtime/capabilities/providers.py` 发生变化。

**结论：本次修复通过本地验收。** D01–D04 和 E01 的原复现全部通过，新增的能力更新边界检查也通过；本轮验证范围内未发现新的阻塞问题。

修复后的原有套件为 **122 passed**，加入本轮三项补测后为 **125 passed、0 failed**，覆盖率 **93%**。四种依赖环境下安装后的公开 Harness 各通过 20 个场景。

## 1. E01 关闭依据

`CapabilitySession.publish()` 现在读取旧绑定的完整映射，为仍在新快照中的工具保留原值；新增工具默认到 `default`，随后应用显式 `extra_bindings`。原来只保留工具名称并把所有值重置为 `default` 的路径已移除。

原集成复现 `test_activating_another_tool_preserves_the_selected_backend` 已通过：ResolverChain 选中 `selected-backend` 后，首次工具调用激活另一工具，第二次仍执行原来的 `selected-backend`。三次模型调用、两次工具执行及成功终态均符合预期。

本轮新增三项用例检查了下列行为：

| 场景 | 验证结果 |
| --- | --- |
| 连续激活、重复激活 | 原工具和先前显式绑定的新工具均保留各自 backend；后续新增工具使用 `default` |
| 旧版本隔离 | 新快照得到新引用；旧引用仍解析到原绑定及原工具集合 |
| 显式覆盖、移除与重新激活 | 显式覆盖生效；移除后新快照不再暴露该绑定；重新加入的工具按新增工具使用默认绑定，旧快照仍可解析原值 |
| 共享 registry 的并发能力会话 | 两个会话通过 barrier 交错执行两轮激活，各自的非默认 backend 与历史版本保持独立 |

复现与补测入口：[原 E01 用例](../../../acceptance/round6/test_fix_followups.py)、[本轮补测](../../../acceptance/round7/test_binding_updates.py)。

## 2. 验证结果

| 检查 | 结果与证据 |
| --- | --- |
| 原有套件 | 122 项通过：[日志](../../../acceptance/round7/baseline-results.txt)、[JUnit](../../../acceptance/round7/baseline-results.xml) |
| 最终完整套件 | 125 项通过，覆盖率 93%：[日志](../../../acceptance/round7/final-results.txt)、[JUnit](../../../acceptance/round7/final-results.xml) |
| Ruff / 格式 | 全部通过，62 个 Python 文件格式正确：[规则](../../../acceptance/round7/ruff-results.txt)、[格式](../../../acceptance/round7/format-results.txt) |
| mypy / diff | 32 个源文件类型检查通过，diff 无空白错误：[mypy](../../../acceptance/round7/mypy-results.txt)、[diff](../../../acceptance/round7/diff-check.txt) |
| 构建 | 先构建 sdist，再由 sdist 构建 wheel：[日志](../../../acceptance/round7/build-results.txt) |
| 基础安装环境 | 20 个公开场景通过：[日志](../../../acceptance/round7/installed-base.txt) |
| OpenAI 2.45.0 | 20 个公开场景通过：[日志](../../../acceptance/round7/installed-openai-floor.txt) |
| OTel 1.43.0 | 20 个公开场景通过：[日志](../../../acceptance/round7/installed-otel-floor.txt) |
| OpenAI 2.54.0 + OTel 1.44.0 | 20 个公开场景通过：[日志](../../../acceptance/round7/installed-combined.txt) |

四个环境均重新安装本轮 wheel，并在源码目录外使用 `python -I` 运行既有[公开验证入口](../../../acceptance/round5/installed_suite.py)。检查确认导入来自 site-packages、包含 `py.typed`，且运行环境没有 pytest。

## 3. 制品与验收边界

已核对源码、wheel 和四个已安装包中的 32 个 `.py` 与 `py.typed`，33 个文件全部一致。验收前后的源码补丁逐字节一致；核对范围内的 42 个历史报告及第五、六轮证据文件均保持原样。

| 对象 | SHA-256 |
| --- | --- |
| 未提交源码补丁 | `3a248f587a149a08be8761d54e00c4151a530b738dd9286a5c01e3a3284b4d2a` |
| `agent_runtime-0.1.0-py3-none-any.whl` | `c7d07d1dce9f82d9b78b728e80e98b0e2135c83509ab5fe3897068aac61974ed` |
| `agent_runtime-0.1.0.tar.gz` | `68fb4b4d5ccb5f1776512bdca2c99a79dcd7aa88dd34db9dd0c3764c5ff68e68` |

来源证据：[源码补丁](../../../acceptance/round7/source-changes.patch)、[初始状态](../../../acceptance/round7/initial-state.json)、[核对脚本](../../../acceptance/round7/artifact_manifest.py)、[manifest](../../../acceptance/round7/source-manifest.json)。

当前源码修复和验收材料尚未提交、推送，因此结论限于本地验证；已有 `e21dadf` 的远端 CI 不能替代当前修改的 CI 结果。提交后仍需核对包含这些修复和用例的提交。公司内部接入 I01–I07 及此前记录的 T15 构建依赖精确锁定要求，继续按原任务范围单独验收。

本轮只新增验收材料，未修改运行库源码。可在仓库根目录复跑：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --tb=short --cov=agent_runtime --cov-report=term-missing
.\.venv\Scripts\python.exe -m ruff check src tests examples acceptance
.\.venv\Scripts\python.exe -m ruff format --check src tests examples acceptance
.\.venv\Scripts\python.exe -m mypy src/agent_runtime
```
