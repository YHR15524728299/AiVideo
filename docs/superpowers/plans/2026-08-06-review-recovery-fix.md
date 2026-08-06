# 审核失败恢复修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复无语义审核结果被接受、审核失败被误报为内容打包失败，以及恢复按钮被错误禁用的问题。

**Architecture:** `ReviewResult` 作为结构化审核契约，拒绝只有标点或 JSON 碎片的审核文本；`M2ContentRunner` 在修订耗尽后将失败状态保留在 `SCRIPT_REVIEWED`；`job_actions.py` 统一定义脚本审核失败可重新审核，供 GUI 和 CLI 共用。恢复时失效脚本审核及下游产物，同时保留资料研究和更早阶段。

**Tech Stack:** Python 3.11、Pydantic 2、pytest、SQLite

---

### Task 1: 审核结果语义校验

**Files:**
- Modify: `src/aicf/models/contracts.py`
- Test: `tests/test_m2_contracts.py`

- [ ] 增加失败用例，证明 `":{`、`"]}{` 等只有符号的 `issues` 和 `revision_instructions` 必须被拒绝。
- [ ] 在 `ReviewResult` 的契约层增加可读文本校验，正常中文审核意见保持兼容。
- [ ] 运行契约测试并确认通过。

### Task 2: 修正阶段归属

**Files:**
- Modify: `src/aicf/m2_runner.py`
- Modify: `src/aicf/autopilot.py`
- Test: `tests/test_m2_content_run.py`
- Test: `tests/test_autopilot_full_chain.py`

- [ ] 增加失败用例，证明两轮修订耗尽后失败阶段必须是 `SCRIPT_REVIEWED`。
- [ ] 让 M2 Runner 明确登记脚本审核失败，并返回可读问题摘要。
- [ ] 外层 Autopilot 不再把该结果重新登记为 `CONTENT_PACKAGED`。
- [ ] 运行 M2 与 Autopilot 定向测试并确认通过。

### Task 3: 统一恢复规则

**Files:**
- Modify: `src/aicf/job_actions.py`
- Test: `tests/test_job_actions.py`

- [ ] 增加失败用例，证明脚本审核失败可显示“继续/恢复”并允许 CLI 自动重开。
- [ ] 在共用动作规则中将 `SCRIPT_REVIEWED` 的审核未通过定义为可重新审核。
- [ ] 运行 GUI 动作与 CLI 恢复规则测试。

### Task 4: 回归与现场恢复

**Files:**
- Runtime data only: `outputs/260806`
- Runtime state only: `data/jobs/260806`

- [ ] 运行全量测试和 80% 覆盖率门禁。
- [ ] 合并至 `main` 并确认未跟踪 `config/` 未被提交。
- [ ] 备份 `260806` 当前审核产物和状态。
- [ ] 使用新代码恢复任务，只重新执行脚本审核及后续阶段。
- [ ] 确认研究阶段时间戳不变、审核结果可读、任务成功进入内容打包或给出可恢复失败。
- [ ] 推送 `main`，确认本地与远端提交一致。
