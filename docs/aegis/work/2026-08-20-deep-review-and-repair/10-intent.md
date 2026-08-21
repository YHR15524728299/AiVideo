# 任务意图

## Requested outcome

先全部Review，再制定计划与标准，按工作模块开发；开发后逐项核对计划、修正偏差，最后汇报证据和结果。

## Goal

消除任务状态、按钮、Worker 生命周期和持久化之间的结构性不一致，使停止、崩溃、恢复、重启和并发启动均安全、可测试、可观测。

## Success evidence

- 全量测试零失败。
- 覆盖率不低于 `80%`。
- `compileall`、CLI冒烟、`git diff --check`通过。
- PID复用、终止失败、并发启动、数据库不可读、快照写失败、停止中断、按钮恢复合同均有测试。
- GUI状态派生不在UI线程执行文件、数据库或进程IO。
- SQLite、Worker记录和快照达到计划定义的一致性。

## Stop condition

- `done`：验收证据全部满足，计划逐项核对无遗漏。
- `blocked`：缺少无法替代的权限、外部依赖或必要输入。
- `needs-verification`：实现已完成但证据不足。
- `scope-exceeded`：继续需要改变视频业务流程、外部Provider或公开兼容边界。

## Non-goals

- 不重写视频生成业务链。
- 不更换GUI框架、数据库或Provider。
- 不擅自发布、上传或提交Git。

## Baseline refs

- `docs/aegis/baseline/2026-08-20-deep-review-baseline.md`
- `README.md`
- `docs/HANDOFF_LATEST.md`
- 当前 Git diff 和全量测试输出。

## Risk hints

进程误杀、双写漂移、竞态启动、UI卡顿、静默异常和旧测试合同漂移。

## Route

深度Review → durable plan → 分模块TDD实施 → 验证与偏差修正 → 汇报。
