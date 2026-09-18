# Agent Runtime 修复计划

目标：修复第二轮验收中的 B01–B07，并让公共 v0.1 在安装制品、默认运行链路和参考适配器上满足验收门槛。

## 1. 修复顺序

### P0：记录、终态和预算安全

1. **回执重放（B04）**
   - 在 `_CommittedOp` 中保存完整的结果回执集合及 run、step、target、logical-op 身份。
   - 首次提交和重放统一执行身份、payload、previous receipt 校验。
   - 重放直接返回已保存的消息回执，不再按 `call_id` 全局搜索。
   - 验证跨步骤、跨目标、错误 run 和重复提交均不会串写或返回错误消息。

2. **WAIT 终态（B05）**
   - 在发布 `RunWaiting` 前完成 `WAITING` 最终化。
   - 统一成功、失败、取消、WAIT 和 `CompletedEntry` 的终态发布顺序：最终化、观测、对外事件。
   - 最终化失败时不发布成功或等待终态。

3. **执行预算（B07）**
   - `remaining_attempts == 0` 时禁止发起模型请求。
   - 在核心分配阶段检查恢复入口的累计 token 预算。
   - 明确累计估算规则：每次模型尝试计入 prompt、工具 schema、调用参数和输出估算；provider usage 单独记录，避免重复计算。

### P1：生命周期和观测

4. **初始化清理（B03）**
   - 将 observer、scope、应用快照和能力解析放入统一的 `try/finally` 所有权范围。
   - 初始化异常、用户停止、宿主取消都要取消并等待已创建任务，不能留下 observer-pump。

5. **终态观测（B06）**
   - 所有 `RunSucceeded`、`RunFailed`、`RunCancelled`、`RunWaiting` 通过统一发布函数进入 observer 队列。
   - 保证观察者异常、阻塞和队列满不改变运行控制结果。

### P1：OpenAI 参考适配器

6. **递归 JSON 转换（B01）**
   - 在适配器边界统一调用 `thaw_json()`，处理工具 schema、工具参数、嵌套对象和数组。
   - 通过真实 OpenAI SDK 对象和 HTTPX MockTransport 验证 2.45.0 与锁定版本。

7. **流超时（B02）**
   - 让 timeout 覆盖 SDK 创建、首个 chunk、chunk 间等待和关闭。
   - 每个读取任务使用明确的剩余 deadline，取消时等待底层流退出。
   - 验证首包超时、首个 delta 后超时、正常完成、取消和共享 client 不被误关闭。

## 2. 验证顺序

1. 先运行新增回归用例，确保 B01–B07 各自有失败前置和通过后断言。
2. 运行完整 pytest 套件，目标为当前 74 项加新增回归全部通过。
3. 运行 Ruff、format、mypy。
4. 用 wheel 在源码目录外验证基础包、`openai`、`otel` 和组合 extra。
5. 用 OpenAI 2.45.0 与锁定版本执行 HTTPX MockTransport 场景。
6. 重建 sdist/wheel，记录哈希，并从安装制品运行公开 Harness 的五个场景。
7. 在 CI 矩阵中取得 Linux Python 3.12/3.13/3.14 和 Windows Python 3.12 的实际结果。

## 3. 完成标准

- 所有 B01–B07 回归用例通过。
- 完整 pytest、Ruff、format、mypy 全部通过。
- OpenAI 两个版本的文本、工具 schema、嵌套历史和中途超时 mock 场景全部通过。
- 初始化、取消和消费者关闭后无残留任务或流。
- 回执重放保持原始消息 ID，错误身份不会改变已提交快照。
- WAIT 在对外事件可见前已提交 `RunStatus.WAITING`。
- 安装制品和 extras 组合均可运行公开 Harness。
- 更新验收报告，记录命令、制品哈希和平台矩阵结果；未完成前不标记 v0.1 通过。
