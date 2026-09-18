# Agent Runtime 文档

这里集中维护 Agent Runtime 的设计、实现计划、适配说明和验收记录。

## 设计与交付

- [集成层设计](integration-design.md)
- [技术选型](technology-selection.md)
- [开发任务清单](development-tasks.md)
- [修复计划](fix-plan.md)
- [适配指南](adapter-guide.md)
- [升级说明](upgrade-notes.md)

## 验收记录

验收报告按轮次归档；每份报告引用的测试脚本、日志、JUnit、manifest 和 CI/SDK 证据保留在仓库根目录的 [`acceptance/`](../../acceptance/) 下。

- [第一轮：初始验收](acceptance/round1-acceptance.md)
- [第二轮：修复后复验](acceptance/round2-reacceptance.md)
- [第四轮：边界修复验收](acceptance/round4-acceptance.md)
- [第五轮：集成边界验收](acceptance/round5-acceptance.md)
- [第六轮：能力绑定补测](acceptance/round6-acceptance.md)
- [第七轮：能力更新验收](acceptance/round7-acceptance.md)

本地缓存、`__pycache__`、`.mypy_cache` 和临时构建制品不纳入版本控制。
